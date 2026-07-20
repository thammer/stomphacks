#!/usr/bin/env python3
"""List the MIDI ports, and optionally test whether the pedal's port is FREE.

Strictly read-only: it enumerates ports and (with --probe) opens and
immediately closes them. It never sends a single byte: no SysEx, no identity
request, nothing.

It is handy for diagnosing "why won't the pedal connect?". On Windows, MIDI
ports are EXCLUSIVE: a browser tab holding WebMIDI (for example zoom-explorer)
owns the port, and a connect attempt dies with:
    _rtmidi.SystemError: MidiInWinMM::openPort: error creating Windows MM MIDI
                         input port.
Enumeration still LISTS the port (only open_port fails), so `--probe` is what
tells the two apart. Closing the tab is not enough; Chrome must be fully quit.

Usage (from the repo root):
    .venv/bin/python3        tools-pedal/midi_ports.py [--probe]
    .venv/Scripts/python.exe tools-pedal/midi_ports.py [--probe]   # Windows

Exit codes: 0 = ok (with --probe: the pedal's ports opened); 1 = the pedal's
port is present but LOCKED by another process; 2 = no pedal port found.
"""

import argparse
import sys

import mido

PREFIX = "ZOOM MS Plus Series"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--probe", action="store_true",
                    help="open and immediately close the pedal's ports to see "
                         "if another process holds them (sends NO data)")
    a = ap.parse_args()

    ins, outs = mido.get_input_names(), mido.get_output_names()
    print("MIDI inputs:")
    for p in ins:
        print("   %s%s" % (p, "   <-- pedal" if p.startswith(PREFIX) else ""))
    print("MIDI outputs:")
    for p in outs:
        print("   %s%s" % (p, "   <-- pedal" if p.startswith(PREFIX) else ""))

    pin = [p for p in ins if p.startswith(PREFIX)]
    pout = [p for p in outs if p.startswith(PREFIX)]
    if not (pin and pout):
        print()
        print("NO PEDAL PORT (looking for a port starting %r)." % PREFIX)
        print("Check the USB cable and that the pedal is powered on.")
        return 2

    if not a.probe:
        return 0

    print()
    locked = False
    for label, name, opener in (("input", pin[0], mido.open_input),
                                ("output", pout[0], mido.open_output)):
        try:
            opener(name).close()
            print("OK      %-7s %s" % (label, name))
        except Exception as e:
            locked = True
            print("LOCKED  %-7s %s\n        -> %s" % (label, name, e))

    if locked:
        print()
        print("The port is held by ANOTHER PROCESS. On Windows the usual culprit "
              "is a browser tab with WebMIDI (zoom-explorer). Closing the TAB is "
              "not enough. Quit the browser fully, then re-run this.")
        return 1
    print()
    print("Pedal ports are FREE - safe_connect.py should work.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
