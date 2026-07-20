#!/usr/bin/env python3
"""Round-trip test for ZD2 containers using the zoom-zt2 Construct grammar.

For each file:
  1. Independently validate the header CRC32 (same recipe as
     zoom-zt2/decode_effect.py: poly 0x104c11db7, reflected, init 0,
     xorOut 0xFFFFFFFF, computed over bytes [12:-16], stored value compared
     as crcValue ^ 0xffffffff).
  2. Parse with zoomzt2.ZD2 and rebuild with ZD2.build(); compare the result
     byte-for-byte with the original file.

This proves (or disproves) that the parse/rebuild path is lossless before
trusting it to repack modified effects.

Usage (from project root):
  .venv/bin/python3 tools/zd2_roundtrip.py EFFECT.ZD2
  .venv/bin/python3 tools/zd2_roundtrip.py path/to/effects/*.ZD2

Pedal-safe: reads local files only (see tools/README.md).
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "zoom-zt2"))

import crcmod  # noqa: E402
import zoomzt2  # noqa: E402


def check_crc(data: bytes) -> bool:
    crc = crcmod.Crc(0x104C11DB7, rev=True, initCrc=0x00000000, xorOut=0xFFFFFFFF)
    crc.update(data[12:-16])
    stored = int.from_bytes(data[8:12], "little")
    return stored == (crc.crcValue ^ 0xFFFFFFFF)


def first_diff(a: bytes, b: bytes) -> int:
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return n  # lengths differ


def check_file(path: Path) -> str:
    data = path.read_bytes()

    crc_ok = check_crc(data)

    try:
        parsed = zoomzt2.ZD2.parse(data)
    except Exception as exc:  # noqa: BLE001 - report any parse failure
        return f"PARSE-FAIL {path.name}: {type(exc).__name__}: {exc}"

    try:
        rebuilt = zoomzt2.ZD2.build(parsed)
    except Exception as exc:  # noqa: BLE001
        return f"BUILD-FAIL {path.name}: {type(exc).__name__}: {exc}"

    if rebuilt == data:
        return f"OK        {path.name} ({len(data)} bytes, CRC {'ok' if crc_ok else 'BAD'})"

    off = first_diff(data, rebuilt)
    return (
        f"MISMATCH  {path.name}: sizes {len(data)}->{len(rebuilt)}, "
        f"first diff at 0x{off:x} "
        f"(orig {data[off:off+8].hex() if off < len(data) else 'EOF'} vs "
        f"rebuilt {rebuilt[off:off+8].hex() if off < len(rebuilt) else 'EOF'}, "
        f"CRC {'ok' if crc_ok else 'BAD'})"
    )


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    results = [check_file(Path(p)) for p in sys.argv[1:]]
    counts = {"OK": 0, "MISMATCH": 0, "PARSE-FAIL": 0, "BUILD-FAIL": 0}
    for line in results:
        print(line)
        counts[line.split()[0]] += 1

    print(
        f"\nSummary: {counts['OK']} ok, {counts['MISMATCH']} mismatch, "
        f"{counts['PARSE-FAIL']} parse-fail, {counts['BUILD-FAIL']} build-fail "
        f"of {len(results)} files"
    )
    return 0 if counts["OK"] == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
