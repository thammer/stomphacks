#!/usr/bin/env python3
"""Build a ZD2 container and a ZIC icon from scratch: no stock file needed.

Every section of a ZD2 container (ICON/TXJ1/TXE1/INFO/DATA/PRMJ/PRME) is
TAG + u32 length + payload. The fixed header's non-identity fields follow
the stock corpus exactly: offset 4 carries the constant 120 (it is never a
file length) and offset 12 the target bitfield (0x0090 on the MS-70CDR+,
the value 145 of its 149 stock effects carry). Three further fields have
undecoded meaning:

    unknown  @16     73 bytes   a sparse flag block; 66 of the bytes are zero
                                in every stock effect
    unknown4 @122     3 bytes   an effect-category tag (opaque encoding)
    unknown5 trailer 16 bytes   not covered by the CRC

This tool ships all three ZEROED. That configuration is hardware-proven: an
effect with all three fields zeroed loads, runs, takes knob edits, bypasses
and survives chain edits on an MS-70CDR+ (firmware 1.20). Proven for a simple
SFX-class effect; other effect classes are extrapolation until tested. The
pedal's own browser categorises an effect by its id group, not by the zeroed
tag. One cosmetic caveat: Guitar Lab may not list such an effect in its
browser and may show a blank icon in its edit window (params still editable
there), and that may be true of any DIY effect, not just zeroed ones.

Because the fields are zero, a built container carries no bytes from any
Zoom file at all; see docs/ip-and-licensing.md.

tools/zd2_make_effect.py uses build_template() from here to synthesize the
container the build pipeline packs your compiled code into. The other entry
points are for checking and experimenting by hand.

Pedal-safe per tools/README.md: builds and verifies local files only. It
never talks to a pedal.

Usage (from the repo root):
  .venv/bin/python3 tools/zd2_from_scratch.py --selftest       # prove it
  .venv/bin/python3 tools/zd2_from_scratch.py --demo OUT.ZD2   # emit a pair
  .venv/bin/python3 tools/zd2_from_scratch.py --recontainer IN.ZD2 OUT.ZD2
      # zero the 3 opaque fields of an existing effect (CRC recomputed,
      # DSP code untouched), normalizing a container to the proven state
"""

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
# zoom-zt2 sits at the repo root; fall back to the working directory so the
# selftest also runs from a checkout that keeps this file elsewhere.
for _cand in (ROOT / "zoom-zt2", pathlib.Path.cwd() / "zoom-zt2"):
    if _cand.is_dir():
        sys.path.insert(0, str(_cand))
        break

import crcmod        # noqa: E402
import zoomzt2       # noqa: E402

# The three undecoded fields, all zero (hardware-proven; see the docstring).
OPAQUE_HEADER_73 = bytes(73)
OPAQUE_CATEGORY_3 = bytes(3)
OPAQUE_TRAILER_16 = bytes(16)

# Effect groups: the id's top byte, and the group name string the container
# carries. Same table as zoom-zt2's grammar and docs/effect-ids.md.
GROUP_NAMES = {1: "DYNAMICS", 2: "FILTER", 3: "DRIVE", 4: "AMP",
               5: "CABINET", 6: "MODULATION", 7: "SFX", 8: "DELAY",
               9: "REVERB", 11: "PEDAL"}

# The standard DIY icon geometry: two frames, 72x97 (browser tile) and
# 102x128 (edit view). The container's ICON section carries one 72x97 frame.
ICON_FRAMES = [(72, 97), (102, 128)]
ICON_TILE_BYTES = 72 * (((97 - 1) >> 3) + 1)     # 936


def crc32_zd2(data: bytes) -> int:
    """CRC recipe re-expressed from mungewell/zoom-zt2 decode_effect.py (MIT).
    Spans data[12:-16]: covers neither magic/length/checksum nor trailer."""
    c = crcmod.Crc(0x104C11DB7, rev=True, initCrc=0x00000000, xorOut=0xFFFFFFFF)
    c.update(data[12:-16])
    return c.crcValue ^ 0xFFFFFFFF


def build_zic(frames) -> bytes:
    """A complete ZIC, from scratch. frames = [(width, height, bitmap), ...].

    Layout (fully decoded, per zoom-zt2's convert_zic.py):
        "ZBMP" | u32 descriptor-block length | per frame: u16 w, u16 h
               | zero entries padding the block to 24 bytes
               | then each frame's 1-bit bitmap, w * ceil(h/8) bytes

    The descriptor block is always the stock 24-byte table (length 0x18 in
    every stock ZIC): the real entries first, zero entries after. The first
    zero entry is not just padding, it is the terminator the convert_zic.py
    parser stops at. Without it the parser reads on into bitmap data and
    fails on any drawn icon (all-zero blank bitmaps mask the bug, which is
    how it once slipped through this file's selftest).
    """
    desc = b"".join(w.to_bytes(2, "little") + h.to_bytes(2, "little")
                    for w, h, _ in frames)
    if len(desc) > 20:
        raise ValueError("more than 5 frames leaves no room for the zero "
                         "terminator entry in the 24-byte descriptor table")
    desc = desc.ljust(24, b"\x00")
    out = b"ZBMP" + len(desc).to_bytes(4, "little") + desc
    for w, h, bmp in frames:
        stripes = ((h - 1) >> 3) + 1
        want = w * stripes
        if len(bmp) != want:
            raise ValueError(f"frame {w}x{h} needs {want} bytes, got {len(bmp)}")
        out += bmp
    return out


def blank_zic() -> bytes:
    """The standard two-frame DIY ZIC with blank bitmaps, ready to draw on."""
    return build_zic([(w, h, bytes(w * (((h - 1) >> 3) + 1)))
                      for w, h in ICON_FRAMES])


def build_zd2(*, blob: bytes, name: str, groupname: str, group: int, eid: int,
              version: str = "1.00", target: int = 0x0090,
              icon: bytes = b"", txe1: str = "", txj1: bytes = b"",
              info: bytes = bytes(16), dspload: float = 8.0,
              prme: bytes = b"", prmj: bytes = b"",
              header73: bytes = OPAQUE_HEADER_73,
              category3: bytes = OPAQUE_CATEGORY_3,
              trailer16: bytes = OPAQUE_TRAILER_16) -> bytes:
    """A complete ZD2 container, from scratch. No stock file is read."""
    c = zoomzt2.Container()
    c.length = 0                      # patched below
    c.checksum = 0                    # patched below
    c.target = target
    c.unknown = header73
    c.version = version
    c.group = group
    c.id = eid
    c.name = name
    c.namepad = bytes(10 - len(name))
    c.groupname = groupname
    c.grouppad = bytes(10 - len(groupname))
    c.unknown4 = category3

    # TXE1's description is an IfThenElse gated on a Peek. On BUILD a Peek
    # yields None, so it takes the Bytes branch; pass bytes, not str.
    txe1_b = txe1.encode("ascii") if isinstance(txe1, str) else txe1
    c.ICON = zoomzt2.Container(length=len(icon), data=icon)
    c.TXJ1 = zoomzt2.Container(length=len(txj1), data=txj1)
    c.TXE1 = zoomzt2.Container(length=len(txe1_b), description=txe1_b)
    c.INFO = zoomzt2.Container(length=len(info) + 4, data=info,
                               dspload=dspload)
    c.DATA = zoomzt2.Container(length=len(blob), data=blob)
    c.INF2 = None
    c.CCOE = None
    # grammar field names differ: PRMJ carries bytes in "data"; PRME carries
    # a str in "xml" (PaddedString: building it with bytes raises inside
    # the grammar's Optional(), which silently DROPS the whole section, so
    # normalize to str here).
    prme_s = prme.decode("ascii") if isinstance(prme, bytes) else prme
    c.PRMJ = (zoomzt2.Container(length=len(prmj), data=prmj) if prmj else None)
    c.PRME = (zoomzt2.Container(length=len(prme_s), xml=prme_s)
              if prme_s else None)
    c.unknown5 = trailer16

    raw = zoomzt2.ZD2.build(c)
    raw = bytearray(raw)
    # @4 is NOT a file length: every stock ZD2 across five pedal models
    # carries the constant 120 here. This builder used to stamp the built
    # size instead, and because the pipeline packs sections into a template
    # afterwards, shipped files carried a stale template size in this
    # field. No stock file ever carries anything but 120; write 120.
    raw[4:8] = (120).to_bytes(4, "little")
    raw[8:12] = crc32_zd2(bytes(raw)).to_bytes(4, "little")   # checksum
    return bytes(raw)


def verify_envelope(raw: bytes) -> None:
    """Assert the stock-corpus envelope invariants on a built container;
    raises ValueError on violation.

    Every stock ZD2 satisfies these, and the installer refuses to upload a
    file that does not (pedal_diy check_envelope). Checking at build time
    too catches a builder regression the moment it happens, not at the
    pedal.
    """
    errs = []
    if len(raw) < 144 or raw[:4] != b"ZDLF":
        errs.append("bad magic or truncated")
    else:
        if int.from_bytes(raw[4:8], "little") != 120:
            errs.append("header @4 is %d, not the stock constant 120"
                        % int.from_bytes(raw[4:8], "little"))
        if int.from_bytes(raw[8:12], "little") != crc32_zd2(raw):
            errs.append("checksum does not validate")
        if not int.from_bytes(raw[12:16], "little") & 0x0080:
            errs.append("target 0x%04x lacks the MS Plus family bit 0x0080"
                        % int.from_bytes(raw[12:16], "little"))
        if raw[93:95] != b"\x00\x00" or raw[125:128] != b"\x00\x00\x00":
            errs.append("constant-zero header bytes (@93..94 / @125..127) "
                        "are not zero")
        if raw[95] != raw[99]:
            errs.append("group byte @95 does not match the id's top byte")
        nm = raw[100:111]
        if nm[0] == 0 or not all(0x20 <= b < 0x7F
                                 for b in nm.split(b"\x00", 1)[0]):
            errs.append("effect name is empty or not printable ASCII")
        if not all(0x20 <= b < 0x7F for b in raw[89:93]):
            errs.append("version @89 is not 4 printable ASCII bytes")
        off = 128
        while off + 8 <= len(raw) - 16:
            tag = raw[off:off + 4]
            if not tag.isalnum():
                errs.append("section walk hit a non-tag at offset %d" % off)
                break
            off += 8 + int.from_bytes(raw[off + 4:off + 8], "little")
        else:
            if off != len(raw) - 16:
                errs.append("sections end at %d but the trailer starts at "
                            "%d" % (off, len(raw) - 16))
    if errs:
        raise ValueError("container envelope out of invariant: "
                         + "; ".join(errs))


def verify_zic(raw: bytes) -> None:
    """Assert the stock-corpus structural invariants on a built ZIC icon;
    raises ValueError on violation. Every stock icon satisfies these
    (tools/zd2_envelope_census.py --lint): ZBMP magic, a sane descriptor
    block, at least one leading frame, bitmaps tiling the file exactly.
    The pedal parses the icon too, so the installer refuses one that
    fails; checking at build time catches the regression at the desk."""
    errs = []
    if len(raw) < 12 or raw[:4] != b"ZBMP":
        errs.append("bad magic or truncated")
    else:
        dlen = int.from_bytes(raw[4:8], "little")
        if dlen % 4 != 0 or not 8 <= dlen <= 64 or len(raw) < 8 + dlen:
            errs.append("descriptor block length %d is out of range" % dlen)
        else:
            frames = []
            for i in range(dlen // 4):
                w = int.from_bytes(raw[8 + 4 * i:10 + 4 * i], "little")
                h = int.from_bytes(raw[10 + 4 * i:12 + 4 * i], "little")
                if w == 0 or h == 0:
                    break
                frames.append((w, h))
            if not frames or any(w > 256 or h > 256 for w, h in frames):
                errs.append("no frames, or a frame dimension out of range")
            else:
                total = 8 + dlen + sum(w * (((h - 1) >> 3) + 1)
                                       for w, h in frames)
                if total != len(raw):
                    errs.append("bitmaps end at %d but the file is %d "
                                "bytes" % (total, len(raw)))
    if errs:
        raise ValueError("icon structure out of invariant: "
                         + "; ".join(errs))


def build_template(*, name: str, eid: int, version: str = "1.00") -> bytes:
    """The container template zd2_make_effect.py packs a build into.

    Every section the finalize step later rewrites (identity, PRME/PRMJ,
    TXE1/TXJ1, ICON pixels, INFO, DATA) is present with a placeholder, so
    the pipeline's existing pack-and-finalize flow works unchanged; it
    just starts from a container that contains nothing from Zoom.
    """
    group = (eid >> 24) & 0xFF
    groupname = GROUP_NAMES.get(group)
    if groupname is None:
        raise ValueError(f"id 0x{eid:08x}: group {group} has no known name "
                         f"(known: {sorted(GROUP_NAMES)})")
    placeholder = b'{  \r\n\t"Parameters":[  \r\n\r\n\t]\r\n}'
    return build_zd2(
        blob=b"\x7fELF" + bytes(60),          # stand-in; the pack replaces it
        name=name[:10], groupname=groupname, group=group, eid=eid,
        version=version,
        icon=bytes(ICON_TILE_BYTES),          # blank tile; finalize swaps in
        txe1="-",                             # the drawn frame by size match
        txj1=b"-\0",
        prme=placeholder, prmj=placeholder)


def recontainer(src: pathlib.Path, dst: pathlib.Path) -> int:
    """Zero the 3 opaque fields of an existing effect and recompute the CRC.

    Byte surgery on the original file, not a reconstruction: by
    construction it changes ONLY the 92 opaque bytes plus the CRC that
    covers them. The result is the hardware-proven zeroed configuration.

    The three field offsets are fixed: the header is a constant length
    because the name and groupname regions are always 11 bytes each.
      unknown   @16   73 bytes  (flag block)
      unknown4  @122   3 bytes  (effect-category tag)
      unknown5  last  16 bytes  (trailer; not covered by the CRC)
    """
    a = bytearray(src.read_bytes())
    orig = bytes(a)

    # sanity-check the fixed offsets against the grammar before touching bytes
    p = zoomzt2.ZD2.parse(orig)
    assert bytes(a[16:89]) == bytes(p.unknown), "unknown@16 offset mismatch"
    assert bytes(a[122:125]) == bytes(p.unknown4), "unknown4@122 offset mismatch"
    assert bytes(a[-16:]) == bytes(p.unknown5), "trailer offset mismatch"

    a[16:89] = OPAQUE_HEADER_73          # unknown
    a[122:125] = OPAQUE_CATEGORY_3       # unknown4 (category tag)
    a[-16:] = OPAQUE_TRAILER_16          # unknown5 (trailer)
    a[8:12] = crc32_zd2(bytes(a)).to_bytes(4, "little")   # recompute checksum
    dst.write_bytes(bytes(a))

    diffs = [i for i in range(len(orig)) if orig[i] != a[i]]
    expected = (set(range(8, 12)) | set(range(16, 89))
                | set(range(122, 125)) | set(range(len(a) - 16, len(a))))
    stray = [i for i in diffs if i not in expected]
    back = zoomzt2.ZD2.parse(bytes(a))
    crc_ok = int.from_bytes(a[8:12], "little") == crc32_zd2(bytes(a))
    code_ok = (not p.DATA) or bytes(back.DATA.data) == bytes(p.DATA.data)

    print(f"re-containered {src.name} -> {dst.name}")
    print(f"  size unchanged: {len(orig)} B")
    print(f"  bytes changed: {len(diffs)} (92 opaque + up to 4 CRC), "
          f"all expected: {'YES' if not stray else f'NO, STRAY {stray[:8]}'}")
    print(f"  DSP code byte-identical: {'YES' if code_ok else 'NO'}")
    print(f"  CRC validates: {'YES' if crc_ok else 'NO'}")
    print(f"  parses back:   YES (id 0x{back.id:08x}, name {back.name!r})")
    ok = crc_ok and code_ok and not stray
    print(f"\n  {'READY' if ok else 'FAILED: do not install'}")
    return 0 if ok else 1


def selftest() -> int:
    print("== ZD2 / ZIC from-scratch selftest (no stock file is opened) ==\n")
    ok = True

    # --- ZIC ---------------------------------------------------------------
    # Two cases: blank bitmaps, and drawn bitmaps (0xff fill, like the
    # inverted frame the pipeline draws). The drawn case is the one that
    # catches a missing zero terminator in the descriptor table: the parser
    # reads entries until it meets a zero width, so without the terminator
    # it runs into bitmap data. All-zero blank bitmaps stop it by luck,
    # which is exactly how the bug once passed this selftest.
    import convert_zic
    for label, fill in (("blank", 0x00), ("drawn", 0xFF)):
        frames = [(72, 97, bytes([fill]) * (72 * 13)),
                  (102, 128, bytes([fill]) * (102 * 16))]
        zic = build_zic(frames)
        back = convert_zic.ZIC.parse(zic)
        got = [(i.width, i.height) for i in back.icons]
        bmps_ok = all(bytes(back.datas[i].data) == frames[i][2]
                      for i in range(len(frames)))
        desc_ok = zic[4:8] == (24).to_bytes(4, "little")
        zic_ok = got == [(72, 97), (102, 128)] and bmps_ok and desc_ok
        print(f"ZIC   built {len(zic):5d} B from scratch ({label} bitmaps) "
              f"-> reparsed frames {got}")
        print(f"      {'PASS' if zic_ok else 'FAIL'}: parses back, bitmaps "
              "intact, stock 24-byte descriptor\n")
        ok &= zic_ok

    # --- ZD2 ---------------------------------------------------------------
    blob = b"\x7fELF" + bytes(60)        # a synthetic stand-in payload
    zd2 = build_zd2(blob=blob, name="Scratch", groupname="SFX", group=7,
                    eid=0x07000F7F, txe1="Built from scratch.",
                    prme=b'{"Parameters":[]}')
    parsed = zoomzt2.ZD2.parse(zd2)
    crc_ok = int.from_bytes(zd2[8:12], "little") == crc32_zd2(zd2)
    rt_ok = zoomzt2.ZD2.build(parsed) == zd2
    data_ok = bytes(parsed.DATA.data) == blob
    id_ok = parsed.id == 0x07000F7F and parsed.name == "Scratch"
    opaque_zero = (bytes(parsed.unknown) == bytes(73)
                   and bytes(parsed.unknown4) == bytes(3)
                   and bytes(parsed.unknown5) == bytes(16))
    env_ok = (int.from_bytes(zd2[4:8], "little") == 120
              and int.from_bytes(zd2[12:16], "little") == 0x0090)

    print(f"ZD2   built {len(zd2):5d} B from scratch")
    for label, good in (("parses back", True), ("CRC validates", crc_ok),
                        ("round-trips byte-exact", rt_ok),
                        ("payload preserved", data_ok),
                        ("identity preserved", id_ok),
                        ("the 3 opaque fields are all zero", opaque_zero),
                        ("@4 = 120 and target = 0x0090, the stock-corpus "
                         "constants", env_ok)):
        print(f"      {'PASS' if good else 'FAIL'}: {label}")
        ok &= good

    # --- the pipeline template ----------------------------------------------
    tpl = build_template(name="Gain", eid=0x07000F0A, version="1.00")
    tp = zoomzt2.ZD2.parse(tpl)
    t_crc = int.from_bytes(tpl[8:12], "little") == crc32_zd2(tpl)
    t_rt = zoomzt2.ZD2.build(tp) == tpl
    missing = [s for s in ("ICON", "TXJ1", "TXE1", "INFO", "DATA",
                           "PRMJ", "PRME") if tp.get(s) is None]
    t_sects = not missing
    if missing:
        print(f"      (missing sections: {missing})")
    t_zero = (bytes(tp.unknown) == bytes(73)
              and bytes(tp.unknown4) == bytes(3)
              and bytes(tp.unknown5) == bytes(16))
    t_grp = tp.group == 7 and tp.groupname == "SFX"
    t_env = (int.from_bytes(tpl[4:8], "little") == 120
             and int.from_bytes(tpl[12:16], "little") == 0x0090)
    print(f"\nTPL   built {len(tpl):5d} B pipeline template (build_template)")
    for label, good in (("CRC validates", t_crc),
                        ("round-trips byte-exact", t_rt),
                        ("every finalize-rewritten section present", t_sects),
                        (f"ICON placeholder is the {ICON_TILE_BYTES}-byte "
                         "tile", tp.ICON.length == ICON_TILE_BYTES),
                        ("group derived from the id (7 -> SFX)", t_grp),
                        ("the 3 opaque fields are all zero", t_zero),
                        ("@4 = 120 and target = 0x0090, the stock-corpus "
                         "constants", t_env)):
        print(f"      {'PASS' if good else 'FAIL'}: {label}")
        ok &= good

    print("\n" + ("SELFTEST PASSED" if ok else "SELFTEST FAILED"))
    return 0 if ok else 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--demo", metavar="OUT.ZD2")
    ap.add_argument("--recontainer", nargs=2, metavar=("IN.ZD2", "OUT.ZD2"),
                    help="rebuild an effect's container with the 3 opaque "
                         "fields zeroed (CRC recomputed, code untouched)")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    if a.recontainer:
        sys.exit(recontainer(pathlib.Path(a.recontainer[0]),
                             pathlib.Path(a.recontainer[1])))
    if a.demo:
        out = pathlib.Path(a.demo)
        out.write_bytes(build_zd2(blob=b"\x7fELF" + bytes(60), name="Scratch",
                                  groupname="SFX", group=7, eid=0x07000F7F))
        out.with_suffix(".ZIC").write_bytes(blank_zic())
        print(f"wrote {out} and {out.with_suffix('.ZIC')}; no stock file "
              "opened")
        sys.exit(0)
    ap.print_help()


if __name__ == "__main__":
    main()
