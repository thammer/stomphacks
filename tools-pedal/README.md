# tools-pedal

Everything in this folder talks to an MS Plus pedal over USB-MIDI. It is the
counterpart to `tools/`, which never does.

The line that keeps the pedal alive is drawn per operation, not per folder.
Read-only operations (finding the pedal, backing it up, reading its state) are
routine and low risk. Every operation that WRITES to the pedal (installs,
uninstalls, patch writes, deletes, anything that changes what is stored on it)
gets deliberate, command-by-command attention: you look at it and confirm it,
every single time. That confirm-every-mutation habit is the anti-bricking gate.
Do not automate past it.

## Rules for scripts in this folder

1. Read [../SAFETY.md](../SAFETY.md) first. Its never-send SysEx list is binding.
2. Only known-good command sequences. Reuse the exact SysEx that `zoomzt2.py`
   implements, or that is documented at https://github.com/thammer/zoom-explorer.
   Do not experiment with undocumented commands unless that experiment is itself
   the planned task and you know what you are doing.
3. One action per script run. A script does a single well-defined thing, and
   prints exactly what it sent and received, so a session can be reconstructed
   afterwards.
4. Fail closed. On any unexpected response: stop, print the state, exit. No
   retries, no fallbacks.

## The scripts

* `safe_connect.py <output-dir>` - the session opener. Run it first, every
  session. It connects, records the pedal and firmware identity, sends the
  autosave-OFF command and refuses to continue unless the pedal's own ACK
  confirms autosave is actually off, then downloads the current patch and prints
  the effects it references so you can check it is stock-only. Autosave OFF is the
  invariant the whole anti-bricking model rests on (SAFETY.md), which is why this
  tool will not proceed until the pedal confirms it.
* `backup_pedal.py <output-dir>` - a full backup: every effect file, every patch
  memory slot, `FLST_SEQ.ZT2`. Run it before your first write to any pedal. It is
  resumable, so a run you had to kill just restarts and skips what it already
  downloaded.
* `pedal_diy.py install|uninstall <FILE.ZD2>` - installs or uninstalls one of
  YOUR OWN DIY effects, and nothing else. It is fail-closed: it checks the
  effect's ID, name, filename and icon against a built-in catalog of every
  stock effect on the MS Plus series (`stock_catalog.py`) and refuses anything
  that looks stock or unknown, refuses anything that is not clearly a DIY
  effect, and never writes a patch or a memory slot, so it cannot put a DIY
  effect into the boot patch (one of the two brick paths - SAFETY.md). It
  closes the other one (an interrupted transfer) too: the whole transfer runs
  over a single connection this one process owns, every pedal reply is
  timeout-bounded, any failure cleanly closes the file session before exit, the
  new effect list is validated on the PC (`../tools/flst_check.py`) before any
  write, and every uploaded file is read back and byte-compared. Every upload
  (install, writetest) also requires the file's container envelope to pass the
  invariants every stock effect satisfies: the constant 120 at header offset 4,
  a valid checksum, the MS Plus target bit, sections that tile the file
  exactly to the trailer, the constant-zero header bytes, a group byte that
  matches the effect ID, and a printable name and version. An install also
  requires the companion icon to pass a structural check (the pedal parses
  the icon too). A file that fails any of them is refused before any
  pedal contact; uninstall is exempt, so a bad file can always be removed.
  Before a real
  install or uninstall it forces autosave OFF and requires the pedal's ACK. It
  always uploads the icon with the effect. Add `--dry-run` to run every check
  without touching the pedal, or `classify` to see its decision on a file and
  nothing more. Exit codes: 0 done; 3 = failed but pedal verified SAFE (nothing
  changed); 4 = failed and unverified (a DO-NOT-POWER-CYCLE banner is printed).
* `pedal_diy.py writetest <FILE.ZD2>` - the effect-list-free write exercise:
  uploads the DIY binary, reads it back byte-compared, deletes it and verifies
  it gone. The effect list is NEVER computed or written - it is read back
  before and after only to prove it unchanged, and a name the list references
  is refused (deleting a listed file would strand its entry). Run this as the
  FIRST hardware exercise after any change to the transfer tools: it validates
  every write primitive an install uses while the one file the pedal's boot
  depends on stays out of reach by construction. Same fail-closed checks and
  exit codes as install.
* `pedal_diy.py rescue [GOOD_FLST_SEQ.ZT2]` - run this if a transfer failed and
  told you to. It closes any open file session (unwedging a mid-transfer pedal)
  and reads the effect list back to check it is intact. If you pass a known-good
  `FLST_SEQ.ZT2` (e.g. from your backup) and add `--write-back`, it rewrites the
  effect list from it through the same guarded, retrying path. Keep the pedal
  POWERED while you do this; a reboot is what commits a half-written state.
* `file_session.py` - the transfer engine `pedal_diy.py` runs on: a
  timeout-guarded, self-aborting implementation of the pedal's SysEx file API.
  Not something you run directly. It exists because a pedal was lost on
  2026-07-17 to an install that hung with no timeout and no clean abort; every
  receive here is bounded and every failure closes the session cleanly. It
  never opens a MIDI port itself (the caller owns the connection).
* `mock_pedal.py` - an in-process fake pedal that speaks the file-API SysEx and
  can inject faults (go dead mid-transfer, drop a reply, report errors, corrupt
  data). Used only by the selftest; never touches MIDI.
* `install_selftest.py` - runs the real engine and the real installer against
  the mock through every failure mode. **Run it after ANY change to
  `file_session.py`, `mock_pedal.py` or `pedal_diy.py`** - the failure behaviour
  of the transfer path must be proven here, off-hardware, before a real pedal
  sees it. `.venv/Scripts/python.exe tools-pedal/install_selftest.py`; exit 0 =
  PASS. It opens no port and is safe to run anywhere. It also writes no line
  into `pedal_diy.log`: the suite redirects the operation log to a throwaway
  file and its last check proves the real one was untouched, so that log only
  ever records what happened to a real pedal. Three scenarios drive
  the real installer with your built gain effect; before your first build
  they SKIP and the verdict line says so - build the gain effect and re-run
  for the full suite.
* `stock_catalog.py` - the generated stock-identity catalog `pedal_diy.py`
  checks against: ids, names, filenames and icon names for the whole MS Plus
  series. Catalog facts only, no effect content. Not something you run.
* `pedal_common.py` - shared safety helpers for the pedal scripts, not something
  you run to do a task. It carries the autosave-off pre-flight guard: send the
  autosave-off command, read the pedal's ACK, and fail closed unless the pedal
  itself confirms autosave is off. Run `python tools-pedal/pedal_common.py
  --selftest` to exercise its decision logic without a pedal attached.
* `midi_ports.py [--probe]` - lists the MIDI ports and, with `--probe`, opens and
  immediately closes the pedal's port to check that nothing else is holding it. It
  never sends a single byte. Handy for "why won't the pedal connect?": on
  Windows a browser tab with WebMIDI holds the port exclusively, and this tells
  that apart from the pedal simply being unplugged.
* `readback.py <name-on-pedal> <out-path>` - downloads one file off the pedal to
  a path you choose, and refuses to overwrite an existing file so a stale readback
  can never be mistaken for a fresh one. Strictly read-only. This is how you do
  the byte-for-byte readback compare after an install (SAFETY.md), and how you
  confirm an uninstall: the file reads back absent.
