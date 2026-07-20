#!/usr/bin/env python3
"""Full MS Plus backup: download every file and patch memory off the pedal.

Downloads, into <output-dir>:
  - every file on the pedal (effects .ZD2/.ZIC/.ZIR, FLST_SEQ.ZT2, ...)
    into <output-dir>/files/
  - every patch memory slot as PATCH_NNN.ZPTC
  - disk usage figure into disk-usage.txt
  - the raw wire log of the enumeration walk into walk-wire.log

Read-only towards the pedal apart from the pcmode enter/leave handshake.
This can take many minutes (150+ files over 512-byte SysEx blocks).

COMPLETENESS GATE: the wildcard directory listing can end early. It
happened on my pedal: a walk once returned only the first six entries (the
system files), reported end-of-listing right before the first effect file,
and a plain re-run listed everything. The cause is unknown and transient,
so the protection cannot depend on knowing it: after the walk, this tool
downloads FLST_SEQ.ZT2 (the pedal's own effect list) and requires every
registered effect's .ZD2 and .ZIC to appear in the walk result. A short
walk is retried once after a file-session close (the zoom-zt2 GUI closes
around its walk the same way); if it is still short, the tool FAILS LOUDLY
and never prints "Backup complete." A backup cannot look complete without
being complete. One known exception: BPM_MDL.ZD2 is registered in the
effect list but built into the pedal with no file of its own
(docs/effect-ids.md), so its absence from the walk is expected. Its .ZIC
must still be present.

Resumable: files that already exist (non-empty) in the output dir are
skipped, so a killed run can simply be restarted.

IMPORTANT: leave the pedal alone while this runs. The transfer library
blocks forever on a lost connection, so rebooting or unplugging the pedal
hangs the backup (kill it and restart; resume makes that cheap).

Usage:
  .venv/bin/python3 tools-pedal/backup_pedal.py <output-dir>
  .venv/bin/python3 tools-pedal/backup_pedal.py --selftest   # gate logic only,
                                                             # never touches a pedal

Pedal-touching: read-only apart from the pcmode enter/leave handshake.
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "zoom-zt2"))
sys.path.insert(0, str(ROOT / "tools"))

import zoomzt2  # noqa: E402

# Registered in the effect list but built into the pedal: no .ZD2 file
# exists for it on the MS-70CDR+ or MS-50G+ (docs/effect-ids.md). Its .ZIC
# file does exist and is still required. If a firmware update ever adds
# another built-in entry, the gate fails loudly on it and this tuple gets
# extended deliberately.
RESIDENT_NO_FILE = ("BPM_MDL.ZD2",)

WALK_ATTEMPTS = 2


def walk_is_complete(names, flst_data):
    """The completeness gate, as a pure function.

    names: the filenames the wildcard walk returned.
    flst_data: raw FLST_SEQ.ZT2 bytes downloaded from the pedal.
    Returns (ok:bool, missing:list[str], n_entries:int). Fail-closed: an
    effect list that does not validate returns ok=False with the problem
    text.
    """
    import flst_check

    ok, problems, entries = flst_check.validate_flst(flst_data)
    if not ok:
        return False, ["the effect list itself does not validate: "
                       + "; ".join(problems)], 0

    have = {n.upper() for n in names}
    missing = []
    if "FLST_SEQ.ZT2" not in have:
        missing.append("FLST_SEQ.ZT2")
    for _group, fname, _ver, _eid, _inst in entries:
        fname = fname.upper()
        stem = fname[:-4] if fname.endswith(".ZD2") else fname
        if fname not in have and fname not in RESIDENT_NO_FILE:
            missing.append(fname)
        zic = stem + ".ZIC"
        if zic not in have:
            missing.append(zic)
    return not missing, missing, len(entries)


class _LogPort:
    """Transparent wrapper around a mido port that appends hex lines to a
    log file. Changes nothing on the wire; used only around the walk so a
    future truncation leaves its terminating reply's bytes on disk."""

    def __init__(self, port, logfile, tag):
        self._port = port
        self._logfile = logfile
        self._tag = tag

    def _log(self, direction, msg):
        try:
            with open(self._logfile, "a") as f:
                f.write("%s %s %s\n" % (self._tag, direction,
                                        bytes(msg.data).hex()))
        except OSError:
            pass  # logging must never break the walk

    def send(self, msg):
        self._log("TX", msg)
        return self._port.send(msg)

    def receive(self):
        msg = self._port.receive()
        self._log("RX", msg)
        return msg

    def __getattr__(self, name):
        return getattr(self._port, name)


def enumerate_files(pedal, outdir, files_dir):
    """The gated enumeration: hygiene close, wire-logged walk, effect-list
    download, completeness gate, one retry. Returns the verified name
    list, or exits loudly without ever claiming success."""
    wire_log = outdir / "walk-wire.log"
    for attempt in range(1, WALK_ATTEMPTS + 1):
        # Close any file session before and after the walk. Harmless when
        # nothing is open, and the zoom-zt2 GUI does the same around its
        # own walk.
        pedal.file_close()

        print(f"Enumerating pedal files (attempt {attempt}) ...", flush=True)
        with open(wire_log, "a") as f:
            f.write(f"# walk attempt {attempt}\n")
        real_in, real_out = pedal.inport, pedal.outport
        pedal.inport = _LogPort(real_in, wire_log, "walk")
        pedal.outport = _LogPort(real_out, wire_log, "walk")
        try:
            names = []
            name = pedal.file_wild(True)
            while name:
                names.append(name)
                name = pedal.file_wild(False)
        finally:
            pedal.inport, pedal.outport = real_in, real_out

        pedal.file_close()
        print(f"{len(names)} files in the walk.", flush=True)

        flst_dest = files_dir / "FLST_SEQ.ZT2"
        if flst_dest.exists() and flst_dest.stat().st_size > 0:
            flst_data = flst_dest.read_bytes()
        else:
            flst_data = zoomzt2.download_and_save_file(
                pedal, "FLST_SEQ.ZT2", str(flst_dest))
        if not flst_data:
            sys.exit("FAIL: could not download FLST_SEQ.ZT2, so the "
                     "enumeration cannot be verified. Refusing to write a "
                     "backup that may be incomplete.")

        ok, missing, n_entries = walk_is_complete(names, flst_data)
        if ok:
            print(f"Enumeration verified against the effect list "
                  f"({n_entries} registered effects).", flush=True)
            return names
        shown = ", ".join(missing[:10])
        more = "" if len(missing) <= 10 else f" (+{len(missing) - 10} more)"
        print(f"Walk is INCOMPLETE: {len(missing)} registered file(s) "
              f"missing: {shown}{more}", flush=True)
        if attempt < WALK_ATTEMPTS:
            print("Retrying the walk once after a session close ...",
                  flush=True)
            time.sleep(0.5)

    sys.exit("FAIL: enumeration still incomplete after %d attempts. "
             "BACKUP INCOMPLETE. Do NOT use this directory as a recovery "
             "baseline. The walk's raw replies are in %s; the terminating "
             "reply's bytes are the diagnosis." % (WALK_ATTEMPTS, wire_log))


def selftest():
    """Prove the gate logic against a synthesized effect list. Pedal-free:
    nothing here opens a MIDI port."""
    entries = [("DELAY.ZD2", "1.00", 0x08000010),
               ("CHORUS.ZD2", "1.00", 0x06000020),
               ("BPM_MDL.ZD2", "1.00", 0x07000FF0)]
    groups = {}
    for name, ver, eid in entries:
        groups.setdefault((eid >> 24) & 0xFF, []).append(
            dict(effect=name, version=ver, id=eid, installed=1))
    flst = zoomzt2.ZT2.build(
        [dict(name="FLST_SEQ"),
         [dict(group=g, groupname=g, effects=effs)
          for g, effs in sorted(groups.items())]])

    # What a complete walk returns for that list: both files per effect,
    # the list itself, and no BPM_MDL.ZD2 (built into the pedal, no file).
    names = ["FLST_SEQ.ZT2", "DELAY.ZD2", "DELAY.ZIC",
             "CHORUS.ZD2", "CHORUS.ZIC", "BPM_MDL.ZIC"]
    failures = []

    ok, missing, n = walk_is_complete(names, flst)
    print(f"complete walk ({len(names)} names, {n} entries): "
          f"{'PASS' if ok else 'FAIL ' + str(missing)}")
    if not ok:
        failures.append("a complete walk must pass")

    ok, missing, _ = walk_is_complete(
        [n2 for n2 in names if n2 != "DELAY.ZD2"], flst)
    good = (not ok) and ("DELAY.ZD2" in missing)
    print(f"missing effect file detected: {'PASS' if good else 'FAIL'}")
    if not good:
        failures.append("a missing .ZD2 must fail the gate")

    ok, missing, _ = walk_is_complete(
        [n2 for n2 in names if n2 != "BPM_MDL.ZIC"], flst)
    good = (not ok) and ("BPM_MDL.ZIC" in missing)
    print(f"missing resident icon detected: {'PASS' if good else 'FAIL'}")
    if not good:
        failures.append("a missing .ZIC must fail the gate")

    ok, missing, _ = walk_is_complete(
        [n2 for n2 in names if n2 != "FLST_SEQ.ZT2"], flst)
    good = (not ok) and ("FLST_SEQ.ZT2" in missing)
    print(f"missing effect list in walk detected: {'PASS' if good else 'FAIL'}")
    if not good:
        failures.append("a walk without FLST_SEQ.ZT2 must fail the gate")

    # The truncation that motivated the gate: a walk that returns only a
    # head fragment must fail loudly, never look complete.
    ok, missing, _ = walk_is_complete(["FLST_SEQ.ZT2"], flst)
    good = (not ok) and (len(missing) >= 5)
    print(f"a truncated walk fails loudly: "
          f"{'PASS' if good else 'FAIL'} ({len(missing)} missing)")
    if not good:
        failures.append("a truncated walk must fail the gate")

    if failures:
        print("selftest: FAIL - " + "; ".join(failures))
        return 1
    print("selftest: PASS")
    return 0


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--selftest":
        return selftest()
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    outdir = Path(sys.argv[1])
    files_dir = outdir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    pedal = zoomzt2.zoomzt2()
    if not pedal.connect():
        sys.exit("FAIL: pedal not found on USB-MIDI.")
    pedal.pcmode_on()

    usage = pedal.disk_usage()
    print(f"Disk usage: {usage:.1f}%", flush=True)
    (outdir / "disk-usage.txt").write_text(f"disk_usage_percent: {usage:.1f}\n")

    names = enumerate_files(pedal, outdir, files_dir)

    for i, name in enumerate(names, 1):
        dest = files_dir / name
        if dest.exists() and dest.stat().st_size > 0:
            print(f"  [{i}/{len(names)}] {name}: already downloaded, skipping",
                  flush=True)
            continue
        print(f"  [{i}/{len(names)}] {name}: downloading ...", flush=True)
        zoomzt2.download_and_save_file(pedal, name, str(dest))

    (count, psize, bsize) = pedal.patch_check()
    print(f"Patch memories: count={count} patch_size={psize} bank_size={bsize}",
          flush=True)
    for loc in range(1, count + 1):
        out = outdir / f"PATCH_{loc:03d}.ZPTC"
        if out.exists() and out.stat().st_size > 0:
            print(f"  slot {loc:3d}: already downloaded, skipping", flush=True)
            continue
        data = pedal.patch_download(loc)
        if not data:
            print(f"  slot {loc:3d}: EMPTY/no data", flush=True)
            continue
        out.write_bytes(data)
        print(f"  slot {loc:3d}: {len(data)} bytes -> {out.name}", flush=True)

    pedal.disconnect()
    print("Backup complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
