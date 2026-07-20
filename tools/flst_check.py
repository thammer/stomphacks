#!/usr/bin/env python3
"""flst_check.py - validate a pedal effect list (FLST_SEQ.ZT2) OFFLINE.

FLST_SEQ.ZT2 is the pedal's master effect list. The pedal's boot code reads
it before anything else, so a missing, truncated or malformed FLST can leave
the pedal unable to boot (that is exactly how a pedal was lost on 2026-07-17,
see SAFETY.md "An interrupted file transfer can brick"). Because the stakes
of writing a bad FLST are so high, every FLST this project is about to upload
is validated HERE, on the PC, before a single byte goes to the pedal.

This tool never touches the pedal. It parses with the same grammar zoomzt2
uses (zoom-zt2/zoomzt2.py, the `ZT2` construct), so "valid here" means
"exactly what the proven tooling would produce".

What "valid" means, precisely:
  1. The file is exactly 8502 bytes (the ZT2 grammar's fixed padded size), OR
     LARGER with every byte past 8502 zero - the NATIVE size of a list this
     tooling has never rewritten (a factory-fresh MS-70CDR+ ships 12324
     bytes; the real catalogue data ends far below 8502 and everything after
     it is zero padding). A larger file with a NONZERO byte past 8502 is
     refused: the 8502-byte grammar cannot represent it without losing data.
  2. The first 8502 bytes parse with the ZT2 grammar.
  3. Rebuilding the parse result reproduces those 8502 bytes BYTE-FOR-BYTE
     (round-trip identity - proves the grammar models every byte).
  4. Every effect entry's id has a group byte matching its containing group.

Native-size preservation: grammar edits always BUILD exactly 8502 bytes, so
an edited list is padded back to the length of the list the pedal actually
held (`pad_to_native`) before it is written. An install on a factory-fresh
12324-byte pedal therefore writes 12324 bytes back - the file's size never
changes under this tooling (allocate exactly the source catalogue's size,
never a hardcoded constant).

Usage (from the repo root; never needs the pedal):
    .venv/bin/python3 tools/flst_check.py <FLST_SEQ.ZT2>
    .venv/bin/python3 tools/flst_check.py <FLST_SEQ.ZT2> --simulate-add <FILE.ZD2>
    .venv/bin/python3 tools/flst_check.py <FLST_SEQ.ZT2> --simulate-remove <NAME.ZD2>
  Windows: .venv/Scripts/python.exe tools/flst_check.py ...

`--simulate-add` / `--simulate-remove` run the SAME catalog edit an install or
uninstall would perform, entirely offline, then validate the result and show
exactly which entries changed. Run this before any real install if you want to
see the FLST change it will make. Exit code 0 = valid, 1 = NOT valid.

The library entry points (used by tools-pedal/pedal_diy.py before an upload):
  validate_flst(data)          -> (ok, problems, entries)
  pad_to_native(new_data, native_size) -> bytes (never truncates)
  diff_entries(old, new)       -> (added, removed) entry lists
  expect_single_add(old, new, filename)    -> (ok, problems)
  expect_single_remove(old, new, filename) -> (ok, problems)
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "zoom-zt2"))

import zoomzt2

FLST_SIZE = 8502   # what the ZT2 grammar Padded(8502, ...) BUILDS - the
                   # canonical size; a pedal's native file may be larger
                   # (zero-tailed), see validate_flst/pad_to_native


def parse_entries(data):
    """Parse FLST bytes -> list of (group:int, filename:str, version:str,
    id:int, installed:int), in file order. Only the canonical first 8502
    bytes are parsed (a native-size list's tail is zero padding by
    definition; validate_flst proves that before anything trusts it).
    Raises on parse failure."""
    config = zoomzt2.ZT2.parse(bytes(data[:FLST_SIZE]))
    entries = []
    for group in config[1]:
        for effect in group["effects"]:
            entries.append((int(group["group"]), str(effect["effect"]),
                            str(effect["version"]), int(effect["id"]),
                            int(effect["installed"])))
    return entries


def validate_flst(data):
    """Return (ok:bool, problems:list[str], entries or None).

    The four checks in the module docstring. Fail-closed: any exception
    from the grammar is reported as a problem, never raised to the caller.
    """
    problems = []
    if len(data) < FLST_SIZE:
        problems.append("size is %d bytes, expected %d or more - a SHORTER "
                        "file cannot be a complete effect list"
                        % (len(data), FLST_SIZE))
        return False, problems, None
    if len(data) > FLST_SIZE:
        tail_nonzero = sum(1 for b in bytes(data[FLST_SIZE:]) if b)
        if tail_nonzero:
            problems.append(
                "size is %d bytes and %d byte(s) past offset %d are NONZERO "
                "- the %d-byte grammar cannot represent them, so any edit "
                "would LOSE DATA; refusing (a native-size list is accepted "
                "only when everything past %d is zero padding)"
                % (len(data), tail_nonzero, FLST_SIZE, FLST_SIZE, FLST_SIZE))
            return False, problems, None
        # Native size (e.g. the factory-fresh MS-70CDR+ 12324-byte list):
        # same catalogue, longer all-zero tail. Valid; writes preserve this
        # length (pad_to_native), never truncate it to the grammar's 8502.
    canon = bytes(data[:FLST_SIZE])
    try:
        entries = parse_entries(canon)
    except Exception as e:
        problems.append("does not parse with the ZT2 grammar: %s" % e)
        return False, problems, None
    if not entries:
        problems.append("parses but contains ZERO effect entries")
    try:
        rebuilt = zoomzt2.ZT2.build(zoomzt2.ZT2.parse(canon))
    except Exception as e:
        problems.append("parses but does not REBUILD: %s" % e)
        return False, problems, entries
    if rebuilt != canon:
        problems.append("round-trip is NOT byte-identical (parse+build "
                        "changed %d of %d bytes) - the grammar does not "
                        "fully model this file; do not upload anything "
                        "derived from it"
                        % (sum(1 for a, b in zip(rebuilt, canon) if a != b),
                           len(canon)))
    for group, name, version, eid, installed in entries or []:
        if ((eid >> 24) & 0xFF) != group:
            problems.append("entry '%s': id 0x%08x group byte %d != "
                            "containing group %d"
                            % (name, eid, (eid >> 24) & 0xFF, group))
    return not problems, problems, entries


def pad_to_native(new_data, native_size):
    """Pad a grammar-built (8502-byte) effect list back to `native_size` -
    the length of the list the pedal actually held. The grammar always
    BUILDS exactly 8502 bytes, but a pedal this tooling has never written
    holds a larger, zero-tailed list (factory-fresh MS-70CDR+: 12324 bytes);
    writing 8502 back would silently shrink the file. That shrink is proven
    benign, but needless - preserving the source length means the file's
    size never changes under this tooling. Raises ValueError rather than
    ever truncating."""
    new_data = bytes(new_data)
    if native_size < len(new_data):
        raise ValueError(
            "refusing to truncate the effect list: computed %d bytes, "
            "native size only %d" % (len(new_data), native_size))
    return new_data + b"\x00" * (native_size - len(new_data))


def diff_entries(old_data, new_data):
    """Entry-level diff of two VALID FLSTs -> (added, removed)."""
    old = parse_entries(old_data)
    new = parse_entries(new_data)
    added = [e for e in new if e not in old]
    removed = [e for e in old if e not in new]
    return added, removed


def expect_single_add(old_data, new_data, filename):
    """Confirm new_data is old_data plus EXACTLY one entry for `filename`
    and nothing else changed. Returns (ok, problems)."""
    problems = []
    ok, val_problems, _ = validate_flst(new_data)
    if not ok:
        return False, ["new FLST is not valid: " + p for p in val_problems]
    added, removed = diff_entries(old_data, new_data)
    if removed:
        problems.append("entries REMOVED that should not be: %s"
                        % [e[1] for e in removed])
    if len(added) != 1:
        problems.append("expected exactly 1 added entry, found %d: %s"
                        % (len(added), [e[1] for e in added]))
    elif added[0][1] != filename:
        problems.append("added entry is '%s', expected '%s'"
                        % (added[0][1], filename))
    return not problems, problems


def expect_single_remove(old_data, new_data, filename):
    """Confirm new_data is old_data minus EXACTLY the entry for `filename`."""
    problems = []
    ok, val_problems, _ = validate_flst(new_data)
    if not ok:
        return False, ["new FLST is not valid: " + p for p in val_problems]
    added, removed = diff_entries(old_data, new_data)
    if added:
        problems.append("entries ADDED that should not be: %s"
                        % [e[1] for e in added])
    if len(removed) != 1:
        problems.append("expected exactly 1 removed entry, found %d: %s"
                        % (len(removed), [e[1] for e in removed]))
    elif removed[0][1] != filename:
        problems.append("removed entry is '%s', expected '%s'"
                        % (removed[0][1], filename))
    return not problems, problems


def _report(data, label):
    ok, problems, entries = validate_flst(data)
    print("%s: %d bytes, %s" % (label, len(data),
                                "VALID" if ok else "NOT VALID"))
    if ok and len(data) > FLST_SIZE:
        print("  native-size list: %d canonical + %d zero-padding bytes "
              "(a write preserves this length)"
              % (FLST_SIZE, len(data) - FLST_SIZE))
    if entries is not None:
        groups = sorted(set(e[0] for e in entries))
        print("  %d effect entries in %d groups %s"
              % (len(entries), len(groups), groups))
    for p in problems:
        print("  problem: %s" % p)
    return ok


def main(argv):
    if not argv or argv[0].startswith("-"):
        sys.exit(__doc__)
    with open(argv[0], "rb") as f:
        data = f.read()
    ok = _report(data, os.path.basename(argv[0]))

    if "--simulate-add" in argv or "--simulate-remove" in argv:
        if not ok:
            print("refusing to simulate an edit on an invalid input FLST")
            return 1
        pedal = zoomzt2.zoomzt2()   # grammar helpers only; never connects
        # Grammar edits run on the canonical first 8502 bytes; the result is
        # padded back to the input's own length - exactly what an install/
        # uninstall does, so the simulation shows the real write.
        canon = bytes(data[:FLST_SIZE])
        if "--simulate-add" in argv:
            zd2 = argv[argv.index("--simulate-add") + 1]
            new = pad_to_native(pedal.add_effect_from_filename(canon, zd2),
                                len(data))
            name = os.path.basename(zd2)
            print()
            new_ok = _report(new, "after add_effect(%s)" % name)
            good, problems = expect_single_add(data, new, name)
            for p in problems:
                print("  problem: %s" % p)
            added, removed = diff_entries(data, new)
            for e in added:
                print("  added: group=%d %s v%s id=0x%08x installed=%d" % e)
            ok = new_ok and good
        if "--simulate-remove" in argv:
            name = os.path.basename(argv[argv.index("--simulate-remove") + 1])
            new = pad_to_native(pedal.remove_effect(canon, name), len(data))
            print()
            new_ok = _report(new, "after remove_effect(%s)" % name)
            good, problems = expect_single_remove(data, new, name)
            for p in problems:
                print("  problem: %s" % p)
            removed = diff_entries(data, new)[1]
            for e in removed:
                print("  removed: group=%d %s v%s id=0x%08x installed=%d" % e)
            ok = new_ok and good

    print()
    print("flst_check: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
