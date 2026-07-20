#!/usr/bin/env python3
"""Strip debug/symbol dead weight from the DSP ELF inside a ZD2 container.

Pedal-safe: reads and writes local files only (see tools/README.md).

What is removed: the `.debug_*` DWARF sections
and the static `.symtab`/`.strtab`. What is kept: every alloc section,
`.rela.dyn`, the whole dynamic machinery (`.dynamic`/`.dynsym`/`.dynstr`/
`.hash`; relocs carry .dynsym indices), the TI metadata sections, the FULL
section-header table (stripped sections stay listed with sh_size = 0, like
the ~20 empty sections every stock blob already carries) and the original
`.shstrtab` bytes (so no sh_name changes at all).

The rewrite is chunk-preserving: retained byte ranges are copied verbatim in
their original order; only the file offsets of ranges after a removed region
change. Everything that encodes a file offset is patched: e_phoff/e_shoff,
each phdr's p_offset, each shdr's sh_offset, and the .dynamic tags that hold
file offsets in TI blobs (DT_HASH/DT_STRTAB/DT_SYMTAB/DT_RELA/DT_JMPREL, all
confirmed file offsets, not vaddrs, in the corpus; DT_SONAME and the
DT_C6000_GSYM/GSTR offsets are section-relative and stay untouched).

Every run verifies its own output (independent pyelftools pass): identical
e_entry/e_flags/section-table structure, byte-identical retained section
and segment content, identical .rela.dyn entries, correctly re-pointed
dynamic tags. It then repacks the container with a recomputed CRC and checks
the result round-trips byte-exact.

Usage:
    .venv/bin/python3 tools/zd2_strip.py FILE.ZD2 -o OUT.ZD2
    .venv/bin/python3 tools/zd2_strip.py FILE.ZD2 -o OUT.ZD2 --keep-blob DIR
    .venv/bin/python3 tools/zd2_strip.py --cross-check STRIPPED.elf FILE.ZD2
    .venv/bin/python3 tools/zd2_strip.py --batch SRC_DIR OUT_DIR

--cross-check compares retained-section content against an independently
stripped ELF (e.g. TI strip6x -p output) as an oracle. --batch strips and
verifies a whole corpus (outputs land in OUT_DIR as <name>.stripped.ZD2).
"""

import argparse
import glob
import io
import os
import struct
import sys

from elftools.elf.elffile import ELFFile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "zoom-zt2"))
import crcmod  # noqa: E402
import zoomzt2  # noqa: E402

STRIP_PREFIXES = (".debug_",)
STRIP_NAMES = {".symtab", ".strtab"}
FILEOFF_TAGS = {4, 5, 6, 7, 23}  # DT_HASH,STRTAB,SYMTAB,RELA,JMPREL
SHF_ALLOC = 0x2
SHT_NOBITS = 8
SHF = ("name", "type", "flags", "addr", "offset", "size",
       "link", "info", "align", "entsize")


def is_strip_name(name):
    return name.startswith(STRIP_PREFIXES) or name in STRIP_NAMES


def cstr(table, off):
    end = table.find(b"\x00", off)
    return table[off:end].decode("ascii", "replace")


def align_of(offset, declared):
    """Alignment to preserve for a chunk: its declared sh_addralign if the
    original offset honors it, else the largest power of two (<=8) that
    divides the original offset."""
    a = max(declared, 1)
    if offset % a == 0:
        return a
    for a in (8, 4, 2):
        if offset % a == 0:
            return a
    return 1


def strip_elf(data):
    """Return (stripped_bytes, report dict). Raises ValueError on any layout
    this rewriter does not provably handle."""
    if data[:4] != b"\x7fELF" or data[4] != 1 or data[5] != 1:
        raise ValueError("not a little-endian ELF32")
    e_phoff, e_shoff = struct.unpack_from("<II", data, 28)
    (e_ehsize, e_phentsize, e_phnum,
     e_shentsize, e_shnum, e_shstrndx) = struct.unpack_from("<6H", data, 40)
    shdrs = [dict(zip(SHF, struct.unpack_from("<10I", data,
                                              e_shoff + i * e_shentsize)))
             for i in range(e_shnum)]
    st = shdrs[e_shstrndx]
    shstr = data[st["offset"]:st["offset"] + st["size"]]
    names = [cstr(shstr, s["name"]) for s in shdrs]

    strip_idx = {i for i, s in enumerate(shdrs)
                 if is_strip_name(names[i]) and s["size"]
                 and s["type"] != SHT_NOBITS}
    for i in strip_idx:
        if shdrs[i]["flags"] & SHF_ALLOC:
            raise ValueError(f"refusing to strip alloc section {names[i]}")

    # ---- chunks: every byte range that exists in the file
    chunks = [{"start": e_phoff, "size": e_phnum * e_phentsize,
               "align": 1, "kind": "phdrs"},
              {"start": e_shoff, "size": e_shnum * e_shentsize,
               "align": 1, "kind": "shdrs"}]
    for i, s in enumerate(shdrs):
        if s["size"] and s["type"] != SHT_NOBITS:
            chunks.append({"start": s["offset"], "size": s["size"],
                           "align": align_of(s["offset"], s["align"]),
                           "kind": "strip" if i in strip_idx else "sec",
                           "idx": i})
    chunks.sort(key=lambda c: c["start"])
    cursor = e_ehsize
    for c in chunks:
        if c["start"] < cursor:
            raise ValueError(f"overlapping regions at {c['start']:#x}")
        cursor = c["start"] + c["size"]
    if cursor > len(data):
        raise ValueError("section data past EOF")

    # ---- first pass: assign new offsets
    cursor = e_ehsize
    for c in chunks:
        if c["kind"] == "strip":
            c["new"] = None
            continue
        cursor = (cursor + c["align"] - 1) & ~(c["align"] - 1)
        c["new"] = cursor
        cursor += c["size"]
    new_size = cursor

    def map_off(off, what):
        """Map an old file offset inside a retained chunk to its new one."""
        for c in chunks:
            if c["new"] is not None and \
                    c["start"] <= off < c["start"] + c["size"]:
                return c["new"] + off - c["start"]
        raise ValueError(f"{what}: offset {off:#x} not in a retained chunk")

    def map_boundary(off):
        """Map any old offset: inside a retained chunk like map_off, else to
        the new start of the next retained chunk (for empty sections and
        never-dereferenced tags)."""
        for c in chunks:
            if c["new"] is not None and \
                    c["start"] <= off < c["start"] + c["size"]:
                return c["new"] + off - c["start"]
        nxt = [c["new"] for c in chunks
               if c["new"] is not None and c["start"] >= off]
        return min(nxt) if nxt else new_size

    # ---- second pass: emit
    out = bytearray(new_size)
    out[:e_ehsize] = data[:e_ehsize]
    for c in chunks:
        if c["new"] is not None:
            out[c["new"]:c["new"] + c["size"]] = \
                data[c["start"]:c["start"] + c["size"]]

    # ---- patch ehdr
    struct.pack_into("<II", out, 28,
                     map_off(e_phoff, "e_phoff"), map_off(e_shoff, "e_shoff"))

    # ---- patch phdrs (content otherwise identical)
    new_phoff = map_off(e_phoff, "e_phoff")
    for i in range(e_phnum):
        o = new_phoff + i * e_phentsize
        p_off, = struct.unpack_from("<I", out, o + 4)
        p_filesz, = struct.unpack_from("<I", out, o + 16)
        if p_filesz:
            new_p = map_off(p_off, f"phdr[{i}].p_offset")
            # the whole segment must map linearly (no stripped bytes inside)
            if map_off(p_off + p_filesz - 1,
                       f"phdr[{i}] end") - new_p != p_filesz - 1:
                raise ValueError(f"phdr[{i}] spans a stripped region")
        else:
            new_p = map_boundary(p_off)
        struct.pack_into("<I", out, o + 4, new_p)

    # ---- patch shdrs
    new_shoff = map_off(e_shoff, "e_shoff")
    for i, s in enumerate(shdrs):
        o = new_shoff + i * e_shentsize
        if i in strip_idx:
            struct.pack_into("<II", out, o + 16, map_boundary(s["offset"]), 0)
        elif s["size"] and s["type"] != SHT_NOBITS:
            struct.pack_into("<I", out, o + 16,
                             map_off(s["offset"], names[i]))
        else:
            struct.pack_into("<I", out, o + 16, map_boundary(s["offset"]))

    # ---- patch .dynamic (tags whose value is a file offset)
    dyn_i = names.index(".dynamic") if ".dynamic" in names else None
    if dyn_i is not None and shdrs[dyn_i]["size"]:
        base = map_off(shdrs[dyn_i]["offset"], ".dynamic")
        for k in range(shdrs[dyn_i]["size"] // 8):
            tag, val = struct.unpack_from("<II", out, base + k * 8)
            if tag == 0:
                break
            if tag in FILEOFF_TAGS and val:
                struct.pack_into("<I", out, base + k * 8 + 4,
                                 map_boundary(val))

    stripped_bytes = sum(shdrs[i]["size"] for i in strip_idx)
    return bytes(out), {
        "stripped": sorted(names[i] for i in strip_idx),
        "stripped_bytes": stripped_bytes,
        "old_size": len(data),
        "new_size": new_size,
    }


def verify_strip(old, new):
    """Independent controlled diff of two blobs (pyelftools). Raises
    AssertionError with a specific message on the first discrepancy;
    returns a list of human-readable check descriptions on success."""
    a, b = ELFFile(io.BytesIO(old)), ELFFile(io.BytesIO(new))
    checks = []

    for f in ("e_entry", "e_machine", "e_type", "e_flags", "e_phnum",
              "e_shnum", "e_shstrndx"):
        assert a.header[f] == b.header[f], f"{f} changed"
    checks.append(f"ehdr: entry {a.header['e_entry']:#x}, machine/type/"
                  "flags/counts identical")

    n_data = n_zero = 0
    for i, (sa, sb) in enumerate(zip(a.iter_sections(), b.iter_sections())):
        assert sa.name == sb.name, f"section {i} name"
        for f in ("sh_type", "sh_flags", "sh_addr", "sh_link", "sh_info",
                  "sh_entsize"):
            assert sa[f] == sb[f], f"{sa.name} {f}"
        if is_strip_name(sa.name) and sa["sh_size"]:
            assert sb["sh_size"] == 0, f"{sa.name} not emptied"
            n_zero += 1
        else:
            assert sa["sh_size"] == sb["sh_size"], f"{sa.name} size"
            if sa["sh_size"] and sa["sh_type"] != "SHT_NOBITS" \
                    and sa.name != ".dynamic":
                # .dynamic is exempt: its re-pointed file-offset tags are
                # verified field-by-field below
                assert sa.data() == sb.data(), f"{sa.name} content differs"
                n_data += 1
    checks.append(f"sections: {n_data} retained byte-identical, "
                  f"{n_zero} stripped to sh_size=0, table structure intact")

    for i, (pa, pb) in enumerate(zip(a.iter_segments(), b.iter_segments())):
        for f in ("p_type", "p_vaddr", "p_paddr", "p_filesz", "p_memsz",
                  "p_flags", "p_align"):
            assert pa.header[f] == pb.header[f], f"phdr {i} {f}"
        if pa["p_type"] == "PT_DYNAMIC":
            continue  # holds the re-pointed tags; verified below
        oa = old[pa["p_offset"]:pa["p_offset"] + pa["p_filesz"]]
        ob = new[pb["p_offset"]:pb["p_offset"] + pb["p_filesz"]]
        assert oa == ob, f"phdr {i} segment content differs"
    checks.append(f"phdrs: {a.header['e_phnum']} segments byte-identical "
                  "(only p_offset re-pointed; PT_DYNAMIC checked tag-wise)")

    da = a.get_section_by_name(".dynamic")
    db = b.get_section_by_name(".dynamic")
    if da and da["sh_size"]:
        old_secs = {s["sh_offset"]: s.name for s in a.iter_sections()
                    if s["sh_size"]}
        new_off = {s.name: s["sh_offset"] for s in b.iter_sections()}
        ta, tb = list(da.iter_tags()), list(db.iter_tags())
        assert len(ta) == len(tb), "dynamic tag count"
        n_re = 0
        for x, y in zip(ta, tb):
            assert x.entry.d_tag == y.entry.d_tag, "dynamic tag order"
            raw = x.entry.d_val if hasattr(x.entry, "d_val") else None
            tagno = getattr(x, "_tag_number", None)
            # resolve numeric tag via the raw struct (pyelftools names vary)
            if x.entry.d_tag in ("DT_HASH", "DT_STRTAB", "DT_SYMTAB",
                                 "DT_RELA", "DT_JMPREL") and raw:
                src = old_secs.get(raw)
                if src is not None:
                    assert y.entry.d_val == new_off[src], \
                        f"{x.entry.d_tag} not re-pointed to {src}"
                    n_re += 1
            elif x.entry.d_tag not in ("DT_JMPREL",):
                assert x.entry.d_val == y.entry.d_val, \
                    f"{x.entry.d_tag} value changed"
        checks.append(f"dynamic: tag sequence identical, {n_re} file-offset "
                      "tags re-pointed to the same-named section")

    ra = a.get_section_by_name(".rela.dyn")
    rb = b.get_section_by_name(".rela.dyn")
    if ra and ra["sh_size"]:
        ea = [(r["r_offset"], r["r_info"], r["r_addend"])
              for r in ra.iter_relocations()]
        eb = [(r["r_offset"], r["r_info"], r["r_addend"])
              for r in rb.iter_relocations()]
        assert ea == eb, ".rela.dyn entries differ"
        checks.append(f"relocs: all {len(ea)} .rela.dyn entries identical")

    return checks


def crc_of(built):
    # CRC recipe (poly/init/xorOut over data[12:-16]) re-expressed from
    # mungewell/zoom-zt2 decode_effect.py (MIT).
    crc = crcmod.Crc(0x104C11DB7, rev=True, initCrc=0x00000000,
                     xorOut=0xFFFFFFFF)
    crc.update(built[12:-16])
    return crc.crcValue ^ 0xFFFFFFFF


def strip_container(src_path, dst_path, keep_blob=None, quiet=False):
    """Full pipeline for one ZD2. Returns report dict; raises on failure."""
    data = open(src_path, "rb").read()
    if os.path.realpath(src_path) == os.path.realpath(dst_path):
        raise ValueError("refusing to overwrite the input file")

    # roundtrip gate: only repack effects the container grammar reproduces
    # byte-exactly
    config = zoomzt2.ZD2.parse(data)
    if zoomzt2.ZD2.build(config) != data:
        raise ValueError("container does not round-trip byte-exact "
                         "(known: FLTERPPD/TremRv dup-PRME); refusing")
    if not config.get("DATA"):
        raise ValueError("no DATA blob in container")

    blob = bytes(config["DATA"]["data"])
    new_blob, rep = strip_elf(blob)
    rep["checks"] = verify_strip(blob, new_blob)

    if keep_blob:
        base = os.path.splitext(os.path.basename(src_path))[0]
        p = os.path.join(keep_blob, base + ".stripped.code")
        open(p, "wb").write(new_blob)
        rep["blob_path"] = p

    config["DATA"]["data"] = new_blob
    config["DATA"]["length"] = len(new_blob)
    built = zoomzt2.ZD2.build(config)
    config["checksum"] = crc_of(built)
    built = zoomzt2.ZD2.build(config)

    # container self-check: parses, CRC valid, round-trips, DATA == blob
    rc = zoomzt2.ZD2.parse(built)
    assert bytes(rc["DATA"]["data"]) == new_blob, "DATA readback mismatch"
    assert zoomzt2.ZD2.build(rc) == built, "output does not round-trip"
    assert crc_of(built) == rc["checksum"], "CRC readback mismatch"

    open(dst_path, "wb").write(built)
    rep["container_old"] = len(data)
    rep["container_new"] = len(built)
    if not quiet:
        print(f"{os.path.basename(src_path)}: blob {rep['old_size']} -> "
              f"{rep['new_size']} B, container {len(data)} -> {len(built)} B "
              f"(-{100 * (len(data) - len(built)) / len(data):.1f}%), "
              f"stripped {', '.join(rep['stripped'])}")
        for c in rep["checks"]:
            print(f"  verify: {c}")
        print(f"  wrote {dst_path} (CRC 0x{rc['checksum']:08x})")
    return rep


def cross_check(zd2_path, elf_path):
    """Compare retained-section content of the blob stripped here against an
    independently stripped ELF (oracle, e.g. strip6x -p output)."""
    config = zoomzt2.ZD2.parse(open(zd2_path, "rb").read())
    mine, _ = strip_elf(bytes(config["DATA"]["data"]))
    a = ELFFile(io.BytesIO(mine))
    b = ELFFile(io.BytesIO(open(elf_path, "rb").read()))
    # index only non-empty sections (stock blobs carry duplicate empty
    # names, e.g. a second zero-size .text)
    bsecs = {s.name: s for s in b.iter_sections() if s["sh_size"]}
    same, diff = [], []
    # .dynamic and .shstrtab are layout-dependent by design (file-offset
    # tags / rebuilt string table); content equality is not expected
    LAYOUT_DEPENDENT = {".dynamic", ".shstrtab"}
    for s in a.iter_sections():
        if not s["sh_size"] or s["sh_type"] == "SHT_NOBITS" \
                or s.name in LAYOUT_DEPENDENT:
            continue
        o = bsecs.get(s.name)
        if o is None or o["sh_size"] == 0:
            diff.append(f"{s.name}: absent/empty in oracle")
        elif o.data() == s.data():
            same.append(s.name)
        else:
            diff.append(f"{s.name}: content differs "
                        f"({s['sh_size']} vs {o['sh_size']} B)")
    print(f"cross-check vs {os.path.basename(elf_path)}: "
          f"{len(same)} retained sections byte-identical: {', '.join(same)}")
    for d in diff:
        print(f"  DIFFERS: {d}")
    return not diff


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="+")
    ap.add_argument("-o", "--output")
    ap.add_argument("--keep-blob", metavar="DIR",
                    help="also write the bare stripped ELF here")
    ap.add_argument("--cross-check", metavar="ELF",
                    help="compare retained sections against this "
                         "independently stripped ELF instead of writing")
    ap.add_argument("--batch", action="store_true",
                    help="input = SRC_DIR OUT_DIR: strip+verify a corpus")
    args = ap.parse_args()

    if args.cross_check:
        ok = cross_check(args.input[0], args.cross_check)
        return 0 if ok else 1

    if args.batch:
        if len(args.input) != 2:
            ap.error("--batch needs SRC_DIR OUT_DIR")
        src, outdir = args.input
        os.makedirs(outdir, exist_ok=True)
        ok = skipped = 0
        tot_old = tot_new = 0
        fails = []
        for path in sorted(glob.glob(os.path.join(src, "*.ZD2"))):
            name = os.path.basename(path)
            dst = os.path.join(outdir, name.replace(".ZD2", ".stripped.ZD2"))
            try:
                rep = strip_container(path, dst, quiet=True)
                ok += 1
                tot_old += rep["container_old"]
                tot_new += rep["container_new"]
            except ValueError as e:
                skipped += 1
                print(f"SKIP {name}: {e}")
            except AssertionError as e:
                fails.append(f"{name}: VERIFY FAIL {e}")
        for f in fails:
            print(f)
        print(f"\nbatch: {ok} stripped+verified, {skipped} skipped, "
              f"{len(fails)} verify-failures")
        if ok:
            print(f"container bytes {tot_old} -> {tot_new} "
                  f"(-{100 * (tot_old - tot_new) / tot_old:.1f}%)")
        return 1 if fails else 0

    if not args.output:
        ap.error("-o OUT.ZD2 required (single-file mode)")
    strip_container(args.input[0], args.output, keep_blob=args.keep_blob)
    return 0


if __name__ == "__main__":
    sys.exit(main())
