#!/usr/bin/env python3
"""A reusable pre-flight guard: confirm the pedal has autosave OFF.

`assert_autosave_off()` sends the autosave-OFF command and reads the pedal's
ACK, which echoes the value the pedal actually applied, so it CONFIRMS autosave
is off rather than assuming the command worked. Any script you write that talks
to the pedal can call it first and fail closed if autosave is still on.

Why it matters - the brick chain autosave-OFF closes:
Autosave OFF is the premise the whole anti-bricking model rests on. With
autosave ON, the pedal saves the current patch to a memory slot a few seconds
after a parameter change (zoom-explorer README, message `45 00 00`), that slot
is the BOOT patch, and if an experimental effect in it crashes the DSP the pedal
crashes on EVERY boot. That is the boot-crash loop SAFETY.md calls the brick
scenario, and it is presumed unrecoverable. So autosave-OFF must be VERIFIED,
not assumed.

HOW: read the pedal's ACK, which echoes the value it ACTUALLY APPLIED (slot
0x64, param 0x0F). The 0x64 SYSTEM commands ACK UNCONDITIONALLY (unlike
effect-parameter edits, which are ACKed only on a CHANGE), so a fresh
confirmation is available even when autosave was already off.

WARNING: THE VALUE BYTE IS A HARDCODED 0x00 AND MUST STAY THAT WAY. It is never
taken from user input, so this helper can only ever turn autosave OFF, never
ON. Turning it ON is precisely the brick path above.
"""

import sys
import time

import mido

# F0 52 00 6E 64 20 00 64 0F <00=off> 00 00 00 00 F7
#                               ^^^^ HARDCODED OFF. Never parameterise this.
AUTOSAVE_OFF = [0x52, 0x00, 0x6E, 0x64, 0x20, 0x00, 0x64, 0x0F,
                0x00, 0x00, 0x00, 0x00, 0x00]
EDIT_ENABLE = [0x52, 0x00, 0x6E, 0x50]
EDIT_DISABLE = [0x52, 0x00, 0x6E, 0x51]


def editor_off(pedal):
    """Leave editor mode (assert_autosave_off turns it on and leaves it on,
    because a caller usually keeps working; call this when it does not)."""
    pedal.outport.send(mido.Message("sysex", data=EDIT_DISABLE))
    time.sleep(0.05)


def parse_autosave_ack(b):
    """Pure decoder, split out so the FAIL-CLOSED branches are TESTABLE without
    hardware: exercising the 'autosave = ON' path on a real pedal would mean
    ENABLING autosave, i.e. creating the very hazard the guard exists to prevent.

    Returns the autosave value the pedal reports having APPLIED (0 = off,
    1 = on), or None if `b` is not a valid autosave ACK.
    Wire shape: 52 00 6E | 64 20 01 | 64 0F | <LSB> <MSB> ...
    """
    if (len(b) >= 10 and b[3:6] == b"\x64\x20\x01"
            and b[6] == 0x64 and b[7] == 0x0F):
        return b[8] | (b[9] << 7)
    return None


def assert_autosave_off(pedal, timeout=2.0):
    """Force autosave OFF and REQUIRE the pedal to confirm it. Exits (2) if it
    cannot be confirmed: fail-closed, because the alternative is the brick path.
    Returns silently on success."""
    pedal.outport.send(mido.Message("sysex", data=EDIT_ENABLE))
    time.sleep(0.05)
    while pedal.inport.poll() is not None:
        pass

    pedal.outport.send(mido.Message("sysex", data=AUTOSAVE_OFF))
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = pedal.inport.poll()
        if msg is not None:
            applied = parse_autosave_ack(bytes(msg.data))
            if applied is not None:
                if applied == 0:
                    return          # confirmed OFF by the pedal itself
                pedal.disconnect()
                sys.exit("REFUSED: the pedal reports AUTOSAVE = %d (ON). "
                         "Refusing to touch the temp patch.\n"
                         "  With autosave ON, loading a DIY effect into the "
                         "current patch gets SAVED to a memory slot within "
                         "seconds. That slot is the BOOT patch, and a broken "
                         "effect there crashes the pedal on every boot (the "
                         "brick scenario, SAFETY.md).\n"
                         "  Turn autosave OFF on the pedal menu and retry."
                         % applied)
        time.sleep(0.005)

    pedal.disconnect()
    sys.exit("REFUSED: could not CONFIRM autosave is OFF (no valid ACK within "
             "%.0fs).\n"
             "  Autosave-OFF is the premise that makes temp-patch "
             "writes non-persistent and therefore brick-safe (SAFETY.md). "
             "Without confirmation this tool will not touch the pedal.\n"
             "  Verify autosave OFF on the pedal's menu, then retry." % timeout)


def _selftest():
    """Prove the guard's decision logic, incl. the branches that cannot be
    exercised on hardware. Run: python tools-pedal/pedal_common.py --selftest"""
    real_off = bytes([0x52, 0x00, 0x6E, 0x64, 0x20, 0x01, 0x64, 0x0F,
                      0x00, 0x00, 0x00, 0x00, 0x00])   # captured from the pedal
    autosave_on = bytes([0x52, 0x00, 0x6E, 0x64, 0x20, 0x01, 0x64, 0x0F,
                         0x01, 0x00, 0x00, 0x00, 0x00])
    cases = [
        ("real ACK captured from the pedal (autosave 0)", real_off, 0),
        ("autosave ON  -> MUST be detected as 1 (refuse)", autosave_on, 1),
        ("a knob ACK, not autosave (slot 0, param 2)",
         bytes([0x52, 0x00, 0x6E, 0x64, 0x20, 0x01, 0x00, 0x02,
                0x32, 0x00, 0x00, 0x00, 0x00]), None),
        ("a 64 12 patch dump, not an ACK",
         bytes([0x52, 0x00, 0x6E, 0x64, 0x12, 0x00, 0x50, 0x06, 0x00, 0x50]), None),
        ("truncated message", bytes([0x52, 0x00, 0x6E, 0x64, 0x20, 0x01]), None),
    ]
    ok = True
    for label, raw, want in cases:
        got = parse_autosave_ack(raw)
        good = got == want
        ok &= good
        print("  %-46s -> %-4s expected %-4s [%s]"
              % (label, got, want, "OK" if good else "FAIL"))
    print()
    print("selftest: %s" % ("PASS - the guard accepts ONLY a pedal-confirmed "
                            "autosave=0; an autosave=ON ack and every "
                            "non-ack are refused" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_selftest() if "--selftest" in sys.argv else
             print(__doc__) or 0)
