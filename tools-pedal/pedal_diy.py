#!/usr/bin/env python3
"""pedal_diy.py - a safe, fail-closed installer for your own DIY effects.

Installs or uninstalls DIY effect binaries you built. It exists so the easy
way to install an effect is also the safe way.

The guarantees this script enforces:
  1. It only ever installs or uninstalls an effect BINARY and edits the
     on-pedal effect list. It never writes a patch or a memory slot, so it
     can never put a DIY effect into the boot patch (one of the two brick
     paths in SAFETY.md).
  2. The other brick path, the interrupted file transfer, is closed by
     construction. The whole job runs over ONE MIDI connection in this one
     process, and every transfer goes through file_session.py: every pedal
     receive has a timeout, and any failure triggers a clean file-session
     close before the tool exits, so a stall becomes a fast, self-unwinding
     error instead of a pedal abandoned mid-write.
  3. The new effect list is computed and VALIDATED offline before a single
     byte is written (tools/flst_check.py: it must parse, round-trip
     byte-for-byte, and differ from the current list by exactly the one
     intended entry). A malformed effect list is what the pedal's boot
     reads first, so it is caught on the PC, never on flash.
  4. Every uploaded file is read back and byte-compared before it counts.
  5. It acts only on effects whose id is in your own DIY id range AND whose
     id, name, filename and icon collide with no stock Zoom effect (a
     built-in catalog covers the whole MS Plus series). It refuses to
     uninstall a stock effect, or an effect of unknown origin.
  6. Before any real install or uninstall it forces autosave OFF and
     requires the pedal's own ACK (pedal_common.assert_autosave_off).
  7. Before any upload (install, writetest) the file's container ENVELOPE
     must satisfy the stock-corpus invariants (check_envelope): header @4
     = 120, valid checksum, the MS Plus target bit, sections that tile
     the file exactly to the trailer, the constant-zero header bytes, a
     group byte that matches the effect ID, and a printable name and
     version. An install additionally requires the companion icon to pass
     check_zic (the pedal parses the icon too). The file uploaded in both
     2026-07 pedal losses violated the first invariant; nothing out of
     invariant reaches a pedal's storage again. Uninstall is exempt (it
     deletes by name), so a bad file can always be removed.
  8. Fail-closed: any parse error, any check that does not clearly pass,
     any surprise, and it refuses without touching the pedal. If a transfer
     fails mid-way it either unwinds to a verified-safe state, or prints a
     loud DO-NOT-POWER-CYCLE banner and tells you to run `rescue`.

Every failure path of the transfer engine is proven off-hardware against a
mock pedal before any real pedal is touched: `install_selftest.py`.

Usage (from the repo root):
    .venv/bin/python3 tools-pedal/pedal_diy.py install    <FILE.ZD2> [--dry-run]
    .venv/bin/python3 tools-pedal/pedal_diy.py uninstall  <FILE.ZD2> [--dry-run]
    .venv/bin/python3 tools-pedal/pedal_diy.py writetest  <FILE.ZD2> [--dry-run]
    .venv/bin/python3 tools-pedal/pedal_diy.py classify    <FILE.ZD2>  # decision only, never touches the pedal
    .venv/bin/python3 tools-pedal/pedal_diy.py rescue      [GOOD_FLST_SEQ.ZT2] [--write-back] [--remove FILE.ZD2]
  Windows (Git Bash): .venv/Scripts/python.exe tools-pedal/pedal_diy.py ...

`--dry-run` runs every safety check and prints exactly what it would do,
without touching the pedal. `classify` is dry-run's read-only core.
`writetest` is the effect-list-free write exercise: it uploads the DIY
binary, reads it back byte-compared, deletes it and verifies it gone - the
effect list is NEVER computed or written, and a name the list references is
refused. Run it as the first hardware exercise after any change to the
transfer tools, so the write machinery is validated with the boot-critical
file untouched by construction. `rescue` closes a wedged file session and
verifies the effect list is intact (and, if you pass a known-good
FLST_SEQ.ZT2 and confirm with --write-back, rewrites it) - use it if a
transfer failed and told you to. `--remove FILE.ZD2` additionally removes
that effect (and its companion .ZIC), but only if it is an orphan the
failed transfer left behind - not registered in the effect list, which
reads back valid first. A file the list still references is left alone;
use `uninstall` for that. Refuses up front unless FILE.ZD2 classifies as
one of your own DIY effects. Every real run appends a line to
tools-pedal/pedal_diy.log.

Exit codes: 0 = done; 1 = classify refused; 2 = refused before touching the
pedal; 3 = transfer failed but the pedal was verified SAFE (nothing changed;
fix the cause and retry); 4 = transfer failed AND the pedal's state could not
be verified (the DO-NOT-POWER-CYCLE banner was printed - keep it powered and
run `rescue`).
"""

import datetime
import glob
import os
import struct
import sys
import time
import zlib

import stock_catalog

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "zoom-zt2"))
sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))

STOCK_GLOB = os.path.join(REPO_ROOT, "effects", "Zoom-*", "*.ZD2")
STOCK_ZIC_GLOB = os.path.join(REPO_ROOT, "effects", "Zoom-*", "*.ZIC")
LOGFILE = os.path.join(REPO_ROOT, "tools-pedal", "pedal_diy.log")

ID_OFF = 96            # uint32le effect id
NAME_OFF, NAME_LEN = 100, 11   # ASCII effect name, null-padded

# Exit codes (see the module docstring).
EXIT_OK = 0
EXIT_CLASSIFY_REFUSED = 1
EXIT_REFUSED = 2
EXIT_SAFE_ABORT = 3
EXIT_UNRESOLVED = 4


def die(msg, code=EXIT_REFUSED):
    sys.stderr.write("pedal_diy: REFUSED - %s\n" % msg)
    sys.exit(code)


def read_zd2(path):
    """Return (id:int, name:str) from a ZD2 file, or die (fail-closed)."""
    if not os.path.isfile(path):
        die("no such file: %s" % path)
    if os.path.splitext(path)[1].upper() != ".ZD2":
        die("not a .ZD2 file: %s" % path)
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < NAME_OFF + NAME_LEN or data[:4] != b"ZDLF":
        die("not a valid ZD2 container (bad magic/size): %s" % path)
    eid = struct.unpack_from("<I", data, ID_OFF)[0]
    raw = data[NAME_OFF:NAME_OFF + NAME_LEN]
    name = raw.split(b"\x00", 1)[0].decode("ascii", "replace")
    return eid, name


def check_envelope(data):
    """Container-envelope lint for a file about to be UPLOADED. Returns a
    list of violations; empty means clean.

    Every stock ZD2 across five pedal models satisfies these invariants,
    and so does every build this project's tools produce. The container
    that preceded both 2026-07 pedal losses violated the first one: its
    header word at offset 4 carried a stale build size where every stock
    file carries the constant 120. Whether that byte caused the losses is
    unproven, but nothing with an out-of-invariant envelope may reach a
    pedal's storage again: uploads are refused unless every check passes.
    """
    errs = []
    if len(data) < 144 or data[:4] != b"ZDLF":
        return ["not a ZD2 container (bad magic or shorter than "
                "header + trailer)"]
    hdr = struct.unpack_from("<I", data, 4)[0]
    if hdr != 120:
        errs.append("header @4 is %d; every stock ZD2 carries the constant "
                    "120 (a stale build size here is the signature of the "
                    "container uploaded in both 2026-07 pedal losses)" % hdr)
    stored = struct.unpack_from("<I", data, 8)[0]
    calc = zlib.crc32(data[12:len(data) - 16]) ^ 0xFFFFFFFF
    if stored != calc:
        errs.append("checksum @8 does not validate (stored 0x%08x, "
                    "computed 0x%08x)" % (stored, calc))
    target = struct.unpack_from("<I", data, 12)[0]
    if not target & 0x0080:
        errs.append("target 0x%04x lacks the MS Plus family bit 0x0080 "
                    "(stock MS-70CDR+ effects carry 0x0090)" % target)
    # The remaining header invariants hold in 633/633 stock files
    # (tools/zd2_envelope_census.py --lint) and in every build these tools
    # produce; a violation means a malformed or corrupted container.
    if data[93:95] != b"\x00\x00":
        errs.append("bytes @93..94 are not zero")
    if data[125:128] != b"\x00\x00\x00":
        errs.append("bytes @125..127 are not zero")
    eid = struct.unpack_from("<I", data, ID_OFF)[0]
    if data[95] != (eid >> 24) & 0xFF:
        errs.append("group byte @95 (%d) does not match the id's top byte "
                    "(0x%08x)" % (data[95], eid))
    nm = data[NAME_OFF:NAME_OFF + NAME_LEN]
    if nm[0] == 0 or not all(0x20 <= b < 0x7F
                             for b in nm.split(b"\x00", 1)[0]):
        errs.append("effect name is empty or not printable ASCII")
    if not all(0x20 <= b < 0x7F for b in data[89:93]):
        errs.append("version @89 is not 4 printable ASCII bytes")
    off = 128
    while off + 8 <= len(data) - 16:
        tag = data[off:off + 4]
        if not tag.isalnum():
            errs.append("section walk hit a non-tag at offset %d" % off)
            break
        off += 8 + struct.unpack_from("<I", data, off + 4)[0]
    else:
        if off != len(data) - 16:
            errs.append("sections end at offset %d but the 16-byte trailer "
                        "starts at %d; the container does not tile the "
                        "file" % (off, len(data) - 16))
    return errs


def check_zic(data):
    """Structural lint for a companion .ZIC icon about to be uploaded.
    Returns a list of violations; empty means clean.

    The icon is a file the pedal parses too (browser rendering, and
    whatever the boot-time scan does with it), so it gets the same
    out-of-distribution refusal as the effect container. Invariants hold
    in 636/636 stock icons (tools/zd2_envelope_census.py --lint): ZBMP
    magic, a sane descriptor block, at least one leading frame with sane
    dimensions, and bitmaps that tile the file exactly. The slot after
    the real frames is unconstrained (stock files carry a flag byte
    there)."""
    if len(data) < 12 or data[:4] != b"ZBMP":
        return ["not a ZIC icon (bad magic or truncated)"]
    dlen = struct.unpack_from("<I", data, 4)[0]
    if dlen % 4 != 0 or not 8 <= dlen <= 64:
        return ["descriptor block length %d is out of range" % dlen]
    if len(data) < 8 + dlen:
        return ["file shorter than its descriptor block"]
    frames = []
    for i in range(dlen // 4):
        w, h = struct.unpack_from("<HH", data, 8 + 4 * i)
        if w == 0 or h == 0:
            break
        frames.append((w, h))
    errs = []
    if not frames:
        errs.append("no icon frames in the descriptor")
    if any(w > 256 or h > 256 for w, h in frames):
        errs.append("a frame dimension is out of range")
    total = 8 + dlen + sum(w * (((h - 1) >> 3) + 1) for w, h in frames)
    if not errs and total != len(data):
        errs.append("bitmaps end at %d but the file is %d bytes; the icon "
                    "does not tile" % (total, len(data)))
    return errs


def require_clean_envelope(path):
    """Die (fail-closed) if the file's container envelope is out of
    invariant. Called on every path that UPLOADS the file's bytes (install,
    writetest); uninstall deletes by name and is exempt so a bad file can
    always be removed."""
    with open(path, "rb") as f:
        data = f.read()
    errs = check_envelope(data)
    if errs:
        die("container envelope out of invariant, refusing to upload %s: %s"
            % (os.path.basename(path), "; ".join(errs)), EXIT_REFUSED)


def require_clean_zic(zic_path):
    """Die (fail-closed) if the companion icon is missing or structurally
    out of invariant. Called on the install path only (install is the one
    operation that uploads the icon)."""
    if not os.path.isfile(zic_path):
        die("companion icon missing: %s" % os.path.basename(zic_path),
            EXIT_REFUSED)
    with open(zic_path, "rb") as f:
        data = f.read()
    errs = check_zic(data)
    if errs:
        die("icon structure out of invariant, refusing to upload %s: %s"
            % (os.path.basename(zic_path), "; ".join(errs)), EXIT_REFUSED)


def is_diy_id(eid):
    """True iff eid is a DIY id this tool recognizes (never a stock id).

    The wrapper only ever installs or uninstalls effects whose id falls in a
    DIY range, so it can never touch a stock effect by accident. Two ranges
    are recognized: the DIY page 0x07000f00..0x07000fff (use this for your
    own effects; see docs/effect-ids.md), and a second reserved block
    (group byte 0x01..0x09 with low 24 bits 0x001000..0x0013ff).

    Zoom occupies two ids inside the DIY page (LINE SEL at 0x07000f00 and
    the pedal-resident BPM module at 0x07000ff0). This range check alone
    does not exclude them; the stock-catalog check in classify() is what
    refuses them.
    """
    if 0x07000F00 <= eid <= 0x07000FFF:
        return True
    group = (eid >> 24) & 0xFF
    low24 = eid & 0xFFFFFF
    if 0x01 <= group <= 0x09 and 0x001000 <= low24 <= 0x0013FF:
        return True
    return False


def stock_zic_bases():
    """Basenames of every STOCK .ZIC icon: the built-in catalog, plus any
    stock files present locally. A DIY install/uninstall carries the
    companion .ZIC (--include-zic), so the icon must be checked against stock
    too: only ever touch your own .ZIC files, never a stock one."""
    bases = set(stock_catalog.STOCK_ZICS)
    bases.update(os.path.basename(p).upper()
                 for p in glob.glob(STOCK_ZIC_GLOB))
    return bases


def stock_index():
    """Stock identities to refuse: (ids, names, filenames).

    The built-in catalog (stock_catalog.py, the whole MS Plus series) is the
    baseline; any stock files present locally are scanned on top of it, so a
    newer effect not yet in the catalog is still refused if its file is
    around."""
    ids = set(stock_catalog.STOCK_IDS)
    names = set(stock_catalog.STOCK_NAMES)
    bases = set(stock_catalog.STOCK_FILENAMES)
    for p in glob.glob(STOCK_GLOB):
        bases.add(os.path.basename(p).upper())
        try:
            with open(p, "rb") as f:
                d = f.read(NAME_OFF + NAME_LEN)
            if d[:4] != b"ZDLF" or len(d) < NAME_OFF + NAME_LEN:
                continue
            ids.add(struct.unpack_from("<I", d, ID_OFF)[0])
            names.add(d[NAME_OFF:NAME_OFF + NAME_LEN].split(b"\x00", 1)[0].decode("ascii", "replace"))
        except OSError:
            continue
    return ids, names, bases


def classify(path):
    """Return (allow:bool, eid, name, reasons:list[str]). Never touches the pedal."""
    eid, name = read_zd2(path)
    base = os.path.basename(path).upper()
    stock_ids, stock_names, stock_bases = stock_index()

    reasons = []
    diy = is_diy_id(eid)
    if not diy:
        reasons.append("id 0x%08x is NOT in your DIY id space" % eid)
    if eid in stock_ids:
        reasons.append("id 0x%08x collides with a STOCK effect" % eid)
    if name in stock_names:
        reasons.append("name '%s' is a STOCK effect name" % name)
    if base in stock_bases:
        reasons.append("filename '%s' matches a STOCK corpus file" % base)

    # The install/uninstall carries the companion .ZIC (--include-zic), whose
    # name the pedal derives from the .ZD2 stem. Vet it against the stock icons
    # too: only ever touch YOUR OWN .ZIC, never a stock one. (In practice
    # a non-stock .ZD2 stem implies a non-stock .ZIC stem, but check it
    # explicitly rather than rely on that: fail-closed beats inference.)
    zic_base = os.path.splitext(base)[0] + ".ZIC"
    if zic_base in stock_zic_bases():
        reasons.append("companion icon '%s' matches a STOCK .ZIC" % zic_base)

    allow = (diy and (eid not in stock_ids) and (name not in stock_names)
             and (base not in stock_bases) and (zic_base not in stock_zic_bases()))
    return allow, eid, name, reasons


def log(op, path, eid, name, decision):
    try:
        with open(LOGFILE, "a") as f:
            f.write("%s\t%s\t%s\tid=0x%08x\tname=%s\t%s\n" % (
                datetime.datetime.now().isoformat(timespec="seconds"),
                op, path, eid, name, decision))
    except OSError:
        pass  # logging must never block a safety decision


# --------------------------------------------------------------- the transfer

def _connect():
    """Open ONE connection this process owns end to end. Returns the zoomzt2
    pedal object (for its port pair and grammar helpers)."""
    import zoomzt2
    pedal = zoomzt2.zoomzt2()
    if not pedal.connect():
        die("no pedal found (no MIDI port matching the MS Plus series); "
            "connect the pedal and retry")
    return pedal


def _autosave_off(pedal):
    """Force autosave OFF and require the pedal's ACK (fail-closed). A binary
    install itself never writes a patch, but the audition that follows can if
    autosave silently saves it; enforcing it here means even a session that
    skipped safe_connect.py has the invariant confirmed before any write."""
    import pedal_common
    pedal_common.assert_autosave_off(pedal)   # exits(2) unless the pedal ACKs
    print("pedal_diy: autosave OFF, confirmed by the pedal's own ACK")
    pedal_common.editor_off(pedal)


def _compute_flst(pedal, sess, op, path, eid, name):
    """Download the current effect list, compute the new one offline, and
    VALIDATE it before anything is written. Returns (flst_old, flst_new,
    files) or dies fail-closed. `files` is [(pedal-name, bytes)] to upload,
    icon first (install only)."""
    import file_session as fs
    import flst_check
    import zoomzt2

    flst_old = sess.download(fs.FLST_NAME)
    sess.close()

    ok, problems, _ = flst_check.validate_flst(flst_old)
    if not ok:
        die("the effect list currently ON THE PEDAL does not validate - "
            "refusing to edit it (%s). Back up and investigate before any "
            "write." % "; ".join(problems), EXIT_REFUSED)

    base = os.path.basename(path)
    # Grammar edits run on the canonical first 8502 bytes; the result is
    # padded back to the length of the list the pedal actually holds
    # (pad_to_native). A factory-fresh pedal's list is LARGER than the
    # grammar's fixed 8502 (12324 B on the MS-70CDR+, all zero past the real
    # catalogue), so without the pad an install there would silently shrink
    # the file - preserve the native size instead, never truncate.
    canon = bytes(flst_old[:flst_check.FLST_SIZE])
    if op == "install":
        flst_new = flst_check.pad_to_native(
            pedal.add_effect_from_filename(canon, path), len(flst_old))
        good, problems = flst_check.expect_single_add(flst_old, flst_new, base)
    else:
        flst_new = flst_check.pad_to_native(
            zoomzt2.zoomzt2().remove_effect(canon, base), len(flst_old))
        good, problems = flst_check.expect_single_remove(flst_old, flst_new, base)
    if not good:
        die("the computed new effect list is not a clean single-entry %s "
            "(%s) - refusing to write it" % (op, "; ".join(problems)),
            EXIT_REFUSED)

    files = []
    if op == "install":
        with open(path, "rb") as f:
            zd2 = f.read()
        zic_path = os.path.splitext(path)[0] + ".ZIC"
        with open(zic_path, "rb") as f:
            zic = f.read()
        # icon first, effect second (files-then-list order in the engine)
        files = [(os.path.basename(zic_path), zic), (base, zd2)]
    return flst_old, flst_new, files


def _execute(pedal, sess, op, path, eid, name):
    """Compute+validate the effect list, run the transfer, report. Assumes
    `sess` is already in PC mode. `pedal` is only used for its ZT2 grammar
    helpers (add_effect_from_filename). Returns an exit code. This is the
    testable core - it opens no port, so install_selftest.py drives it
    against the mock pedal end to end."""
    import file_session as fs

    if op == "install":
        require_clean_envelope(path)
        require_clean_zic(os.path.splitext(path)[0] + ".ZIC")

    flst_old, flst_new, files = _compute_flst(pedal, sess, op, path, eid, name)

    # uninstall needs the pedal filenames (effect + its icon)
    base = os.path.basename(path)
    zic_base = os.path.splitext(base)[0] + ".ZIC"

    log(op, path, eid, name, "RUN")
    print("pedal_diy: %s %s (id 0x%08x, name '%s') - verified DIY, "
          "one connection, timeout-guarded" % (op, base, eid, name))

    try:
        if op == "install":
            result = fs.install_effect(sess, files, flst_old, flst_new)
        else:
            result = fs.uninstall_effect(sess, [base, zic_base],
                                         flst_old, flst_new)
    except fs.TransferAborted as e:
        print("pedal_diy: transfer did NOT complete, but the pedal was "
              "verified SAFE - nothing was changed.")
        print("  reason: %s" % e)
        log(op, path, eid, name, "SAFE-ABORT")
        return EXIT_SAFE_ABORT
    except fs.PedalUnresolved as e:
        # The DO-NOT-POWER-CYCLE banner has already been printed by the
        # engine. Do NOT disconnect abruptly or advise a reboot.
        log(op, path, eid, name, "UNRESOLVED")
        print("pedal_diy: run `pedal_diy.py rescue` while the pedal stays "
              "POWERED. Details: %s" % e)
        return EXIT_UNRESOLVED

    sess.pcmode_off()
    if op == "install":
        print("pedal_diy: installed and read-back-verified (%d effect-list "
              "retr%s). Now, in order (SAFETY.md):"
              % (result["flst_retries"],
                 "y" if result["flst_retries"] == 1 else "ies"))
        print("  1. Audition it in a scratch patch and NEVER save the patch.")
        print("  2. Replacing it later? UNINSTALL FIRST (or the new upload "
              "is refused because the name already exists).")
        print("  3. Uninstall it at the end of the session; leave only "
              "effects you trust.")
    else:
        print("pedal_diy: uninstalled and effect-list-verified. Confirm it "
              "is gone: tools-pedal/readback.py %s <out> reports ABSENT."
              % base)
    log(op, path, eid, name, "DONE")
    return EXIT_OK


def _run_transfer(op, path, eid, name):
    """The single-connection transfer. Returns an exit code."""
    import file_session as fs

    pedal = _connect()
    sess = fs.FileSession(fs.MidoTransport(pedal.inport, pedal.outport),
                          log=lambda line: None)
    try:
        _autosave_off(pedal)
        sess.pcmode_on()
        if op == "writetest":
            return _writetest(sess, path, eid, name)
        return _execute(pedal, sess, op, path, eid, name)
    finally:
        if pedal.is_connected():
            pedal.disconnect()


def _writetest(sess, path, eid, name):
    """The effect-list-free write exercise: upload + readback-verify + delete
    a DIY binary WITHOUT ever computing or writing the effect list; the list
    is read back before and after only to PROVE it never changed. This is
    the testable core - it opens no port, so install_selftest.py drives it
    against the mock pedal end to end."""
    import file_session as fs
    import flst_check

    require_clean_envelope(path)

    base = os.path.basename(path)
    flst_old = sess.download(fs.FLST_NAME)
    sess.close()
    ok, problems, entries = flst_check.validate_flst(flst_old)
    if not ok:
        die("the effect list currently ON THE PEDAL does not validate - "
            "refusing any write while it reads back broken (%s)"
            % "; ".join(problems), EXIT_REFUSED)
    if any(e[1] == base for e in entries):
        die("'%s' is REFERENCED by the pedal's effect list - the write test "
            "deletes its file at the end, which would strand that entry. "
            "Use a DIY effect the pedal's list does not know." % base,
            EXIT_REFUSED)

    with open(path, "rb") as f:
        data = f.read()

    log("writetest", path, eid, name, "RUN")
    print("pedal_diy: writetest %s (id 0x%08x, name '%s') - upload, "
          "readback-verify, delete; the effect list is never written"
          % (base, eid, name))
    try:
        fs.writetest_effect(sess, base, data, flst_old)
    except fs.TransferAborted as e:
        print("pedal_diy: write test did NOT complete, but the pedal was "
              "verified SAFE - nothing was changed.")
        print("  reason: %s" % e)
        log("writetest", path, eid, name, "SAFE-ABORT")
        return EXIT_SAFE_ABORT
    except fs.PedalUnresolved as e:
        # The DO-NOT-POWER-CYCLE banner has already been printed by the engine.
        log("writetest", path, eid, name, "UNRESOLVED")
        print("pedal_diy: run `pedal_diy.py rescue` while the pedal stays "
              "POWERED. Details: %s" % e)
        return EXIT_UNRESOLVED

    sess.pcmode_off()
    print("pedal_diy: write test PASSED - %d bytes uploaded, read back "
          "byte-identical, deleted, verified absent; the effect list read "
          "back byte-identical throughout." % len(data))
    log("writetest", path, eid, name, "DONE")
    return EXIT_OK


def _rescue(argv):
    """Close a wedged file session and verify the effect list is intact.

    This is the tool to run after a transfer failed and told you to. It sends
    the file-session close sequence (unwedging a mid-transfer pedal), then
    reads the effect list back and validates it. If you pass a known-good
    FLST_SEQ.ZT2 AND the on-pedal list is invalid or differs, it offers to
    rewrite it through the same guarded, retrying path an install uses.

    --remove FILE.ZD2 additionally removes that effect (and its companion
    .ZIC) from the pedal, but only once the effect list reads back valid AND
    only if FILE.ZD2's basename is not one of its entries - i.e. it is an
    orphan the failed transfer left behind, never a file the list still
    references (use `uninstall` for that). Refuses up front unless FILE.ZD2
    classifies as one of your own DIY effects, the same gate every other
    upload/delete path here uses.
    """
    import file_session as fs
    import flst_check

    good_path = None
    remove_path = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--remove":
            i += 1
            if i >= len(argv):
                die("--remove needs a path", EXIT_REFUSED)
            remove_path = argv[i]
        elif not a.startswith("--") and good_path is None:
            good_path = a
        i += 1
    good = None
    if good_path:
        with open(good_path, "rb") as f:
            good = f.read()
        ok, problems, _ = flst_check.validate_flst(good)
        if not ok:
            die("the known-good FLST you passed does not itself validate "
                "(%s)" % "; ".join(problems), EXIT_REFUSED)

    remove_names = None
    if remove_path:
        allow, eid, name, reasons = classify(remove_path)
        if not allow:
            die("--remove target '%s' is not a removable DIY effect (%s) - "
                "refusing; rescue never touches anything that could be a "
                "stock effect" % (os.path.basename(remove_path),
                                  "; ".join(reasons)), EXIT_REFUSED)
        base = os.path.basename(remove_path)
        remove_names = [os.path.splitext(base)[0] + ".ZIC", base]

    pedal = _connect()
    sess = fs.FileSession(fs.MidoTransport(pedal.inport, pedal.outport),
                          log=lambda line: print("  " + line))
    try:
        return _rescue_session(sess, good, good_path, remove_names,
                               "--write-back" in argv)
    finally:
        if pedal.is_connected():
            pedal.disconnect()


def _rescue_session(sess, good, good_path, remove_names, write_back):
    """The connection-free core of rescue, so install_selftest can drive it
    against the mock pedal (as it drives _execute). Operates on an
    already-open session: unwedge, read the effect list back, validate it,
    optionally remove a named orphan, optionally rewrite from a known-good
    copy. Returns an exit code."""
    import file_session as fs
    import flst_check

    try:
        sess.pcmode_on()
    except fs.TransferTimeout:
        # A pedal WEDGED mid-file-session does not answer a new PC-mode
        # command until its open session is closed. Do NOT give up here: the
        # close sequence below is exactly what clears that wedge, and it is
        # unreachable if pcmode_on hard-fails first. (pedal #1, 2026-07-18:
        # rescue died at this line and could not recover a real wedge.) A
        # prior transfer already left the pedal in PC mode, so the close
        # still lands.
        print("  PC-mode not acknowledged; the pedal may be wedged "
              "mid-session - sending the close sequence anyway")
    print("pedal_diy rescue: closing any open file session...")
    acked = sess.abort()
    print("  close sequence %s"
          % ("ACKNOWLEDGED" if acked else "SENT (no ack)"))
    try:
        on_pedal = sess.download(fs.FLST_NAME)
        sess.close()
    except fs.TransferError as e:
        print("pedal_diy rescue: could not read the effect list back "
              "(%s). Keep the pedal POWERED; the file API is still not "
              "responding." % e)
        return EXIT_UNRESOLVED
    ok, problems, entries = flst_check.validate_flst(on_pedal)
    if ok:
        print("pedal_diy rescue: effect list reads back VALID (%d entries)."
              % len(entries))
        if good is not None and on_pedal != good:
            print("  (it differs from the known-good file you passed, but "
                  "it is structurally valid; not rewriting.)")
        if remove_names is not None:
            registered = {e[1] for e in entries}
            if remove_names[1] in registered:
                print("pedal_diy rescue: --remove target '%s' IS "
                      "registered in the valid effect list - this is NOT "
                      "an orphan. Not touching it; run "
                      "'pedal_diy.py uninstall' instead if you mean to "
                      "remove it." % remove_names[1])
            else:
                for nm in remove_names:
                    if sess.file_check(nm):
                        sess.delete(nm)
                        sess.close()
                        still_present = sess.file_check(nm)
                        sess.close()
                        if still_present:
                            print("pedal_diy rescue: '%s' still present "
                                  "after a delete attempt. Keep the "
                                  "pedal POWERED; do not power-cycle."
                                  % nm)
                            return EXIT_UNRESOLVED
                        print("pedal_diy rescue: removed orphan file "
                              "'%s'." % nm)
                    else:
                        sess.close()   # every file_check gets its file_close
                        print("pedal_diy rescue: '%s' was not on the "
                              "pedal (nothing to remove)." % nm)
        print("pedal_diy rescue: the pedal is safe to power-cycle.")
        # Leave PC mode symmetrically. Found on hardware: without this,
        # rescue leaves the pedal in PC mode, PC mode mutes the pedal's
        # parameter ACKs, and the NEXT tool's autosave pre-flight
        # refuses (fail-closed, but a needless stop). zoomzt2's
        # disconnect() cannot cover it because PC mode was entered
        # through the session, so zoomzt2's own flag was never set.
        sess.pcmode_off()
        return EXIT_OK

    print("pedal_diy rescue: the effect list on the pedal is NOT valid:")
    for p in problems:
        print("    - %s" % p)
    if good is None:
        print("  Keep the pedal POWERED. Re-run rescue with a known-good "
              "FLST_SEQ.ZT2 (e.g. from your backup) to rewrite it.")
        return EXIT_UNRESOLVED

    # Write-back is a real mutation; require explicit confirmation.
    if not write_back:
        print("  A known-good FLST was supplied. To rewrite the effect "
              "list from it, re-run with --write-back. The pedal stays "
              "POWERED throughout; the write is timeout-guarded and "
              "retried, and verified by readback.")
        return EXIT_UNRESOLVED
    print("pedal_diy rescue: rewriting the effect list from %s ..."
          % os.path.basename(good_path))
    try:
        fs.replace_flst(sess, on_pedal, good)
    except (fs.PedalUnresolved, fs.TransferError) as e:
        # PedalUnresolved: the write window failed (banner already shown).
        # TransferError: a read-only pre-window check failed (link, space,
        # or the list changed under us) - no write happened.
        print("pedal_diy rescue: rewrite did not complete (%s). Keep the "
              "pedal POWERED." % e)
        return EXIT_UNRESOLVED
    sess.pcmode_off()
    print("pedal_diy rescue: effect list rewritten and verified. Safe to "
          "power-cycle now.")
    return EXIT_OK


def main(argv):
    if not argv or argv[0] not in ("install", "uninstall", "writetest",
                                   "classify", "rescue"):
        sys.exit(__doc__)
    op = argv[0]

    if op == "rescue":
        return _rescue(argv[1:])

    if len(argv) < 2:
        sys.exit(__doc__)
    path = argv[1]
    dry = "--dry-run" in argv[2:]

    allow, eid, name, reasons = classify(path)

    if op == "classify":
        verdict = "DIY-OK (would be allowed)" if allow else "REFUSED"
        print("%s: id=0x%08x name=%s -> %s" % (os.path.basename(path), eid, name, verdict))
        for r in reasons:
            print("  - %s" % r)
        return EXIT_OK if allow else EXIT_CLASSIFY_REFUSED

    if not allow:
        for r in reasons:
            sys.stderr.write("pedal_diy:   %s\n" % r)
        log(op, path, eid, name, "REFUSED")
        die("'%s' (id 0x%08x, name '%s') is not a removable/installable DIY "
            "effect. Use zoomzt2 directly, confirming each command yourself."
            % (os.path.basename(path), eid, name))

    # --include-zic is MANDATORY (SAFETY.md): without it an install leaves a
    # BLANK tile in the effect browser (the browser renders icons, not names)
    # and an uninstall ORPHANS the .ZIC on the pedal, so the file census would
    # not return to the opening baseline. On INSTALL the icon must exist to be
    # uploaded.
    if op == "install":
        zic_path = os.path.splitext(path)[0] + ".ZIC"
        if not os.path.isfile(zic_path):
            log(op, path, eid, name, "REFUSED-NO-ZIC")
            die("no companion icon next to the effect: %s. An install without "
                "it leaves a BLANK, unidentifiable tile in the effect browser "
                "(SAFETY.md). Rebuild the effect to generate the .ZIC."
                % os.path.basename(zic_path))

    # The upload paths (_execute install, _writetest) re-check this before
    # sending bytes; checking here too means a dry run gives the verdict
    # and a real run refuses before any pedal contact.
    if op in ("install", "writetest"):
        require_clean_envelope(path)
    if op == "install":
        require_clean_zic(os.path.splitext(path)[0] + ".ZIC")

    if dry:
        print("pedal_diy: DRY-RUN - checks PASSED for %s (id 0x%08x, name '%s')"
              % (os.path.basename(path), eid, name))
        if op == "writetest":
            print("pedal_diy: would upload, readback-verify and delete over "
                  "one timeout-guarded connection; the effect list is never "
                  "written.")
        else:
            print("pedal_diy: would %s over one timeout-guarded connection, "
                  "validating the new effect list offline before any write."
                  % op)
        log(op, path, eid, name, "DRY-RUN-OK")
        return EXIT_OK

    return _run_transfer(op, path, eid, name)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
