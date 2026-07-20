# tools

Pedal-safe scripts only. The contract: nothing in this folder may communicate
with a pedal.

Allowed here:

* Reading and analysing local files (ZD2 and ZIC parsing, ELF inspection)
* Writing local output files (generated assembly and C, built containers, icons,
  reports)

Never here:

* MIDI I/O of any kind (mido, python-rtmidi, amidi, SysEx)
* Anything that uploads, downloads, deletes or modifies files or patches on a
  pedal
* Network access

Scripts that talk to the pedal live in `tools-pedal/`; see its README.

The point of the split: anything that can touch the pedal deserves your full
attention every single time, and everything else should not need it. This folder
is the safe side of that line. Keep every script in it there: local files only.

## The build tools

* `zd2_make_effect.py`: the build. Give it an effect folder's `manifest.json`
  and it compiles the C kernel with the TI C6000 compiler, generates the rest of
  the DSP blob to the documented ABI, assembles the ZD2 container and the ZIC
  icon, verifies the whole thing, and writes `build/REPORT.md`. This is the one
  command you run to turn source into an installable effect.
* `zd2_cleanroom.py`: generates the whole DSP scaffold (entry stub, init,
  parameter and function tables, coefficient table, one edit handler per knob)
  as fresh assembly written to the documented ABI, with no Zoom-authored
  code in it. That is what makes a built effect shareable.
  `zd2_make_effect.py` calls it; you do not run it directly.
* `zd2_from_scratch.py`: synthesizes the ZD2 container and the ZIC icon from
  scratch, so the build needs no stock effect file at all. The three undecoded
  container header fields ship zeroed (hardware-proven to load and run), and
  the rest of the header follows the stock corpus exactly: the constant 120 at
  offset 4 (it is not a file length) and target 0x0090. Its `verify_envelope()`
  asserts those invariants plus a valid checksum and a clean section layout on
  every finished build, and the installer refuses to upload a file that fails
  them, so a malformed container stops at your desk, not on the pedal.
  `zd2_make_effect.py` calls it; run it yourself for its `--selftest`, or
  `--recontainer` to zero the fields of an existing effect.
* `zd2_strip.py`: strips the debug and symbol dead weight (DWARF sections, the
  static symbol table) out of the DSP ELF object (Executable and Linkable Format)
  inside a container, keeps everything the loader needs, and verifies the result
  round-trips byte for byte.
* `zd2_roundtrip.py`: parses a ZD2 file and rebuilds it, then checks the rebuild
  is byte-identical to the original. A quick way to prove the container
  parse-and-repack path is lossless before you trust it with a real effect.
* `flst_check.py`: validates a pedal effect list (`FLST_SEQ.ZT2`) offline - it
  must be exactly 8502 bytes, parse with the effect-list grammar, round-trip
  byte-for-byte, and have every entry's id agree with its group. `FLST_SEQ.ZT2`
  is the file the pedal reads FIRST at boot, so a bad one can stop it booting
  (SAFETY.md "An interrupted file transfer can brick"); this is how the effect
  list an install is about to write is checked on the PC, before any byte
  reaches the pedal. `--simulate-add <FILE.ZD2>` / `--simulate-remove <NAME>`
  show the exact effect-list change an install/uninstall would make. The
  installer (`tools-pedal/pedal_diy.py`) calls its library entry points as a
  mandatory pre-flight. This is local-file analysis only - it never touches the
  pedal, so it lives here.
* `build_zd2_from_asm.sh`: the shared backend that assembles and links a TI
  C6000 assembly file and packs the result into a ZD2 container, then validates
  the CRC and round-trips it. `zd2_make_effect.py` calls it; you rarely run it
  by hand.
