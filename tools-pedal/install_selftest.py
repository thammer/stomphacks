#!/usr/bin/env python3
"""install_selftest.py - prove the transfer engine's FAILURE paths, off-pedal.

Runs the real engine (`file_session.py`) against the fake pedal
(`mock_pedal.py`) through the happy paths AND every failure mode that can be
injected: a pedal that goes dead mid-transfer, one that keeps working but
stops replying, lost single replies, error statuses, corrupted data - at
every phase, including the critical effect-list rewrite.

THE RULE THIS FILE ENFORCES (SAFETY.md "An interrupted file transfer can
brick"): no change to the transfer tools may reach a real pedal until this
selftest passes. A pedal was destroyed on 2026-07-17 because the failure
behaviour of the install path had only ever been "tested" by failing on real
hardware. Run it after ANY edit to file_session.py, mock_pedal.py or
pedal_diy.py:

    .venv/bin/python3 tools-pedal/install_selftest.py     (Windows:
    .venv/Scripts/python.exe tools-pedal/install_selftest.py)

It never opens a MIDI port and is safe to run anywhere. Exit 0 = PASS.

What "pass" means, per scenario, is spelled out in each function, but the
contract under test is always the same three-way outcome:
  - success        => everything verified, effect list correct, session closed;
  - TransferAborted => the failure was unwound: session closed, partial files
                      removed, effect list VERIFIED at the expected baseline;
  - PedalUnresolved => the engine could not verify safety, and then it MUST
                      have (a) still sent the close sequence, and (b) printed
                      the DO-NOT-POWER-CYCLE banner. Never a hang, never a
                      silent exit, never a false "success".
"""

import io
import os
import sys
import glob
import random
import tempfile
import zlib
import contextlib

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_ROOT, "tools"))
sys.path.insert(0, os.path.join(_ROOT, "zoom-zt2"))

import file_session as fs
import flst_check
import mock_pedal
import zoomzt2

GAIN_ID = 0x07000F0A
ZD2_BYTES = bytes(range(256)) * 32 + b"tail-of-the-effect"   # 8210 B, 17 blocks
ZIC_BYTES = b"\x89ICONDATA" + bytes(reversed(range(256))) * 3


def make_flst(entries):
    """Build a valid effect list from scratch with the zoomzt2 grammar."""
    groups = {}
    for name, ver, eid in entries:
        groups.setdefault((eid >> 24) & 0xFF, []).append(
            dict(effect=name, version=ver, id=eid, installed=1))
    cfg = [dict(name="FLST_SEQ"),
           [dict(group=g, groupname=g, effects=effs)
            for g, effs in sorted(groups.items())]]
    return zoomzt2.ZT2.build(cfg)


# Includes a group-7 (SFX) entry so that installing GAIN (id 0x07000f0a,
# group 7) lands in an EXISTING group and uninstalling returns the list to
# byte-identical - which is how it behaves on a real pedal, where all six
# groups always exist. A baseline lacking group 7 would leave an (harmless,
# still-valid) empty group behind after uninstall.
FLST_OLD = make_flst([("DELAY.ZD2", "1.00", 0x08000010),
                      ("CHORUS.ZD2", "1.00", 0x06000020),
                      ("STOCKSFX.ZD2", "1.00", 0x07000001)])
FLST_NEW = zoomzt2.zoomzt2().add_effect(FLST_OLD, "GAIN.ZD2", "1.00", GAIN_ID)

# The native-size twins. A pedal this tooling has never written holds a
# LARGER, zero-tailed effect list (a factory-fresh MS-70CDR+ ships 12324
# bytes; the grammar builds 8502). The engine must accept that list and
# every write must preserve its native length - never truncate it to 8502.
FACTORY_SIZE = 12324
FLST_OLD_NATIVE = flst_check.pad_to_native(FLST_OLD, FACTORY_SIZE)
FLST_NEW_NATIVE = flst_check.pad_to_native(FLST_NEW, FACTORY_SIZE)

FAILED = []
SKIPPED = []


def check(label, cond, detail=""):
    tag = "OK  " if cond else "FAIL"
    print("  %s %s%s" % (tag, label, (" - " + detail) if detail else ""))
    if not cond:
        FAILED.append(label)


def isolate_pedal_diy_log():
    """Point pedal_diy's operation log at a throwaway file for this run.

    This suite drives the REAL pedal_diy functions, and every install /
    uninstall / writetest appends a line to tools-pedal/pedal_diy.log.
    That file is the record of what was done to a REAL pedal - the
    2026-07-18 incident timeline is read out of it - so a mock run must
    never write into it: a line reading "install ... DONE" from a desk
    session is indistinguishable from a real pedal operation, in the one
    file that is supposed to say what actually touched hardware.

    Returns (real_path, fingerprint) so main() can PROVE the real log was
    left alone."""
    import pedal_diy
    real = pedal_diy.LOGFILE
    fp = ((os.path.getsize(real), os.path.getmtime(real))
          if os.path.exists(real) else None)
    pedal_diy.LOGFILE = os.path.join(
        tempfile.mkdtemp(prefix="selftest-log"), "pedal_diy.log")
    return real, fp


def log_fingerprint(path):
    return ((os.path.getsize(path), os.path.getmtime(path))
            if os.path.exists(path) else None)


def skip(label):
    """Record a scenario that cannot run on this checkout. A skip is not a
    failure, but the verdict line reports the count so a pass that did not
    run everything can never read as a full pass."""
    print("  SKIP %s" % label)
    SKIPPED.append(label)


class Run:
    """One engine run against one fresh mock, with captured log+stdout."""

    def __init__(self, fault=None, files=None, disk_available=None):
        base = {fs.FLST_NAME: FLST_OLD, "STOCK1.ZD2": b"\x01" * 100}
        if files:
            base.update(files)
        self.mock = mock_pedal.MockPedal(files=base)
        if disk_available is not None:
            self.mock.disk_available = disk_available
        self.mock.fault = fault
        self.lines = []
        self.sess = fs.FileSession(self.mock, log=self.lines.append)

    def __call__(self, op, *args, **kwargs):
        """Run op; return 'success' | 'aborted' | 'unresolved'."""
        self.result = None
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                self.result = op(self.sess, *args, log=self.lines.append,
                                 **kwargs)
            self.outcome = "success"
        except fs.TransferAborted as e:
            self.outcome = "aborted"
            self.message = str(e)
        except fs.PedalUnresolved as e:
            self.outcome = "unresolved"
            self.message = str(e)
        self.stdout = out.getvalue()
        return self.outcome

    def banner_shown(self):
        return "DO NOT POWER-CYCLE" in self.stdout

    def close_was_sent(self):
        """Did the engine send the file-close sequence (60 21 ... 60 09),
        even to a dead pedal?"""
        subs = [p[4] for p in self.mock.rx_log
                if len(p) > 4 and p[3] == 0x60]
        return fs.SUB_CLOSE in subs and fs.SUB_SESSION_END in subs

    def abort_close_sent_after_wedge(self):
        """The stronger check: AFTER the pedal wedged, did the engine still
        transmit BOTH close bytes (60 21 and 60 09)? A close from an earlier
        successful phase must not satisfy this."""
        subs = [p[4] for p in self.mock.rx_after_fault
                if len(p) > 4 and p[3] == 0x60]
        return fs.SUB_CLOSE in subs and fs.SUB_SESSION_END in subs

    def flst_is(self, expect):
        return self.mock.files.get(fs.FLST_NAME) == expect

    def boot_safe(self):
        """Would a reboot of the mock 'pedal' meet a sane filesystem?"""
        if self.mock.wedged():
            return False
        flst = self.mock.files.get(fs.FLST_NAME)
        if flst is None:
            return False
        return flst_check.validate_flst(flst)[0]


INSTALL_FILES = [("GAIN.ZIC", ZIC_BYTES), ("GAIN.ZD2", ZD2_BYTES)]


def s1_codecs():
    print("S1: codec equivalence with zoomzt2 (pack/unpack/CRC)")
    z = zoomzt2.zoomzt2()
    rng = random.Random(20260717)
    blobs = [b"", b"\x00", b"\xff" * 7, bytes(range(8)),
             rng.randbytes(511), rng.randbytes(512), rng.randbytes(513)]
    blobs += [rng.randbytes(rng.randint(1, 2000)) for _ in range(40)]
    ok_pack = all(bytes(fs.pack7(b)) == bytes(z.pack(b)) for b in blobs)
    ok_unpack = all(bytes(fs.unpack7(fs.pack7(b))) == b for b in blobs)
    ok_cross = all(bytes(z.unpack(fs.pack7(b))) == b for b in blobs)
    check("pack7 byte-equals zoomzt2.pack on %d blobs" % len(blobs), ok_pack)
    check("unpack7(pack7(x)) == x", ok_unpack)
    check("zoomzt2.unpack(pack7(x)) == x", ok_cross)
    import binascii
    b = rng.randbytes(512)
    check("CRC tail round-trip",
          (fs.tail_to_int(fs.crc32_tail(b)) ^ 0xFFFFFFFF)
          == binascii.crc32(b))
    check("test FLSTs are valid (old)", flst_check.validate_flst(FLST_OLD)[0])
    check("test FLSTs are valid (new)", flst_check.validate_flst(FLST_NEW)[0])


def s2_happy_install():
    print("S2: happy-path install")
    r = Run()
    outcome = r(fs.install_effect, INSTALL_FILES, FLST_OLD, FLST_NEW)
    check("outcome is success", outcome == "success")
    check("no effect-list retries were needed",
          r.result == {"flst_retries": 0})
    check("ZD2 stored byte-exact", r.mock.files.get("GAIN.ZD2") == ZD2_BYTES)
    check("ZIC stored byte-exact", r.mock.files.get("GAIN.ZIC") == ZIC_BYTES)
    check("effect list is the new one", r.flst_is(FLST_NEW))
    check("a reboot would be safe", r.boot_safe())


def s3_already_present():
    print("S3: refusal when the file is already on the pedal "
          "(no silent-skip installs, no half-written pairs)")
    r = Run(files={"GAIN.ZD2": b"OLD CODE"})
    outcome = r(fs.install_effect, INSTALL_FILES, FLST_OLD, FLST_NEW)
    check("outcome is a clean abort", outcome == "aborted")
    check("old binary untouched", r.mock.files.get("GAIN.ZD2") == b"OLD CODE")
    check("icon was NOT uploaded first", "GAIN.ZIC" not in r.mock.files)
    check("effect list untouched", r.flst_is(FLST_OLD))
    check("a reboot would be safe", r.boot_safe())


def s4_single_lost_reply():
    print("S4: ONE lost reply mid-upload (recovered by the status poll)")
    r = Run(fault=mock_pedal.Fault("drop", sub=fs.SUB_WRITE_BLOCK,
                                   name="GAIN.ZD2", block=3))
    outcome = r(fs.install_effect, INSTALL_FILES, FLST_OLD, FLST_NEW)
    check("outcome is success", outcome == "success")
    check("ZD2 stored byte-exact", r.mock.files.get("GAIN.ZD2") == ZD2_BYTES)
    check("effect list is the new one", r.flst_is(FLST_NEW))
    check("a reboot would be safe", r.boot_safe())


def s5_dead_mid_upload():
    print("S5: pedal goes DEAD mid-ZD2-upload (the 2026-07-17 wedge)")
    r = Run(fault=mock_pedal.Fault("dead", sub=fs.SUB_WRITE_BLOCK,
                                   name="GAIN.ZD2", block=5))
    outcome = r(fs.install_effect, INSTALL_FILES, FLST_OLD, FLST_NEW)
    check("outcome is unresolved (engine cannot verify a dead pedal)",
          outcome == "unresolved")
    check("the abort sent BOTH close bytes AFTER the wedge",
          r.abort_close_sent_after_wedge())
    check("DO-NOT-POWER-CYCLE banner printed", r.banner_shown())
    check("effect list bytes were never touched", r.flst_is(FLST_OLD))
    check("the run terminated (no hang) - implicit", True)


def s6_mute_mid_upload():
    print("S6: pedal keeps working but stops REPLYING mid-upload")
    r = Run(fault=mock_pedal.Fault("mute", sub=fs.SUB_WRITE_BLOCK,
                                   name="GAIN.ZD2", block=5))
    outcome = r(fs.install_effect, INSTALL_FILES, FLST_OLD, FLST_NEW)
    check("outcome is unresolved (no ack = cannot verify)",
          outcome == "unresolved")
    check("DO-NOT-POWER-CYCLE banner printed", r.banner_shown())
    check("but the close it sent DID land (file no longer open)",
          not r.mock.wedged())
    check("effect list bytes were never touched", r.flst_is(FLST_OLD))


def s7_dead_in_flst_window():
    print("S7: pedal goes DEAD inside the effect-list rewrite window "
          "(the residual worst case)")
    r = Run(fault=mock_pedal.Fault("dead", sub=fs.SUB_WRITE_BLOCK,
                                   name=fs.FLST_NAME, block=2))
    outcome = r(fs.install_effect, INSTALL_FILES, FLST_OLD, FLST_NEW)
    check("outcome is unresolved", outcome == "unresolved")
    check("DO-NOT-POWER-CYCLE banner printed", r.banner_shown())
    check("the abort sent BOTH close bytes AFTER the wedge",
          r.abort_close_sent_after_wedge())
    check("mock records the danger honestly (truncated effect list)",
          not r.boot_safe())


def s8_flst_retry_recovers():
    print("S8: transient bad write of the effect list; the READBACK gate "
          "(not a status code) catches it and the retry recovers")
    r = Run(fault=mock_pedal.Fault("corrupt_stored", name=fs.FLST_NAME,
                                   times=1))
    outcome = r(fs.install_effect, INSTALL_FILES, FLST_OLD, FLST_NEW)
    check("outcome is success", outcome == "success")
    check("exactly one retry was used", r.result == {"flst_retries": 1})
    check("effect list is the new one, verified", r.flst_is(FLST_NEW))
    check("a reboot would be safe", r.boot_safe())


def s9_storage_corruption():
    print("S9: silent storage corruption caught by the readback-verify")
    r = Run(fault=mock_pedal.Fault("corrupt_stored", name="GAIN.ZD2"))
    outcome = r(fs.install_effect, INSTALL_FILES, FLST_OLD, FLST_NEW)
    check("outcome is a clean abort", outcome == "aborted")
    check("the corrupt file was removed", "GAIN.ZD2" not in r.mock.files)
    check("the already-verified icon was cleaned up too",
          "GAIN.ZIC" not in r.mock.files)
    check("effect list untouched", r.flst_is(FLST_OLD))
    check("a reboot would be safe", r.boot_safe())


def s10_download_crc_error():
    print("S10: CRC error in the readback (download must FAIL, not "
          "silently drop the block as zoomzt2 does)")
    r = Run(fault=mock_pedal.Fault("corrupt_read", name="GAIN.ZIC"))
    outcome = r(fs.install_effect, INSTALL_FILES, FLST_OLD, FLST_NEW)
    check("outcome is a clean abort", outcome == "aborted")
    check("partial install cleaned up", "GAIN.ZIC" not in r.mock.files)
    check("effect list untouched", r.flst_is(FLST_OLD))
    check("a reboot would be safe", r.boot_safe())


def s11_disk_full():
    print("S11: not enough free space - refused BEFORE the window")
    r = Run(disk_available=1000)
    outcome = r(fs.install_effect, INSTALL_FILES, FLST_OLD, FLST_NEW)
    check("outcome is a clean abort", outcome == "aborted")
    check("uploaded files were cleaned up again",
          "GAIN.ZD2" not in r.mock.files and "GAIN.ZIC" not in r.mock.files)
    check("effect list untouched", r.flst_is(FLST_OLD))
    check("a reboot would be safe", r.boot_safe())


def s12_happy_uninstall():
    print("S12: happy-path uninstall (list rewritten FIRST, then files)")
    r = Run(files={"GAIN.ZD2": ZD2_BYTES, "GAIN.ZIC": ZIC_BYTES,
                   fs.FLST_NAME: FLST_NEW})
    outcome = r(fs.uninstall_effect, ["GAIN.ZD2", "GAIN.ZIC"],
                FLST_NEW, FLST_OLD)
    check("outcome is success", outcome == "success")
    check("files removed", "GAIN.ZD2" not in r.mock.files
          and "GAIN.ZIC" not in r.mock.files)
    check("effect list is the old one again", r.flst_is(FLST_OLD))
    check("a reboot would be safe", r.boot_safe())


def s13_uninstall_delete_fails():
    print("S13: pedal dies during the file deletes of an uninstall "
          "(AFTER the list rewrite committed)")
    r = Run(files={"GAIN.ZD2": ZD2_BYTES, "GAIN.ZIC": ZIC_BYTES,
                   fs.FLST_NAME: FLST_NEW},
            fault=mock_pedal.Fault("dead", sub=fs.SUB_DELETE,
                                   name="GAIN.ZD2"))
    outcome = r(fs.uninstall_effect, ["GAIN.ZD2", "GAIN.ZIC"],
                FLST_NEW, FLST_OLD)
    check("outcome is unresolved", outcome == "unresolved")
    check("DO-NOT-POWER-CYCLE banner printed", r.banner_shown())
    check("the effect list was ALREADY consistent (old list committed)",
          r.flst_is(FLST_OLD))
    check("orphan file remains (benign) - documented behaviour",
          "GAIN.ZD2" in r.mock.files)


def s14_flst_window_never_entered_lightly():
    print("S14: the effect-list window is guarded - a concurrent rewrite "
          "is detected before the window opens")
    other = zoomzt2.zoomzt2().add_effect(FLST_OLD, "OTHER.ZD2", "1.00",
                                         0x07000F20)
    r = Run(files={fs.FLST_NAME: other})   # pedal list != the computed input
    outcome = r(fs.install_effect, INSTALL_FILES, FLST_OLD, FLST_NEW)
    check("outcome is a clean abort", outcome == "aborted")
    check("pedal's (different) effect list untouched", r.flst_is(other))
    check("uploaded files cleaned up", "GAIN.ZD2" not in r.mock.files)
    check("a reboot would be safe", r.boot_safe())


def s15_no_change_no_window():
    print("S15: identical effect list -> the window is never entered")
    r = Run()
    outcome = r(fs.install_effect, INSTALL_FILES, FLST_OLD, FLST_OLD)
    check("outcome is success", outcome == "success")
    writes_to_flst = [p for p in r.mock.rx_log
                      if len(p) > 5 and p[3] == 0x60
                      and p[4] in (fs.SUB_DELETE, fs.SUB_OPEN)
                      and b"FLST_SEQ" in bytes(p)]
    check("not a single write/delete ever addressed the effect list",
          not writes_to_flst)


def s16_pedal_diy_wrapper_end_to_end():
    print("S16: pedal_diy._execute install+uninstall end to end, driving the "
          "REAL wrapper (offline FLST validation + dispatch) against the mock, "
          "with the REAL DIYGAIN.ZD2/ZIC build artifacts")
    import pedal_diy
    zd2_path = os.path.join(_ROOT, "effects", "gain", "build", "DIYGAIN.ZD2")
    zic_path = os.path.join(_ROOT, "effects", "gain", "build", "DIYGAIN.ZIC")
    if not (os.path.exists(zd2_path) and os.path.exists(zic_path)):
        skip("no DIYGAIN build artifacts on this checkout - build the gain "
             "effect first to run this scenario")
        return
    with open(zd2_path, "rb") as f:
        gain_zd2 = f.read()
    with open(zic_path, "rb") as f:
        gain_zic = f.read()
    eid, gname = pedal_diy.read_zd2(zd2_path)

    # a pedal whose effect list does NOT yet contain DIYGAIN
    flst_before = FLST_OLD
    grammar = zoomzt2.zoomzt2()   # never connects; grammar helpers only
    mock = mock_pedal.MockPedal(files={fs.FLST_NAME: flst_before})
    sess = fs.FileSession(mock)
    sess.pcmode_on()

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = pedal_diy._execute(grammar, sess, "install", zd2_path, eid, gname)
    check("install returns EXIT_OK", rc == pedal_diy.EXIT_OK, "rc=%d" % rc)
    check("DIYGAIN.ZD2 byte-exact on the mock",
          mock.files.get("DIYGAIN.ZD2") == gain_zd2)
    check("DIYGAIN.ZIC byte-exact on the mock",
          mock.files.get("DIYGAIN.ZIC") == gain_zic)
    flst_installed = mock.files.get(fs.FLST_NAME)
    ok, _, _ = flst_check.validate_flst(flst_installed)
    check("effect list valid after install", ok)
    added, _ = flst_check.diff_entries(flst_before, flst_installed)
    check("exactly the DIYGAIN entry was added",
          len(added) == 1 and added[0][1] == "DIYGAIN.ZD2")

    # now uninstall it back off the same mock
    sess.pcmode_on()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = pedal_diy._execute(grammar, sess, "uninstall", zd2_path, eid, gname)
    check("uninstall returns EXIT_OK", rc == pedal_diy.EXIT_OK, "rc=%d" % rc)
    check("DIYGAIN.ZD2 removed from the mock", "DIYGAIN.ZD2" not in mock.files)
    check("DIYGAIN.ZIC removed from the mock", "DIYGAIN.ZIC" not in mock.files)
    check("effect list is byte-identical to the pre-install baseline",
          mock.files.get(fs.FLST_NAME) == flst_before)


def s17_nonzero_write_status_tolerated():
    print("S17: a nonzero WRITE status is TOLERATED, not aborted (matches "
          "zoomzt2, which discards it) - the readback is the gate. A benign "
          "nonzero status must NOT trigger a false abort / false brick-scare.")
    r = Run(fault=mock_pedal.Fault("error_status", name="GAIN.ZD2", times=99))
    outcome = r(fs.install_effect, INSTALL_FILES, FLST_OLD, FLST_NEW)
    check("outcome is success despite nonzero status on every ZD2 block",
          outcome == "success")
    check("ZD2 stored byte-exact (the status was benign)",
          r.mock.files.get("GAIN.ZD2") == ZD2_BYTES)
    check("effect list is the new one", r.flst_is(FLST_NEW))
    check("a reboot would be safe", r.boot_safe())


def s18_native_size_flst():
    print("S18: factory-native 12324-byte effect list - validated as native "
          "size, refused on real tail data, and install->uninstall "
          "round-trips AT native size through the engine (no truncation)")
    ok, _, entries = flst_check.validate_flst(FLST_OLD_NATIVE)
    check("native-size list validates", ok)
    check("same entries as its 8502-byte canonical form",
          entries == flst_check.parse_entries(FLST_OLD))
    bad = bytearray(FLST_OLD_NATIVE)
    bad[10000] = 0x01
    check("a NONZERO byte past 8502 is refused (real data would be lost)",
          not flst_check.validate_flst(bytes(bad))[0])
    check("a shorter-than-8502 list is still refused",
          not flst_check.validate_flst(FLST_OLD[:8000])[0])
    try:
        flst_check.pad_to_native(FLST_NEW, 8000)
        check("pad_to_native refuses to truncate", False)
    except ValueError:
        check("pad_to_native refuses to truncate", True)

    r = Run(files={fs.FLST_NAME: FLST_OLD_NATIVE})
    outcome = r(fs.install_effect, INSTALL_FILES,
                FLST_OLD_NATIVE, FLST_NEW_NATIVE)
    check("install at native size succeeds", outcome == "success")
    check("effect list stays %d bytes with the new entry" % FACTORY_SIZE,
          r.flst_is(FLST_NEW_NATIVE))
    check("a reboot would be safe", r.boot_safe())
    outcome = r(fs.uninstall_effect, ["GAIN.ZD2", "GAIN.ZIC"],
                FLST_NEW_NATIVE, FLST_OLD_NATIVE)
    check("uninstall at native size succeeds", outcome == "success")
    check("effect list byte-identical to the native baseline (still %d B)"
          % FACTORY_SIZE, r.flst_is(FLST_OLD_NATIVE))
    check("a reboot would be safe", r.boot_safe())


def s19_pedal_diy_native_size():
    print("S19: pedal_diy._execute against a factory-native 12324-byte "
          "effect list - the REAL wrapper (download, offline compute, "
          "pad_to_native) preserves the native size end to end")
    import pedal_diy
    zd2_path = os.path.join(_ROOT, "effects", "gain", "build", "DIYGAIN.ZD2")
    zic_path = os.path.join(_ROOT, "effects", "gain", "build", "DIYGAIN.ZIC")
    if not (os.path.exists(zd2_path) and os.path.exists(zic_path)):
        skip("no DIYGAIN build artifacts on this checkout - build the gain "
             "effect first to run this scenario")
        return
    eid, gname = pedal_diy.read_zd2(zd2_path)

    flst_native = flst_check.pad_to_native(FLST_OLD, FACTORY_SIZE)
    grammar = zoomzt2.zoomzt2()   # never connects; grammar helpers only
    mock = mock_pedal.MockPedal(files={fs.FLST_NAME: flst_native})
    sess = fs.FileSession(mock)
    sess.pcmode_on()

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = pedal_diy._execute(grammar, sess, "install", zd2_path, eid, gname)
    check("install returns EXIT_OK", rc == pedal_diy.EXIT_OK, "rc=%d" % rc)
    flst_installed = mock.files.get(fs.FLST_NAME)
    check("effect list KEPT its native %d bytes" % FACTORY_SIZE,
          len(flst_installed or b"") == FACTORY_SIZE)
    check("effect list valid after install",
          flst_check.validate_flst(flst_installed)[0])
    added, removed = flst_check.diff_entries(flst_native, flst_installed)
    check("exactly the DIYGAIN entry was added",
          len(added) == 1 and not removed and added[0][1] == "DIYGAIN.ZD2")

    sess.pcmode_on()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = pedal_diy._execute(grammar, sess, "uninstall", zd2_path, eid,
                                gname)
    check("uninstall returns EXIT_OK", rc == pedal_diy.EXIT_OK, "rc=%d" % rc)
    check("effect list byte-identical to the native baseline",
          mock.files.get(fs.FLST_NAME) == flst_native)


def _flst_writes(mock):
    """Every transmitted message that could MUTATE the effect list: a
    delete/replace of its name, or an open-FOR-WRITE of its name (the
    read-only open, mode 0x02, is legitimate and excluded)."""
    return [p for p in mock.rx_log
            if len(p) > 5 and p[3] == 0x60
            and (p[4] == fs.SUB_DELETE
                 or (p[4] == fs.SUB_OPEN and p[5] == 0x01))
            and b"FLST_SEQ" in bytes(p)]


def s20_writetest_happy():
    print("S20: effect-list-free write test (writetest_effect) - upload, "
          "readback-verify, delete, verify absent; the effect list is "
          "NEVER addressed by a mutating message")
    r = Run()
    outcome = r(fs.writetest_effect, "GAIN.ZD2", ZD2_BYTES, FLST_OLD)
    check("outcome is success", outcome == "success")
    check("file is GONE at the end", "GAIN.ZD2" not in r.mock.files)
    check("effect list byte-identical throughout", r.flst_is(FLST_OLD))
    check("no delete/open-for-write ever addressed the effect list",
          not _flst_writes(r.mock))
    check("a reboot would be safe", r.boot_safe())


def s21_writetest_failures():
    print("S21: write-test failure paths - already-present refusal, dead "
          "mid-upload, corrupt storage; the effect list survives them all")
    r = Run(files={"GAIN.ZD2": b"OLD CODE"})
    outcome = r(fs.writetest_effect, "GAIN.ZD2", ZD2_BYTES, FLST_OLD)
    check("already-present -> clean abort, nothing written",
          outcome == "aborted")
    check("existing file untouched",
          r.mock.files.get("GAIN.ZD2") == b"OLD CODE")
    check("effect list untouched", r.flst_is(FLST_OLD))

    r = Run(fault=mock_pedal.Fault("dead", sub=fs.SUB_WRITE_BLOCK,
                                   name="GAIN.ZD2", block=5))
    outcome = r(fs.writetest_effect, "GAIN.ZD2", ZD2_BYTES, FLST_OLD)
    check("dead mid-upload -> unresolved", outcome == "unresolved")
    check("DO-NOT-POWER-CYCLE banner printed", r.banner_shown())
    check("the abort sent BOTH close bytes AFTER the wedge",
          r.abort_close_sent_after_wedge())
    check("effect list bytes were never touched", r.flst_is(FLST_OLD))
    check("no delete/open-for-write ever addressed the effect list",
          not _flst_writes(r.mock))

    r = Run(fault=mock_pedal.Fault("corrupt_stored", name="GAIN.ZD2"))
    outcome = r(fs.writetest_effect, "GAIN.ZD2", ZD2_BYTES, FLST_OLD)
    check("corrupt storage -> clean abort (the readback gate)",
          outcome == "aborted")
    check("partial file removed", "GAIN.ZD2" not in r.mock.files)
    check("effect list untouched", r.flst_is(FLST_OLD))
    check("a reboot would be safe", r.boot_safe())


def s22_pedal_diy_writetest():
    print("S22: pedal_diy._writetest end to end against the mock - the real "
          "wrapper's happy path (on a native-size list) and its "
          "listed-name refusal")
    import pedal_diy
    zd2_path = os.path.join(_ROOT, "effects", "gain", "build", "DIYGAIN.ZD2")
    if not os.path.exists(zd2_path):
        skip("no DIYGAIN build artifact on this checkout - build the gain "
             "effect first to run this scenario")
        return
    eid, gname = pedal_diy.read_zd2(zd2_path)

    flst_native = flst_check.pad_to_native(FLST_OLD, FACTORY_SIZE)
    mock = mock_pedal.MockPedal(files={fs.FLST_NAME: flst_native})
    sess = fs.FileSession(mock)
    sess.pcmode_on()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = pedal_diy._writetest(sess, zd2_path, eid, gname)
    check("writetest returns EXIT_OK", rc == pedal_diy.EXIT_OK, "rc=%d" % rc)
    check("file absent at the end", "DIYGAIN.ZD2" not in mock.files)
    check("effect list byte-identical (native size, untouched)",
          mock.files.get(fs.FLST_NAME) == flst_native)
    check("no delete/open-for-write ever addressed the effect list",
          not _flst_writes(mock))

    # refusal: a name the effect list REFERENCES must be refused before
    # any write (deleting its file would strand the entry)
    flst_ref = zoomzt2.zoomzt2().add_effect(FLST_OLD, "DIYGAIN.ZD2", "1.00",
                                            eid)
    mock2 = mock_pedal.MockPedal(files={fs.FLST_NAME: flst_ref})
    sess2 = fs.FileSession(mock2)
    sess2.pcmode_on()
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(out):
            pedal_diy._writetest(sess2, zd2_path, eid, gname)
        check("listed name refused before any write", False,
              "writetest did not refuse")
    except SystemExit as e:
        check("listed name refused before any write",
              e.code == pedal_diy.EXIT_REFUSED, "exit=%r" % (e.code,))
    check("nothing was written to the refusing mock",
          "DIYGAIN.ZD2" not in mock2.files
          and mock2.files.get(fs.FLST_NAME) == flst_ref
          and not mock2.wedged())


def s23_rescue_recovers_a_wedged_session():
    print("S23: rescue recovers a pedal WEDGED mid-file-session (pedal #1, "
          "2026-07-18) - it sends the close sequence even though PC-mode "
          "gets no reply, and the effect list reads back VALID")
    import pedal_diy

    # First prove the gap that the old pcmode-first rescue died on: a wedged
    # pedal does not answer PC-mode on, so pcmode_on() times out.
    w0 = mock_pedal.MockPedal(files={fs.FLST_NAME: FLST_OLD}, wedged=True)
    sess0 = fs.FileSession(w0, recv_timeout=0.05)
    try:
        sess0.pcmode_on()
        check("PC-mode on a wedged pedal times out (the gap)", False,
              "pcmode_on unexpectedly returned")
    except fs.TransferTimeout:
        check("PC-mode on a wedged pedal times out (the gap)", True)

    # Now the real fixed rescue core against a fresh wedged mock: it must
    # tolerate the PC-mode silence, send the close sequence, unwedge the
    # pedal, and read the effect list back VALID.
    mock = mock_pedal.MockPedal(files={fs.FLST_NAME: FLST_OLD}, wedged=True)
    sess = fs.FileSession(mock, recv_timeout=0.05)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = pedal_diy._rescue_session(sess, None, None, None, False)
    check("rescue returns EXIT_OK", rc == pedal_diy.EXIT_OK, "rc=%d" % rc)
    check("the pedal is no longer wedged", not mock._wedged)
    close_seq = [p for p in mock.rx_log if len(p) > 4 and p[3] == 0x60
                 and p[4] in (fs.SUB_CLOSE, fs.SUB_SESSION_END)]
    check("the close sequence was sent", len(close_seq) >= 2)
    check("the effect list is untouched (rescue wrote nothing)",
          mock.files.get(fs.FLST_NAME) == FLST_OLD)


def s24_envelope_gate_refuses_bad_container():
    print("S24: the container-envelope gate refuses an out-of-invariant "
          "file BEFORE any pedal traffic (the @4 invariant the file in "
          "both 2026-07 pedal losses violated, plus a corrupt checksum)")
    import pedal_diy
    zd2_path = os.path.join(_ROOT, "effects", "gain", "build", "DIYGAIN.ZD2")
    if not os.path.exists(zd2_path):
        skip("no DIYGAIN build artifact on this checkout - build the gain "
             "effect first to run this scenario")
        return
    with open(zd2_path, "rb") as f:
        good = f.read()
    errs = pedal_diy.check_envelope(good)
    check("the real build artifact passes the envelope lint", errs == [],
          "; ".join(errs))

    # the loss configuration: @4 = a stale build size, with the checksum
    # made valid again so ONLY the @4 invariant fires
    bad4 = bytearray(good)
    bad4[4:8] = (1285).to_bytes(4, "little")
    bad4[8:12] = (zlib.crc32(bytes(bad4)[12:-16])
                  ^ 0xFFFFFFFF).to_bytes(4, "little")
    errs = pedal_diy.check_envelope(bytes(bad4))
    check("@4 != 120 is flagged (and only that)",
          len(errs) == 1 and "@4" in errs[0], repr(errs))

    badcrc = bytearray(good)
    badcrc[8] ^= 0xFF
    errs = pedal_diy.check_envelope(bytes(badcrc))
    check("a corrupt checksum is flagged",
          any("checksum" in e for e in errs), repr(errs))

    badtile = good[:-8]   # truncated: sections no longer tile to the trailer
    errs = pedal_diy.check_envelope(badtile)
    check("a truncated container is flagged", errs != [], repr(errs))

    # extended invariants: a group byte contradicting the id is flagged
    # (@95 is inside the CRC span, so recompute the checksum to isolate it)
    badgrp = bytearray(good)
    badgrp[95] ^= 0x01
    badgrp[8:12] = (zlib.crc32(bytes(badgrp)[12:-16])
                    ^ 0xFFFFFFFF).to_bytes(4, "little")
    errs = pedal_diy.check_envelope(bytes(badgrp))
    check("group byte / id mismatch is flagged (and only that)",
          len(errs) == 1 and "group byte" in errs[0], repr(errs))

    # the companion icon: the real build's ZIC passes, a truncated one is
    # flagged, and a broken icon refuses an install end to end
    zic_path = os.path.splitext(zd2_path)[0] + ".ZIC"
    with open(zic_path, "rb") as f:
        good_zic = f.read()
    zerrs = pedal_diy.check_zic(good_zic)
    check("the real build icon passes the ZIC lint", zerrs == [],
          "; ".join(zerrs))
    zerrs = pedal_diy.check_zic(good_zic[:-8])
    check("a truncated icon is flagged", zerrs != [], repr(zerrs))

    zdir = tempfile.mkdtemp(prefix="envgate")
    with open(os.path.join(zdir, "GAIN.ZD2"), "wb") as f:
        f.write(good)
    with open(os.path.join(zdir, "GAIN.ZIC"), "wb") as f:
        f.write(good_zic[:-8])
    mock = mock_pedal.MockPedal(files={fs.FLST_NAME: FLST_OLD})
    sess = fs.FileSession(mock)
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(out):
            pedal_diy._execute(zoomzt2.zoomzt2(), sess, "install",
                               os.path.join(zdir, "GAIN.ZD2"), GAIN_ID,
                               "Gain")
        check("install with a broken icon refused", False, "no refusal")
    except SystemExit as e:
        check("install with a broken icon refused",
              e.code == pedal_diy.EXIT_REFUSED, "exit=%r" % (e.code,))
    check("broken-icon install: the mock is untouched",
          "GAIN.ZD2" not in mock.files
          and mock.files.get(fs.FLST_NAME) == FLST_OLD
          and not mock.wedged())

    # end to end: writetest and install on the @4-bad file refuse with
    # EXIT_REFUSED before a single request reaches the mock
    bad_path = os.path.join(tempfile.mkdtemp(prefix="envgate"), "GAIN.ZD2")
    with open(bad_path, "wb") as f:
        f.write(bytes(bad4))
    for label, run in (
            ("writetest", lambda s: pedal_diy._writetest(
                s, bad_path, GAIN_ID, "Gain")),
            ("install", lambda s: pedal_diy._execute(
                zoomzt2.zoomzt2(), s, "install", bad_path, GAIN_ID, "Gain"))):
        mock = mock_pedal.MockPedal(files={fs.FLST_NAME: FLST_OLD})
        sess = fs.FileSession(mock)
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(out):
                run(sess)
            check("%s refused the bad envelope" % label, False, "no refusal")
        except SystemExit as e:
            check("%s refused the bad envelope" % label,
                  e.code == pedal_diy.EXIT_REFUSED, "exit=%r" % (e.code,))
        check("%s: the mock is untouched" % label,
              "GAIN.ZD2" not in mock.files
              and mock.files.get(fs.FLST_NAME) == FLST_OLD
              and not mock.wedged())


def s25_rescue_remove_orphan():
    print("S25: pedal_diy._rescue_session with --remove - cleans up an "
          "orphan DIY file the effect list does NOT reference, refuses one "
          "it DOES reference (that is uninstall's job), refuses a non-DIY "
          "target before any pedal traffic, and never claims safety if a "
          "delete does not verify")
    import pedal_diy
    zd2_path = os.path.join(_ROOT, "effects", "gain", "build", "DIYGAIN.ZD2")
    zic_path = os.path.join(_ROOT, "effects", "gain", "build", "DIYGAIN.ZIC")
    if not (os.path.exists(zd2_path) and os.path.exists(zic_path)):
        skip("no DIYGAIN build artifacts on this checkout - build the gain "
             "effect first to run this scenario")
        return
    eid, gname = pedal_diy.read_zd2(zd2_path)
    zbase = os.path.basename(zd2_path)
    zic_base = os.path.basename(zic_path)
    with open(zd2_path, "rb") as f:
        diy_zd2 = f.read()
    with open(zic_path, "rb") as f:
        diy_zic = f.read()

    diy_group = (eid >> 24) & 0xFF
    flst_without = make_flst([("DELAY.ZD2", "1.00", 0x08000010),
                             ("CHORUS.ZD2", "1.00", 0x06000020),
                             ("PLACEHLD.ZD2", "1.00", (diy_group << 24) | 0x01)])

    # --- orphan present, list does not reference it: gets removed ---
    mock = mock_pedal.MockPedal(files={fs.FLST_NAME: flst_without,
                                       zbase: diy_zd2, zic_base: diy_zic})
    sess = fs.FileSession(mock)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = pedal_diy._rescue_session(sess, None, None, [zic_base, zbase],
                                       False)
    check("orphan removal returns EXIT_OK", rc == pedal_diy.EXIT_OK, "rc=%d" % rc)
    check("orphan ZD2 removed from the mock", zbase not in mock.files)
    check("orphan ZIC removed from the mock", zic_base not in mock.files)
    check("effect list untouched by the removal",
          mock.files.get(fs.FLST_NAME) == flst_without)

    # --- registered (NOT an orphan): rescue succeeds but leaves it alone ---
    flst_with = zoomzt2.zoomzt2().add_effect(flst_without, zbase, "1.00", eid)
    mock2 = mock_pedal.MockPedal(files={fs.FLST_NAME: flst_with,
                                        zbase: diy_zd2, zic_base: diy_zic})
    sess2 = fs.FileSession(mock2)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = pedal_diy._rescue_session(sess2, None, None, [zic_base, zbase],
                                       False)
    check("registered-file rescue still returns EXIT_OK (the list itself "
          "is valid)", rc == pedal_diy.EXIT_OK, "rc=%d" % rc)
    check("registered ZD2 was NOT removed (not an orphan)", zbase in mock2.files)
    check("registered ZIC was NOT removed (not an orphan)",
          zic_base in mock2.files)
    check("effect list untouched", mock2.files.get(fs.FLST_NAME) == flst_with)

    # --- nothing to remove: reports cleanly, still safe ---
    mock3 = mock_pedal.MockPedal(files={fs.FLST_NAME: flst_without})
    sess3 = fs.FileSession(mock3)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = pedal_diy._rescue_session(sess3, None, None, [zic_base, zbase],
                                       False)
    check("nothing-to-remove rescue returns EXIT_OK",
          rc == pedal_diy.EXIT_OK, "rc=%d" % rc)
    check("nothing appeared that was not there before",
          mock3.files.get(fs.FLST_NAME) == flst_without
          and len(mock3.files) == 1)

    # --- a delete that does not verify: UNRESOLVED, never claims safe ---
    mock4 = mock_pedal.MockPedal(
        files={fs.FLST_NAME: flst_without, zbase: diy_zd2, zic_base: diy_zic})
    mock4.fault = mock_pedal.Fault("delete_ineffective", name=zbase)
    sess4 = fs.FileSession(mock4)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = pedal_diy._rescue_session(sess4, None, None, [zic_base, zbase],
                                       False)
    check("a delete that does not verify returns EXIT_UNRESOLVED, never "
          "claims the pedal is safe", rc == pedal_diy.EXIT_UNRESOLVED,
          "rc=%d" % rc)

    # --- refuses a non-DIY (stock) target up front, before any pedal traffic
    stock = sorted(glob.glob(os.path.join(_ROOT, "effects", "Zoom-MS-70CDR+",
                                          "*.ZD2")))
    if not stock:
        print("     (no stock corpus on this checkout - skipping the "
              "stock-refusal check)")
    else:
        allow, _, _, reasons = pedal_diy.classify(stock[0])
        check("sanity check: a stock file classifies as NOT removable",
              not allow, repr(reasons))
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(out):
                pedal_diy._rescue(["--remove", stock[0]])
            check("--remove refuses a stock target", False, "did not refuse")
        except SystemExit as e:
            check("--remove refuses a stock target before touching a "
                  "pedal (classify runs before _connect)",
                  e.code == pedal_diy.EXIT_REFUSED, "exit=%r" % (e.code,))


def main():
    print(__doc__.split("\n")[0])
    print()
    real_log, log_before = isolate_pedal_diy_log()
    print("operation log redirected to a throwaway file for this run; "
          "%s is left untouched\n" % os.path.basename(real_log))
    for scenario in (s1_codecs, s2_happy_install, s3_already_present,
                     s4_single_lost_reply, s5_dead_mid_upload,
                     s6_mute_mid_upload, s7_dead_in_flst_window,
                     s8_flst_retry_recovers, s9_storage_corruption,
                     s10_download_crc_error, s11_disk_full,
                     s12_happy_uninstall, s13_uninstall_delete_fails,
                     s14_flst_window_never_entered_lightly,
                     s15_no_change_no_window,
                     s16_pedal_diy_wrapper_end_to_end,
                     s17_nonzero_write_status_tolerated,
                     s18_native_size_flst, s19_pedal_diy_native_size,
                     s20_writetest_happy, s21_writetest_failures,
                     s22_pedal_diy_writetest,
                     s23_rescue_recovers_a_wedged_session,
                     s24_envelope_gate_refuses_bad_container,
                     s25_rescue_remove_orphan):
        scenario()
        print()

    print("S26: this suite left the real operation log alone")
    check("tools-pedal/pedal_diy.log unchanged by the whole run",
          log_fingerprint(real_log) == log_before,
          "mock runs must never appear in the record of what touched a "
          "real pedal")
    print()

    if FAILED:
        print("selftest: FAIL - %d check(s) failed:" % len(FAILED))
        for f in FAILED:
            print("  - %s" % f)
        return 1
    note = ""
    if SKIPPED:
        note = (" (%d scenario(s) SKIPPED - build the gain effect and "
                "re-run for the full suite)" % len(SKIPPED))
    print("selftest: PASS%s - every failure mode ends in a verified-safe "
          "abort or an explicit KEEP-POWERED banner; no hangs, no silent "
          "corruption, no false success." % note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
