#!/usr/bin/env python3
"""Read ONE file back off the pedal into a chosen output path. Read-only.

It takes an explicit destination and refuses to overwrite it, so a stale
readback from an earlier step can never masquerade as a fresh download: the
failure is loud instead of silent. Point each readback at a fresh path.

Strictly read-only towards the pedal: it calls file_check + file_download +
file_close (the same path as `zoomzt2.py -e`) and writes nothing to the pedal.
Keep it that way: any operation that writes to the pedal belongs in its own
command.

Usage (from the repo root):
    .venv/bin/python3      tools-pedal/readback.py NAME.ZD2 OUT/PATH.ZD2
    .venv/Scripts/python.exe tools-pedal/readback.py NAME.ZD2 OUT/PATH.ZD2   # Windows
    ... --force            overwrite an existing destination (opt-in)

Exit codes: 0 = downloaded; 1 = not found on the pedal (also the ABSENCE
check after an uninstall: an expected, useful result); 2 = usage/refusal.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "zoom-zt2"))

import zoomzt2  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Read one file off the pedal (read-only).")
    ap.add_argument("name", metavar="NAME", help="filename ON THE PEDAL, e.g. AWGAINCR.ZD2")
    ap.add_argument("out", metavar="OUT", help="local destination path")
    ap.add_argument("--force", action="store_true",
                    help="overwrite OUT if it already exists (default: refuse)")
    a = ap.parse_args()

    out = Path(a.out)
    if out.exists() and not a.force:
        # Exit 2, NOT 1: a refusal must be distinguishable from "absent on the
        # pedal" (exit 1), or a caller checking the status would read a stale
        # local file as proof the effect is gone.
        print("REFUSING to overwrite %s. Point at a fresh path (or pass "
              "--force). This guard is what stops a STALE readback being "
              "mistaken for a fresh download." % out, file=sys.stderr)
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)

    pedal = zoomzt2.zoomzt2()
    if not pedal.connect():
        sys.exit("FAIL: pedal not found on USB-MIDI. (On Windows, a browser tab "
                 "with WebMIDI holds the port exclusively; quit the browser "
                 "fully, then try again.)")
    pedal.pcmode_on()
    try:
        data = zoomzt2.download_and_save_file(pedal, a.name, str(out))
    finally:
        pedal.disconnect()          # disconnect() runs pcmode_off()

    if not data:
        # Not an error: this is exactly how an uninstall is confirmed.
        print('ABSENT: "%s" is not on the pedal.' % a.name)
        return 1

    print("%s -> %s (%d bytes)" % (a.name, out, len(data)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
