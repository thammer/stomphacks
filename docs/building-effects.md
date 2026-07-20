# Building an effect

You write two files: a `manifest.json` (the effect's identity and its knobs)
and a C kernel (the audio math). One command turns them into an installable
effect:

```
.venv/bin/python3 tools/zd2_make_effect.py effects/gain/manifest.json
```

Output lands in `effects/gain/build/`: the upload pair (`DIYGAIN.ZD2` +
`DIYGAIN.ZIC`), review disassemblies, and a `REPORT.md` whose checks must ALL pass
before the effect goes near a pedal.

The pipeline generates everything around your kernel: the entry stub, init,
bypass handling, the parameter table, the edit handlers, the icon. All of it
from the documented ABI, which is in [zd2-abi.md](zd2-abi.md). The ZD2
container and the ZIC icon are synthesized from scratch too, so nothing from a
stock Zoom file enters the build. See
[ip-and-licensing.md](ip-and-licensing.md) for what that means.

Start by copying [../effects/gain/](../effects/gain/). It is a one-knob level
trim, the simplest complete effect, and it is hardware-proven. Rename its
identity fields, then replace the math.

## What you need

1. TI C6000 Code Generation Tools 8.5.0 (`cl6x`, `asm6x`, `lnk6x`), free from
   TI, and shipped with Code Composer Studio.
2. Python 3 and the deps:
   `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
3. A clone of https://github.com/mungewell/zoom-zt2 inside this repo, at
   `zoom-zt2/` in the root. It does the ZD2 file handling and the MIDI
   transfers.

No stock effect file is needed, and none ships here.

## The manifest

```json
{
  "name": "MyEffect",        // catalogue name, 10 chars or less
  "filename": "MYEFFECT",    // pedal flash filename, 8 chars of A-Z 0-9 _
  "id": "07000f70",          // your effect ID, see effect-ids.md
  "version": "1.00",
  "display": "MY EFFECT",    // the text drawn on the icon. The name field is
                             // NOT what the pedal shows you.
  "badge": "MY",             // icon corner badge
  "description": "...",      // container text, and the app display
  "kernel": "myeffect.c",
  "state_bytes": 16,         // per-instance state, zeroed at init. 0 to 188.
  "scaffold": "cleanroom",   // the default, and the only supported value here.
  "dspload": 8,              // the pedal TRUSTS this number. Declare honestly.
  "params": [
    { "name": "Level",       // 12 chars or less
      "max": 100,            // knob range 0..max, in integer clicks
      "default": 80,
      "curve": "stock_vol",  // optional: the decoded stock VOL curve. Unity at
                             // 80, +3.5 dB at 100. The kernel then reads
                             // coeff[slot] directly as an amplitude.
      "explanation": "Output level. 80 is unity gain." }
  ]
}
```

One to twelve knobs, in UI order (the pedal pages 4 knobs across up to 3
pages). Pick the id per [effect-ids.md](effect-ids.md).

About `dspload`: every effect declares a DSP load number, and the pedal trusts
it completely. It sums the DECLARED values of a patch and refuses to add an
effect that would exceed its budget, without ever measuring what your effect
really costs. So declare an honest number. A trivial gain or utility effect
declares 8, which is what stock effects of that size declare.

The less common keys:

| Key | What it does |
|---|---|
| kernel_section: "text" | Put the kernel in .text instead of the 1472-byte .audio section. For big kernels. Stock-precedented: the big stock reverbs run entirely from .text. Budget 16 KB. |
| allow_stack: true | Let the kernel use the stack and callee-saved registers. Stock-precedented. Calls are still banned, and this needs opt_for_space: null, because any -ms level makes the compiler CALL a save helper. |
| opt_for_space: 3 | The default. Plain -O2 (null) unrolls hard, and a kernel can quintuple in size and blow the .audio budget. |
| init_edit_calls: true | The default. Keep it. Init calls every edit handler, which is how your knob values survive the init re-run the firmware does on EVERY chain edit. Without it, knobs silently revert to defaults whenever the user edits the chain. |
| strip: true | The default. The shipped blob loses its debug symbols, which are 40% of the file and which the loader never reads. |

## The kernel

The generated `build/sh_params.h` defines the macros.

* Signature: `void SH_AUDIO_FN(void **instance, void **ctx)`, called once per
  audio block.
* The effect bus is `(float *)ctx[1]`, processed IN PLACE: 16 frames of channel
  A at [0..15], channel B at [16..31] (`SH_CH_B_OFFSET`). 44.1 kHz, float32.
* Knob values arrive as floats 0..1 (knob/max) in `coeff[SH_PARAM_<NAME>]`. The
  exception is a `"curve": "stock_vol"` knob, where the coefficient IS the
  curved amplitude, 0..1.5.
* `coeff[SH_COEFF_BYPASS]` is the firmware's bypass crossfade, ramping 0..1.
  Mix `dry * (1 - fade) + wet * fade` and bypass is exact and click-free.
* Per-instance state lives at `instance[2]`, `state_bytes` of it, zeroed at
  init.

Your kernel touches the effect bus and nothing else. Other context slots exist,
and some of them are live firmware state that will freeze the pedal if you
write to them. The bus model is not covered here. Do not go poking.

### The guard prologue, and the output clamp

These are mandatory. Both exist because their absence crashed a real pedal.

The firmware can run your audio function against an instance whose init has not
run yet. This is routine, not rare: the pedal LIVE-PREVIEWS every effect the
user scrolls past in the browser. A kernel that computes with garbage
coefficients writes Inf or NaN onto the bus, and the pedal's audio then latches
silent until it is power-cycled.

So every kernel does this:

```c
    unsigned ok = 1;
#ifdef SH_STATE_GUARD
    if (((const uint32_t *)instance[2])[SH_STATE_GUARD_WORD]
            != SH_STATE_GUARD)
        ok = 0;                  /* init has not run yet */
#endif
    float fade = coef[SH_COEFF_BYPASS];
    if (!(fade >= 0.0f && fade <= 1.0f))
        ok = 0;                  /* garbage or NaN fade */
    /* ...the same range check for every coefficient you consume... */

    if (!ok) {                   /* write SILENCE. Never forward whatever the
                                  * transitional bus happens to hold. */
        for (i = 0; i < SH_FRAMES; i++) {
            eff[i]                  = 0.0f;
            eff[i + SH_CH_B_OFFSET] = 0.0f;
        }
        return;
    }
    ...
    /* and at EVERY bus write. NaN fails any comparison, so this scrubs NaN,
     * Inf and torn-read garbage from everything you write. */
    if (!(out > -8.0f && out < 8.0f)) out = 0.0f;
    eff[i] = out;
```

The +/-8 bound: nothing legitimate in the shipped effects exceeds about 3.5.
Widen it if your kernel's honest output can approach 8. The cost is a couple of
float compares per sample, which is nothing.

One trade-off worth knowing. A math bug that produces NaN now sounds like quiet
one-sample dropouts instead of latching the pedal. When you are debugging
suspected kernel math, take the clamp out temporarily to make the bug audible,
then put it back.

One more way to defeat the guard, learned from a real crash: checking the
coefficients once at the top of the block, but then reading `coef[...]` again
inside the sample loop (declaring the coefficient pointer `volatile` forces
exactly that, because the compiler then reloads it from memory on every use).
The firmware can update coefficient memory while your block is running, so
those fresh reads can see values the check at the top never saw, and the loop
computes with unchecked data. Read each coefficient into a local variable
once, check the locals, and use only the locals in the loop. That is what the
gain kernel does.

### SAFE-DSP rules

The verifier enforces a discipline on the kernel, and each rule has a concrete
reason:

* No calls, which is what "leaf function" means. A call needs code to land in,
  and the blob links no runtime library; the firmware resolves no symbols for
  you either. A call to anything outside the blob is a jump into nowhere.
* No divides and no stdlib math are the same rule in disguise. This DSP has no
  divide instruction and no math library, so `/` and `sinf` both compile into
  calls to runtime helpers that are not in the blob. Use reciprocal and inline
  approximations instead.
* No stack and no callee-saved registers go together. The calling convention
  says registers A10..A15 and B10..B15 belong to the caller, so a kernel that
  touches them must save them first, and saving needs a stack frame. A kernel
  that stays out of both cannot corrupt the firmware's register state and
  needs no prologue at all.
* No statics. A static variable would land in a writable data section the blob
  does not carry, and even if it loaded, one static would be shared by every
  running instance of your effect. Per-instance state lives in `instance[2]`
  (`state_bytes` in the manifest), allocated per instance and zeroed at init.

Watch register pressure. Per-sample smoothing inside an interleaved loop can
push the compiler into callee-saved registers, which forces a stack frame that
the verifier rejects. The fixes that worked: smooth the staged values, and
share derived products across the two channels.

Big kernels can escape the no-stack rule with `allow_stack` and
`kernel_section`. "No calls" is absolute.

### Smoothing

A knob write is a step. Every audible-path parameter needs smoothing or it
zipper-clicks on a fast twist. For a level-like knob: `"curve": "stock_vol"` in
the manifest, plus a one-pole in the kernel, tau about 11 ms:

```c
    s += 0.002f * (target - s);    /* per sample, at 44.1 kHz */
```

`effects/gain/gain.c` is the reference.

### Signal levels

A guitar into the pedal measures about 0.24 average and 0.70 peak of float full
scale. That was measured on one rig, judged by ear.

It matters because desktop algorithms usually assume a signal that reaches
+/-1.0. Ported straight over, anything with a level-dependent nonlinearity is
inaudibly subtle at pedal levels. Such a port needs drive staging around its
nonlinearity: multiply the input by D, divide the output by D.

## What REPORT.md proves

The build is not done until every check in `build/REPORT.md` passes:

* Structural: the generated tables, handlers and descriptor match the manifest
  and the documented ABI. The relocations are only the three types the loader
  supports.
* SAFE-DSP asserts on the kernel (leaf, no stack, no calls), unless the
  manifest relaxed them.
* Provenance: the blob is the generated scaffold plus your compiled kernel, and
  the container and icon are synthesized from scratch. No stock file enters
  the build anywhere.
* CRC, and a byte-exact container round-trip.
* The container envelope: the constant 120 at header offset 4, a valid
  checksum, the MS Plus target bit, sections that tile the file exactly to
  the trailer, the constant-zero header bytes, a group byte that matches the
  effect ID, and a printable name and version. The icon's structure is
  checked the same way (the pedal parses it too). Every stock effect and
  icon satisfies these invariants, the build fails if its output does not,
  and the installer independently refuses to upload a file that fails them.

A build with a failed check never goes near a pedal, and never gets
distributed. See [ip-and-licensing.md](ip-and-licensing.md).

## Installing it

That is hardware territory. [../SAFETY.md](../SAFETY.md) has the procedure:
install the icon with the effect, read it back and compare, uninstall first
when replacing, and never save a patch that contains it.

The short version of everything that has ever gone wrong: read SAFETY.md.

## Needs investigating

* The real size of the `instance[2]` state grant. 192 bytes is proven. Stock
  effects have been seen using more.
