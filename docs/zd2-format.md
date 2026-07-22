# The ZD2 container file format

ZD2 is the effect file format for Zoom's MS+ pedal generation (MS-50G+,
MS-60B+, MS-70CDR+, MS-80IR+, MS-200D+) and the G5-family. A `.ZD2` file is
a container: metadata + icon + descriptive text + an embedded ELF binary
holding the DSP code that actually runs on the pedal.

The authoritative parser is the Construct grammar `ZD2` in
[mungewell/zoom-zt2](https://github.com/mungewell/zoom-zt2)'s `zoomzt2.py`,
with `decode_effect.py` as the extract/rebuild tool. Everything below is
from those sources plus hex-level verification done independently;
anything not verified first-hand says so.

## Top-level layout

Magic `ZDLF` (yes, ZD2 files start with the bytes "ZDLF"), then a header,
then a sequence of tagged sections, each `4-byte TAG` + `uint32le length` +
payload.

Header fields, in order:

* `"ZDLF"` magic (4 bytes)
* `length` (uint32le)
* `checksum` (uint32le, CRC32, see below)
* `target` (uint32le bitfield): which pedal models may load the effect.
  Named bits are in zoomzt2.py's `targets` BitStruct (e.g. `ms-50g+` =
  0x0080).
* 73 unknown bytes
* `version` (4 ascii chars, e.g. "1.00")
* `group` (1 byte)
* `id` (uint32le), see [effect-ids.md](effect-ids.md)
* `name` (padded CString, 11 bytes)
* `groupname` (padded CString, 11 bytes)
* 3 unknown bytes

Sections, in order (confirmed by hex inspection of stock effects):

| Tag | Contents |
|---|---|
| `ICON` | BMP bitmap (the on-screen icon) |
| `TXJ1` | Japanese description text (Shift-JIS) |
| `TXE1` | English description text |
| `INFO` | 16-byte payload + a trailing float32le `dspload`, see below |
| `DATA` | The DSP code blob, a TI C6000 ELF file (optional; a few effects have none) |
| `INF2`, `CCOE`, `PRMJ`, `PRME` | Optional. `PRME`/`PRMJ` = English/Japanese parameter definitions; the payload is JSON text (`{"Parameters":[...]}`), although upstream tooling historically calls it "xml" |
| (trailer) | 16 unknown bytes |

### The INFO section

Two facts here matter a lot for DIY effects:

* `dspload` (the trailing float) is enforced by the pedal, on trust.
  Confirmed on hardware 2026-07-08: adding an effect is refused with
  "Process Overflow" when the declared values of the patch would sum
  past the budget (270 in raw units on the MS-70CDR+); the real measured
  load is not consulted. The Guitar Lab app displays `raw/2.7` as the
  percentage; the pedal itself shows no DSP numbers. So declare honestly:
  a too-low declaration lets users build patches that genuinely overflow,
  a too-high one wastes their budget. To pick a value, compare against a
  stock effect of similar complexity and err on the high side.
* `word0` (the payload's first u32) is an effect-class marker: 0 for
  normal knob effects (the vast majority), 1 for graphic-EQ slider banks,
  and 2 for the stock line selector, unique to it corpus-wide. The value
  2 is not cosmetic: the audio engine's output-bus gate keys on it
  (confirmed on hardware). The routing effects in this release declare 0
  on purpose: they are gate-independent and never rely on the amp-side
  mix (docs/chain-routing.md). Leave word0 at 0 unless you know exactly
  why you need the gate.

## Checksum

CRC32, polynomial `0x104c11db7`, reflected, init `0x00000000`, xorOut
`0xFFFFFFFF`, computed over `data[12:-16]`: everything after the 12-byte
magic+length+checksum prefix, excluding the final 16 trailer bytes. The
stored value equals `crcValue ^ 0xffffffff` under that recipe. Copy the
working code from `tools/zd2_roundtrip.py` or zoom-zt2's `decode_effect.py`
rather than re-deriving it.

## Display name: it's in the icon, not the name field

Confirmed on hardware: the pedal's effect browser shows the icon, and
the human-readable effect name is rendered as pixels into the icon
bitmap. It is NOT the ZD2 `name` field. Changing the `name` field changes
the catalogue entry but not what's on screen. To name your effect visibly,
draw it into the ICON section and the `.ZIC` (the build pipeline does this
for you from the manifest's `display` string).

## Companion files

* `.ZIC`: the icon file uploaded alongside the effect; the pedal
  associates it with the ZD2 by filename. A ZIC holds multiple 1-bit
  frames (typically 72x97 and 102x128). Converter: zoom-zt2's
  `convert_zic.py` (PNG in both directions, needs Pillow).
* `.ZIR`: impulse-response file, only for IR-based effects
  (`convert_zir.py`).
* `FLST_SEQ.ZT2`: not a companion of one effect but the pedal's on-device
  effect list; installing an effect requires adding it there, which
  zoomzt2.py's `--install` flow does.

## The DATA blob

`decode_effect.py -c OUT.code FILE.ZD2` writes exactly the DATA payload.
It is a little-endian 32-bit ELF for the TI C6000 DSP family
(`e_machine = 0x8C`). What's inside and how the firmware runs it is the
ABI doc's job: docs/zd2-abi.md.

## Round-trip fidelity

`tools/zd2_roundtrip.py` parses an effect with the zoomzt2 grammar,
rebuilds it, and byte-compares. Across the full MS-70CDR+ stock set (149
effects), 147 round-trip byte-exact and all 149 CRC-valid. The two
exceptions (`FLTERPPD.ZD2`, `TremRv.ZD2`) each contain a second `PRME`
section that the grammar models as one, so a rebuild silently drops the
duplicate. Rule: before repacking any effect, round-trip it first and only
proceed on a byte-exact result.

## Needs investigating

* The 73 unknown header bytes and the 16-byte trailer: semantics
  undecoded (the build pipeline ships the undecoded fields zeroed, which
  is hardware-proven to load and run; see the release-checklist caveat
  in [ip-and-licensing.md](ip-and-licensing.md)).
* The MS-70CDR+ bit in the `target` bitfield is inferable from stock
  files; document it explicitly here once double-checked.
