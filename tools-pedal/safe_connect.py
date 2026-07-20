#!/usr/bin/env python3
"""Session opener for the MS-70CDR+ (SAFETY.md invariant 1).

Does, in order:
  1. Connect to the pedal over USB-MIDI (port prefix "ZOOM MS Plus Series").
  2. MIDI Universal Identity Request -> records the reply (firmware version).
  3. Editor mode on -> send autosave OFF SysEx -> editor mode off.
  4. Download the current patch (edit buffer) and save it, printing the
     printable strings found in it so the effects in use can be reviewed.

Fail-closed: every expected reply has a timeout; on timeout the script
prints state and exits nonzero. It never retries.

Usage:
  .venv/bin/python3 tools-pedal/safe_connect.py <output-dir>

Writes <output-dir>/CURRENT.ZPTC and <output-dir>/identity.txt.
Pedal-touching: it reads identity and the current patch, and sets autosave OFF.
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "zoom-zt2"))

import binascii  # noqa: E402
import mido  # noqa: E402
import zoomzt2  # noqa: E402

# Autosave OFF: F0 52 00 6E 64 20 00 64 0F <00=off/01=on> 00 00 00 00 F7
# An earlier form (64 03 00 0A 0F ...) does NOT turn autosave off on the
# MS-70CDR+ (fw 1.20), hardware-verified: autosave stayed ON in the menu. The
# bytes above are the working command for the MS+ series (from the zoom-explorer
# SysEx table, "64 20 00 64 0F <auto-save>").
AUTOSAVE_OFF = [0x52, 0x00, 0x6E, 0x64, 0x20, 0x00, 0x64, 0x0F,
                0x00, 0x00, 0x00, 0x00, 0x00]
IDENTITY_REQUEST = [0x7E, 0x7F, 0x06, 0x01]
EDITOR_ON = [0x52, 0x00, 0x6E, 0x50]
EDITOR_OFF = [0x52, 0x00, 0x6E, 0x51]
CURRENT_PATCH_REQ = [0x52, 0x00, 0x6E, 0x64, 0x13]


def recv(inport, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = inport.poll()
        if msg is not None:
            return msg
        time.sleep(0.01)
    return None


def send_expect(pedal, data, what, timeout=3.0):
    pedal.outport.send(mido.Message("sysex", data=data))
    msg = recv(pedal.inport, timeout)
    if msg is None:
        sys.exit(f"FAIL-CLOSED: no reply to {what} within {timeout}s. "
                 "Stopping (SAFETY.md escalation rule).")
    return msg


def printable_strings(blob, minlen=4):
    out, run = [], []
    for b in blob:
        if 32 <= b < 127:
            run.append(chr(b))
        else:
            if len(run) >= minlen:
                out.append("".join(run))
            run = []
    if len(run) >= minlen:
        out.append("".join(run))
    return out


def main():
    if len(sys.argv) == 2 and sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    if len(sys.argv) != 2 or sys.argv[1].startswith("-"):
        # A dash-prefixed arg is a flag (or a typo), never an output dir:
        # without this guard, `safe_connect.py --help` connected to the
        # pedal for real and ran a full session-open into a directory
        # literally named "--help/". backup_pedal.py already guards
        # against the same mistake.
        print(__doc__)
        return 2
    outdir = Path(sys.argv[1])
    outdir.mkdir(parents=True, exist_ok=True)

    pedal = zoomzt2.zoomzt2()
    if not pedal.connect():
        sys.exit("FAIL: no MIDI port matching 'ZOOM MS Plus Series' / 'ZOOM G'. "
                 f"Ports seen: {mido.get_input_names()}")
    print("Connected. Input ports:", [p for p in mido.get_input_names()])

    # 1. Identity (safe, universal MIDI)
    msg = send_expect(pedal, IDENTITY_REQUEST, "identity request")
    ident = bytes(msg.data)
    ascii_tail = "".join(chr(b) for b in ident if 32 <= b < 127)
    print(f"Identity reply ({len(ident)} bytes): {ident.hex()}")
    print(f"  printable: {ascii_tail!r}")
    (outdir / "identity.txt").write_text(
        f"identity_reply_hex: {ident.hex()}\n"
        f"identity_printable: {ascii_tail}\n")

    # 2. Autosave OFF, and CONFIRM it from the pedal's own ACK.
    #
    # The pedal replies `64 20 01` (ack) echoing <slot> <param> <LSB> <MSB>, i.e.
    # the value it ACTUALLY APPLIED. For this command that is slot 0x64, param
    # 0x0F, value 0 => "autosave is 0 (OFF)", straight from the pedal. That is a
    # machine-checkable confirmation, not just "message received".
    #   (Unlike EFFECT-parameter edits, which are ACKed only when the value
    #   CHANGES, the 0x64 system commands ACK UNCONDITIONALLY, so a fresh ACK
    #   is available even when autosave was already off. That is exactly what
    #   makes it usable as a state check.)
    #   The old, WRONG autosave bytes got NO reply and did NOT work, so a valid
    #   ACK also proves the pedal understood the command at all.
    send_expect(pedal, EDITOR_ON, "editor-mode on")
    pedal.outport.send(mido.Message("sysex", data=AUTOSAVE_OFF))
    time.sleep(0.5)
    ack = recv(pedal.inport, timeout=2.0)

    autosave_confirmed = False
    if ack is not None:
        b = bytes(ack.data)
        # 52 00 6E | 64 20 01 | <slot> <param> <LSB> <MSB> ...
        if (len(b) >= 10 and b[3:6] == b"\x64\x20\x01"
                and b[6] == 0x64 and b[7] == 0x0F):
            applied = b[8] | (b[9] << 7)
            if applied == 0:
                autosave_confirmed = True
                # ASCII only: the Windows console is cp1252 and dies on
                # non-ASCII output.
                print("Autosave OFF - CONFIRMED by the pedal's own ACK "
                      f"(it reports autosave = {applied}). Reply: {b.hex()}")
            else:
                sys.exit("FAIL-CLOSED: the pedal ACKed autosave = "
                         f"{applied} (ON!). Refusing to continue. Autosave OFF "
                         "is the invariant that makes a crashed experiment "
                         "non-fatal (SAFETY.md).")
        else:
            print(f"Autosave OFF sent, but the reply was not the expected "
                  f"64 20 01 / 64 0F ack: {b.hex()}")
    if not autosave_confirmed:
        print("!! AUTOSAVE NOT CONFIRMED - no valid ACK. Do NOT proceed to any "
              "install/audition until you have VERIFIED AUTOSAVE IS OFF on the "
              "pedal's own menu (SAFETY.md invariant 1).")

    # 3. Current patch (edit buffer), same decode as zoomzt2.patch_download_current
    msg = send_expect(pedal, CURRENT_PATCH_REQ, "current-patch download")
    packet = msg.data
    length = int(packet[7]) * 128 + int(packet[6])
    if length == 0:
        print("WARNING: current patch reply had zero length, nothing saved.")
    else:
        data = pedal.unpack(packet[8:8 + length + int(length / 7) + 1])
        checksum = (packet[-5] + (packet[-4] << 7) + (packet[-3] << 14)
                    + (packet[-2] << 21) + ((packet[-1] & 0x0F) << 28))
        crc_ok = (checksum ^ 0xFFFFFFFF) == binascii.crc32(data)
        (outdir / "CURRENT.ZPTC").write_bytes(data)
        print(f"Current patch: {len(data)} bytes, CRC {'ok' if crc_ok else 'BAD'} "
              f"-> {outdir / 'CURRENT.ZPTC'}")
        print("Printable strings in current patch (review for non-stock effects):")
        for s in printable_strings(data):
            print("   ", s)

    send_expect(pedal, EDITOR_OFF, "editor-mode off")
    pedal.disconnect()
    print("Session opener complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
