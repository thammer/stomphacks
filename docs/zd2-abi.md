# The ZD2 ABI

The application binary interface (ABI) is the contract between the code inside a
ZD2 effect file and the pedal firmware: how the firmware finds your code, what it
hands you, where the audio lives, and the rules that keep the pedal alive.

All of it was worked out by disassembling stock effects, restating the facts in
my own words, building small probe effects, and confirming the result on an
MS-70CDR+ running firmware 1.20. No stock code appears in this repo. The how
and why of that is in [docs/ip-and-licensing.md](ip-and-licensing.md). Facts
confirmed on hardware say so below; so do the ones that are not.

This doc covers what you need for a simple effect that processes the audio it is
handed: a gain, a filter, a waveshaper. The audio buses between effects, the
delay-line memory, and the full host service table are not covered here. If
you just want to build the worked example and hear it run, start at
[../README.md](../README.md); this page is the reference underneath it.

## What the code blob is

The code section of a ZD2 file is a complete ELF (Executable and Linkable Format)
shared object for the TI C6000 DSP: the C674x core, little-endian, EABI. Some
facts about it, all confirmed across the 149 stock MS-70CDR+ effects:

* Relocations are exactly three types: R_C6000_ABS32, R_C6000_ABS_L16 and
  R_C6000_ABS_H16. Nothing else, anywhere. The loader accepts only these three;
  an ELF carrying any other relocation type will not load. A generated ELF must
  emit only these.
* Blobs are fully self-contained. Zero undefined symbols, and the firmware
  provides none. Any runtime-support routine your code needs must be linked into
  the blob.
* The loader is name-blind. It enters at e_entry and reaches everything else
  through pointers. Two stock effects export no entry symbol at all and run fine;
  an effect with every symbol renamed loads and runs. The symbol names are
  convention, not contract.
* Sizes run from 1.3 KB to 72 KB, median 6 KB. Big blobs are fine.

The compiler flags that match the factory build model:

```
--abi=eabi --array_alignment=8 --endian=little --long_precision_bits=32
--mem_model:code=near --mem_model:const=data
--mem_model:data=far_aggregates --object_format=elf --silicon_version=6740
```

`--mem_model:data=far` matters: the firmware does not set up the B14 data-page
register for plugins. The build pipeline handles all of this. You will only care
if you go looking.

## How the firmware finds your code

Your effect exports one entry stub, named `Dll_<Name>` by convention. The
firmware calls it once with a pointer to a struct, and the stub fills in three
fields: how many parameter-table entries there are, a pointer to the
parameter/function table, and a pointer to the image info. The parameter table
then points at everything else: the init function, the on/off (bypass) handler,
the audio function, and one edit handler per knob.

Handlers follow the naming `Fx_<GROUP>_<Name>`, plus `_init`, `_onf` and
`_<Param>_edit`. The whole stock corpus follows that convention, and the loader
does not care about any of it (see name-blindness above).

You write the audio function. The build pipeline generates the stub, the table
and the handlers from your manifest:

```
.venv/bin/python3 tools/zd2_make_effect.py effects/gain/manifest.json
```

The worked example is the gain effect (effects/gain/): one knob, under a
hundred lines, and the folder to copy to start your own. Pick your own effect
id per [docs/effect-ids.md](effect-ids.md); the build tools are described in
[tools/README.md](../tools/README.md).

## The audio call

Your kernel, the audio function, is called once per audio block, with the
instance pointer in register A4 and the context pointer in register B4.

| Property | Value |
|---|---|
| Sample rate | 44100 Hz, confirmed on hardware |
| Sample format | IEEE-754 float32 |
| Block | 16 frames per channel, 2 channels, 32 samples per call |
| Layout | Channel A at word offsets 0..15, channel B at 16..31 (a 64-byte stride) |
| Your bus | ctx[1], the effect bus. Read it, process it, write it back in place. |

The two-channel layout is where stereo lives. Channel A is the left samples and
channel B the right; a level knob applies the same gain to both, while a true
stereo effect keeps separate per-channel state.

WARNING: your kernel touches ctx[1] and nothing else. Other context slots
exist: some carry audio to and from other effects, some are live firmware
state, and writing to the wrong one will freeze the pedal hard, requiring a
power-cycle. The full bus model is not covered here. Do not go poking at other
slots.

## The instance

The instance pointer is the first argument to every handler. It is an array of
pointers:

| Slot | What it is |
|---|---|
| instance[0] | The host's parameter object (the thing get_param reads) |
| instance[1] | The live coefficient table (float32) |
| instance[2] | Your per-instance state, state_bytes of it, zeroed at init |

A fourth slot points at delay-line memory; it has rules of its own and this
page does not cover them. Leave it alone.

No stock blob declares how big the state area is. The observed safe floor is 192
bytes: one stock effect zeroes 644, so the real grant is probably larger, but
192 is what is proven. Keep all your mutable per-instance state in instance[2].

## Coefficients

The coefficient table (instance[1]) is where your knob values arrive as float32,
one per slot. Three slots are not yours:

* coeff[0] and coeff[1] are the firmware's bypass crossfade pair. The on/off
  handler ramps them (see Bypass below), and anything you park in coeff[1] gets
  wiped on every bypass toggle. Never repurpose them.
* coeff[4] is host-owned. Stock effects multiply their output by it, but the
  writer that sets it is unknown, so it is never assigned to a knob. Treat it as
  reserved.

Your knobs get the rest, and the pipeline assigns them. More coefficient slots
and host services exist than this page lists; the ones here are what a knob
effect needs.

### The stock VOL curve

Every stock VOL knob, 0 to 100, maps through the same curve, decoded and
confirmed end to end on hardware:

```
v <= 80:  gain = v / 80             (0 = mute, 80 = 1.0, unity)
v >= 81:  gain = 1 + (v - 80) / 40  (steeper: 90 = 1.25, 100 = 1.5)
```

Unity at the stock default of 80, and +3.5 dB at 100. Put `"curve": "stock_vol"`
on a parameter and the pipeline generates this, so your Level knob feels exactly
like a stock one. The coefficient your kernel then reads IS the amplitude:
multiply by it.

## Host services

The context (B4) carries function pointers to a handful of firmware services, at
fixed byte offsets. Your handlers reach them through the context; the audio
function itself usually calls none of them. The pipeline wires up the calls, so
this table is mostly here to explain how init and bypass work, and for when you
write a handler by hand.

| Offset | Service | What it does |
|---|---|---|
| ctx+12 (word 3) | ramp(dest, target, rate) | Register a host-driven fade ramp on one float. The on/off handler uses it at rate 705.6 for the bypass crossfade. Confirmed on hardware. |
| ctx+16 (word 4) | smooth(dest, target, rate) | Host-side parameter smoothing, the stock VOL/Bal glide. Confirmed on hardware, with the caveat below. |
| ctx+160 (word 40) | get_param(obj, index) | Return the current UI value of parameter-table entry index. First argument is instance[0]. Confirmed on hardware. |
| ctx+172 (word 43) | memcpy(dst, src, nbytes) | init uses it to copy the effect's ROM coefficient defaults into the live table. Confirmed on hardware. |
| ctx+176 (word 44) | memset(dst, val, nbytes) | init uses it to zero the per-instance state area. Confirmed on hardware. |

The smoothing caveat: the ctx+16 smooth service is how stock effects glide their
VOL and Bal knobs without zippering. When a DIY kernel consumes its output,
though, a fast knob twist zipper-clicks, and how stock stays click-free through
the same service is still open. So the approach that works, and the one the gain
effect uses, is to keep the stock_vol curve on the parameter and smooth the value
with a one-pole filter inside the kernel, per sample. That is click-free and does
not lean on the service at all.

## Bypass

The on/off handler reads the on/off parameter, then registers two ramps at rate
705.6: ON ramps coeff[0] to 1.0 and coeff[1] to 0.0, OFF does the reverse. That
is the whole bypass crossfade.

Your kernel mixes against it: `dry * (1 - fade) + wet * fade`, reading the fade
from coeff[0] (1.0 when the effect is active, 0.0 when bypassed). That makes
bypass exact and click-free, and it is why coeff[0] and coeff[1] are off-limits
for your own values.

Edit handlers fire while the effect is bypassed, confirmed on hardware, so they
must be safe to run in any on/off state. They read a parameter, write a
coefficient or register a ramp, and nothing else.

## Init, and why it runs more often than you think

init does three things: copy the default coefficients into the live table (the
ctx+172 memcpy), zero the state area (the ctx+176 memset), and then call every
edit handler once.

That third step matters more than it looks. init re-runs on EVERY chain edit
(any insert or delete anywhere in the patch), and the init-time edit calls are
how the user's knob values re-materialise afterwards. Skip them and your
parameters silently revert to their defaults every time the user edits the
chain. The manifest key `init_edit_calls` defaults to true; leave it that way.

### The hazard this creates

The firmware can run your audio function against an instance whose init has NOT
run yet. This is routine, not rare, and the reason is the effect browser: the
pedal live-previews every installed effect the user scrolls past, instantiating
it in the chain as they go. So an installed effect is running code even when no
patch uses it, a plain gain included.

A kernel that computes with a garbage coefficient table writes Inf or NaN onto
the effect bus, and the firmware's float state can latch silent until the pedal
is power-cycled.

So the kernel has to do two things, both attributed to real pedal crashes, not
theory:

* Verify init has run before it trusts the coefficient table, and if anything is
  not yet sane, write SILENCE rather than forwarding whatever the transitional
  bus happens to hold. The standard way is an init-written guard word in the
  state area that the kernel checks first: no guard word, no trust, fail closed
  to silence.
* Clamp every sample it writes to a sane range. NaN fails every comparison, so a
  range check like `if (!(x > -8.0f && x < 8.0f)) x = 0.0f;` scrubs NaN, Inf and
  torn-read garbage in one line.

The gain kernel (effects/gain/gain.c) shows both, with comments; copy its guard
prologue and its output clamp into anything you build. These are not style:
each one is there because its absence crashed a real pedal. Read
[../SAFETY.md](../SAFETY.md) before you put any DIY effect on hardware, and
install and audition it with the safe wrapper described in
[tools-pedal/README.md](../tools-pedal/README.md).

## Needs investigating

* Three fields that most stock effects write into the entry struct. Their meaning
  is unknown. They are proven NOT to size the delay memory, and they are not a
  function of the table sizes.
* The real size of the state grant above the proven 192-byte floor.
* Writable data sections (.fardata, .far) are accepted by the loader, but the
  per-instance semantics are unknown; two chain instances of one effect might
  share them. Until that is settled, keep mutable state in instance[2].
* How stock effects consume the ctx+16 smoothing service without zippering on
  fast knob twists. The kernel one-pole sidesteps it, so this is knowledge
  worth having rather than a blocker.
