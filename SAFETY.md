# Safety

WARNING: a broken effect saved into a patch can brick your Zoom MS Plus pedal.
There is no known recovery. Preventing that one thing is the entire strategy on
this page, and none of the rules below are optional.

Everything here exists because of that single fact. The rules are short. Follow
all of them, every session, even when a test will only take a second.

This comes from work on an MS-70CDR+ running firmware 1.20. As far as anyone
knows the boot and patch mechanics are the same across the MS Plus series, but
I have only worked on the one pedal, so treat the rest of the series as very
probably the same rather than confirmed.

The commands below are run from the repo root; on Windows use
`.venv/Scripts/python.exe` in place of `.venv/bin/python3`. SysEx (MIDI System
Exclusive) bytes are hexadecimal.

## The risk model

On boot, the pedal loads the current patch from patch memory. If that saved
patch uses an effect whose code crashes the pedal's audio processor (the DSP),
the pedal crashes on boot, and then on the next boot, and the one after that.
That is the brick. There is no rescue boot mode and no PC-side recovery, and the
official firmware updater needs a booted, USB-responsive pedal to run at all.
That is consistent with Zoom's own firmware update guide and with the brick
reports in zoom-zt2 issues 103 and 109.

Two things prevent it, and both have to hold at all times:

* Autosave OFF. A crashed experiment is then never written to memory, so a
  power-cycle brings back the last saved patch.
* Saved patches contain only official Zoom effects. Your experimental effects
  live in the current, unsaved patch and nowhere else.

The good news falls out of the same mechanics. An effect that crashes in the
CURRENT patch is recoverable by design: power-cycle, the pedal comes back on the
last saved patch, and you delete the bad effect. A missing or malformed effect
file is gentler still: the pedal boots and shows the effect as missing
(zoom-zt2 issue 100). It is crashing code in a SAVED patch that bricks by this
route. So one half of the strategy is: keep autosave off, and never let an
experimental effect reach a saved patch.

## An invalid ZD2 file can brick your pedal too

There is a SECOND brick path, and it has nothing to do with patches. It is not
hypothetical either: it resulted in 2 bricked pedals during my work.
Both times, the file being uploaded was invalid: its container
header carries a 4-byte value at bytes 4-8 that every stock effect and every
valid build sets to the same constant, and this file carried an incorrect
value there instead (a build bug, since fixed). Uploading that invalid file
is what hung both transfers, and the power-cycle that followed each hang left
that pedal permanently unable to boot.

Installing or uninstalling an effect writes files to the pedal's internal
storage over a SysEx "file API", and rewrites `FLST_SEQ.ZT2`, the master effect
list the pedal reads FIRST when it boots. A transfer that hangs partway - from
an invalid file, a lost MIDI reply, a killed process, an unplugged cable - can
leave the pedal with a half-written file and its file-transfer session still
open. The pedal is still alive at that point (it keeps enumerating over USB),
but that is not the same as being responsive to commands. **Whether the pedal
can actually be recovered from there is NOT established.** In the two losses
this project has seen, recovery-while-powered was either never attempted (the
first predates the rescue tooling below - the advice at the time was to
power-cycle, which is what happened) or attempted for about 20 minutes and
did not succeed (the second - no successful reply was ever received back
from the pedal during that whole attempt, and the power-cycle that followed
came only after recovery had already failed, not as its cause). What IS
certain: a POWER-CYCLE while a transfer session is still open
makes the boot code parse a half-written filesystem, and it can hang there
forever, before it ever looks at a patch, foreclosing whatever chance staying
powered might have offered. Autosave being off does not help; no patch
referencing the effect does not help; the corruption is in the FILE STORAGE,
not in any patch.

So the other half of the strategy is two things: **never upload a file that
fails the container-envelope check** (below - this is exactly the check that
now refuses the file that caused both losses above), **and if a transfer ever
hangs for any reason, DO NOT POWER-CYCLE - keep the pedal powered and attempt
a clean session close first.** That does not guarantee recovery - it is the
only thing known to keep the option open at all.

The install tooling here is built to make both automatic. Every upload is
checked against the container-envelope invariants before a single byte reaches
the pedal, so the exact file that caused both 2026-07 losses is refused
outright now, before it ever touches the pedal. On top of that: every pedal
reply has a timeout, so a lost reply becomes a fast error instead of an
endless hang. Any failure triggers a clean close of the file session before
the tool exits, so the pedal is never abandoned mid-write. The new effect list
is computed and validated on the PC before a single byte is sent. Every
uploaded file is read back and byte-compared before it counts. And if a
transfer still cannot be verified safe, the tool prints a loud
DO-NOT-POWER-CYCLE banner and points you at `pedal_diy.py rescue`, which
closes the session and checks the effect list is intact. All of this failure
behaviour is proven off-hardware against a mock pedal
(`tools-pedal/install_selftest.py`) before any real pedal is touched -
because the one thing you must never discover on real hardware is what your
tool does when a transfer fails.

## Before anything else, every session

Run the connect script first. It is the session opener, and it does the
safety-critical setup for you:

```
.venv/bin/python3 tools-pedal/safe_connect.py <output-dir>
```

It connects over USB-MIDI, reads and records the pedal's firmware version, and
turns autosave OFF by sending:

```
F0 52 00 6E 64 20 00 64 0F 00 00 00 00 00 F7
                        ^^ 0F selects autosave; the next byte (00) is the flag, 00 = off
```

Then it reads the pedal's reply. The pedal echoes back the value it actually
applied, so the script CONFIRMS autosave is really off rather than assuming the
command worked. If the pedal will not confirm it, the script says so, and you
stop and check autosave on the pedal's own menu before doing anything else.

Finally it downloads the current patch and prints the effect names in it, so you
can confirm by eye that only stock Zoom effects are loaded. If you see one of
your own effects in there, remove it before you go any further.

Then, still every session:

1. Make a full backup before your first write - every effect, every patch, 
and the effect list itself in FLST_SEQ.ZT2:

   ```
   .venv/bin/python3 tools-pedal/backup_pedal.py <output-dir>
   ```

   Do this at least once per pedal, and again before anything risky. A dated
   backup is cheap; a bricked pedal is not.

2. Note your firmware version (safe_connect.py records it) and then leave it
   alone. The format and ABI knowledge here may be firmware-specific, and a
   firmware update can invalidate it silently.

3. Never save a patch that contains an experimental effect. Audition it in the
   current patch and DO NOT press save on the pedal. This is the rule the whole
   risk model rests on.

4. Give every experimental effect its own identity: its own effect ID from the
   DIY range (see [docs/effect-ids.md](docs/effect-ids.md)), its own filename of
   8 characters or fewer, its own name and its own icon. Never reuse or shadow a
   stock effect's ID, name or filename.

5. One program talks to the pedal at a time. It is a single physical MIDI
   device, and two writers at once is undefined behaviour. (On Windows, a
   browser tab holding the WebMIDI port counts as one, so close it. 
   Sometimes Chrome might hold the MIDI port even after the tab is closed. 
   If you suspect that is the case, restart Chrome).

## Installing and updating effects

Install and uninstall your own effects with the DIY installer:

```
.venv/bin/python3 tools-pedal/pedal_diy.py install   MYGAIN.ZD2
.venv/bin/python3 tools-pedal/pedal_diy.py uninstall MYGAIN.ZD2
```

It is a deliberately narrow, fail-closed wrapper, and it is the safe way to
install. It only ever installs or uninstalls an effect BINARY, and it refuses to
touch anything that is not clearly your own DIY effect: it checks the ID, name,
filename and icon against a built-in catalog of every stock effect on the MS
Plus series and stops if any of them collide. It never writes a patch or a
memory slot, so it cannot put your effect into the boot patch - closing ONE of
the two brick paths above. It closes the OTHER one (the invalid-file / hung-
transfer path) by construction too: the whole transfer runs over a single
connection this one process owns, every pedal reply is timeout-bounded, any
failure cleanly closes
the file session before the tool exits, the new effect list is validated on the
PC before any write, and every uploaded file is read back and verified. Every
upload must also pass a container-envelope check first: the header constants,
checksum, target bits, identity bytes and section layout have to match the
invariants every stock effect satisfies, and an install requires the
companion icon to pass the same kind of structural check, or the tool
refuses before it touches the pedal. A
container that broke the first of those invariants was uploaded immediately
before both pedal losses described above, so nothing out of invariant is
allowed near a pedal's storage again; uninstall stays exempt so a bad file can
always be removed. Before
any real install or uninstall it also forces autosave OFF and requires the
pedal's own ACK to confirm it, so even a session that skipped the opener gets the
autosave invariant enforced before the pedal is touched. If it refuses, you are
exactly where you started; drive zoom-zt2 by hand instead and confirm each
command yourself. To see the accept-or-refuse decision without touching the
pedal at all, run `pedal_diy.py classify MYGAIN.ZD2` first.

If an install or uninstall ever fails partway, read what the tool prints. If it
says the pedal was verified SAFE, nothing changed - fix the cause and retry. If
it prints the DO-NOT-POWER-CYCLE banner, **leave the pedal powered** and run
`pedal_diy.py rescue` (optionally with a known-good `FLST_SEQ.ZT2` from your
backup); it closes the session and checks the effect list before you reboot.
Add `--remove FILE.ZD2` to also remove that effect (and its icon) if the
failed transfer left it as an orphan the effect list does not reference -
it refuses to touch anything the list still references (that is what
uninstall is for) or anything that does not classify as one of your own DIY
effects. This option is proven off-hardware against a mock pedal, the same
way as everything else in this file, but is new and not yet
hardware-validated - what actually recovers a real wedged pedal is still an
open question (see the section above).

Every step below talks to the pedal. Take them one at a time and check the
result before the next one.

1. Always install the icon with the effect. pedal_diy.py does this for you and
   will not install without the companion .ZIC file. The pedal's effect browser
   renders icons, not names, so an effect installed without its icon is a blank
   tile you cannot identify.

2. Read the effect back off the pedal and compare it, byte for byte, against the
   exact build artifact you uploaded:

   ```
   .venv/bin/python3 tools-pedal/readback.py MYGAIN.ZD2 readback/MYGAIN.ZD2
   ```

   The two files must be identical. Compare against the SAME build you uploaded,
   not a fresh rebuild: the container is not byte-reproducible. Two builds of
   identical source differ in about 11 bytes (a randomised toolchain symbol name
   and the CRC that covers it), so a rebuild differs even when the code is the
   same.

3. To replace an effect under a filename that is already on the pedal: UNINSTALL
   FIRST, then install. If a file of that name is already present, the install
   skips the binary upload: it re-adds the catalog entry and silently leaves
   the OLD code running. The readback in step 2 is what catches this if you
   forget. (readback.py reporting the file ABSENT is also how you confirm an
   uninstall actually removed it.)

4. You probably do not need to power-cycle after a FRESH install (a filename
   the pedal has not seen before). On my MS-70CDR+ (firmware 1.20) a freshly
   uploaded effect loaded and ran correctly with no reboot at all. That is a
   single observation, of one effect, on the new-filename path only, so do not
   lean on it: check by ear, and power-cycle if anything looks off. It changes
   nothing about step 3. Replacing a same-named file still needs the uninstall
   first, and whether THAT re-registers without a reboot has not been tested.

5. Audition the effect in a scratch patch (current temporary patch, 
   not saved to patch memory), and DO NOT SAVE. Saving is the brick
   path (see the risk model), so keep the experiment in the unsaved patch only.

6. Uninstall your experimental effects at the end of the session. The effect
   browser LIVE-PREVIEWS every installed effect you scroll past, so an installed
   effect is running code even when no patch uses it. Only leave effects on the
   pedal that you trust completely.

## Your kernel must never emit garbage

This one is not hypothetical: it cost a few dead audio engines to learn, and
it bites even a plain gain effect.

The pedal instantiates and runs an effect the moment you scroll onto it in the
effect browser (the live preview in point 6 above), so a freshly instantiated
effect runs BEFORE its parameters and coefficients are necessarily real. Pass those uninitialised
values straight through and you put an infinity or a NaN (not-a-number) on the
audio bus; the firmware's float state then latches silent until you
power-cycle. So your kernel does two things, without exception:

* It fails closed. Until it has confirmed its own state is initialised, it
  writes silence rather than computing with garbage coefficients or forwarding
  whatever the transitional bus happens to hold. That is what the guard
  prologue at the top of the kernel is for: no init, no processing, just
  silence.
* It clamps every sample it writes. Then a bad computation still cannot put an
  infinity or a NaN on the bus; the worst it can produce is a clamped, finite
  sample.

Your kernel processes the effect bus and nothing else. Other buffers exist on
the pedal, and some of them are live firmware state, but a gain effect does not
touch them. Their rules come with the more complex effects that do, and this
page does not cover them. How the kernel is called (the calling convention
the guard prologue sits inside) is in [docs/zd2-abi.md](docs/zd2-abi.md).

## If the pedal misbehaves

1. Stop. Do not retry the last command, and do not power-cycle by reflex.
2. Write down the exact state: what you sent, what the display shows, and
   whether USB-MIDI still responds.
3. If the pedal is still running: uninstall the experimental effect, confirm the
   current patch is stock-only, and only then power-cycle.
4. If the pedal is frozen: because autosave is off and you never saved the
   experiment, a power-cycle is safe by design. The pedal boots back to the
   last saved patch, which is stock. Do step 2 first anyway.
5. If you broke the rules and a crashing effect might actually
   be in a SAVED patch, DO NOT POWER-CYCLE. While the pedal is still up, remove
   the effect from that patch, delete the file, and save a clean, stock-only
   patch over the slot. Power-cycling in that state is the brick trigger.
6. If a FILE TRANSFER (install, uninstall, or backup) hung or was killed
   partway - the pedal may be wedged inside an open file-transfer session with
   its storage mid-write - DO NOT POWER-CYCLE. This is the second brick path
   above, and it is first-hand confirmed (2026-07-17). While the pedal is still
   powered the session can be closed cleanly and the effect list checked; a cold
   boot commits the half-written state. Run `tools-pedal/pedal_diy.py rescue`
   (it sends the file-session close and verifies the effect list) before you do
   anything else, and only reboot once the effect list reads back intact.

On ANY unexpected reply, stop and work out what happened before you send another
command. Do not keep poking to see what the pedal does next. A wrong reflex
after a partial failure is how a recoverable state becomes a brick. When in
doubt, leave the pedal on and think.

Please pass on the information about what happened and your theory about why
it happened to me, either as a github issue at
https://github.com/thammer/stomphacks/issues,
or by email to h@mmer.no

## SysEx you must never send

From the MS Plus SysEx notes at https://github.com/thammer/zoom-explorer and
zoom-zt2 issue 17:

| Message | What it does |
|---|---|
| 5B | Factory reset. Wipes all user patches. |
| 64 20 00 64 09 with values > 0x0A | Slot move/swap out of range. Freezes the pedal. |
| 64 05 | Effect cycling. Crashes the pedal unless the current patch is empty. |
| 60 xx | The file access API. Show caution. |

Stick to the exact sequences the tools already implement. Do not experiment
blindly with longer or unknown messages. That is how people brick pedals.

## Useful links

* [README.md](README.md) - what this project is and where to start
* [tools-pedal/README.md](tools-pedal/README.md) - the scripts that talk to the pedal
* [docs/effect-ids.md](docs/effect-ids.md) - picking a DIY effect ID that will not collide
* [docs/zd2-abi.md](docs/zd2-abi.md) - how your kernel is called
* [docs/ip-and-licensing.md](docs/ip-and-licensing.md) - what you may and may not share
* https://github.com/mungewell/zoom-zt2/issues - the brick reports, and the DIY effect thread (issue 93)
* https://github.com/thammer/zoom-explorer - the SysEx reference
