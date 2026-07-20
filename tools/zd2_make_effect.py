#!/usr/bin/env python3
"""Build a ZD2 effect for the MS-70CDR+ from a C kernel and a manifest.json.

You write the audio kernel in C and describe the effect (name, id, knobs,
defaults) in a manifest.json; this tool compiles the kernel with the TI
C6000 compiler, assembles a complete ZD2 container and a ZIC icon, verifies
the result, and writes a build/REPORT.md. The container and the icon are
synthesized from scratch (tools/zd2_from_scratch.py); nothing from a stock
effect file enters the build. Local build and verification only: it talks to
no pedal and opens no MIDI port.

Usage (from the repo root):
    .venv/bin/python3 tools/zd2_make_effect.py effects/gain/manifest.json

The default and recommended scaffold is "cleanroom": the whole DSP blob is
generated from assembly written to the documented ABI
(tools/zd2_cleanroom.py, docs/zd2-abi.md). Its output contains no Zoom code
and is MIT-shareable. This is the path the gain example uses and the one
this repo supports out of the box.

There is a second scaffold, "bitcrush", which transforms a stock effect
binary instead of generating one from scratch. It needs Zoom-derived anchor
fragments that are not part of this repository, so it fails closed here, and
its output would contain Zoom code that cannot be shared
(docs/ip-and-licensing.md). The gain example does not use it.

What the cleanroom path does:
  1. Generates sh_params.h (a param -> coefficient-slot map) and compiles
     the kernel C.
  2. Generates the whole blob: the entry points, the parameter table, one
     edit handler per knob (get-param -> scale by 1/max -> coeff[slot]), the
     coefficient table seeded with the manifest defaults, and the init state
     memset sized to the manifest (up to 192 bytes).
  3. Synthesizes a from-scratch container template (tools/zd2_from_scratch.py)
     and assembles, links and packs via tools/build_zd2_from_asm.sh.
  4. Verifies: the loader accepts only ABS32/ABS_L16/ABS_H16 relocations, the
     parameter table matches the manifest, the compiled .audio passes the
     safe-DSP checks, the container CRC is valid and the file round-trips.
  5. Stamps identity (id/name), writes the PRME/PRMJ parameter JSON and the
     TXE1/TXJ1 description, strips DWARF/.symtab/.strtab from the shipped
     blob (manifest "strip", default true; the intermediates keep symbols),
     recomputes the CRC.
  6. Draws a fresh icon (all pixels drawn here) into a ZIC.
  7. Writes build/REPORT.md.

Coefficient slot policy (docs/zd2-abi.md): coeff[0] and coeff[1] are the
firmware bypass crossfade pair, and coeff[4] has an unknown host-side
writer, so none of those are assigned to a knob. Knobs get [3], [5], [6],
... [11], then [2], then [12], [13], [14]: up to 12, which is the pedal UI
maximum of 4 knobs across 3 pages.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import struct
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# TI C6000 install root: override with the ZOOM_TI_CGT env var if your
# compiler lives somewhere else. The default is the macOS 8.5.0 install path.
TI = Path(os.environ.get("ZOOM_TI_CGT",
                         "/Applications/ti/ti-cgt-c6000_8.5.0.LTS")) / "bin"
# venv interpreter differs by OS: Scripts/python.exe on Windows, bin/python3
# elsewhere.
PY = ROOT / (".venv/Scripts/python.exe" if os.name == "nt"
             else ".venv/bin/python3")
SCAFFOLD_DIR = ROOT / "rebuild" / "bitcrush"

sys.path.insert(0, str(ROOT / "zoom-zt2"))

# Loader-accepted relocation types (docs/zd2-abi.md): the loader accepts
# only these three, so anything else must fail the build.
OK_RELOC_TYPES = {0x01: "R_C6000_ABS32",
                  0x09: "R_C6000_ABS_L16",
                  0x0A: "R_C6000_ABS_H16"}

# Free coefficient slots, in assignment order. coeff[0]/[1] = bypass
# crossfade pair (host ramps), coeff[4] = unknown host writer; both are off
# limits. [2] comes after the [3,5..11] block: it is only free because the
# stock output-level registration is unhooked. [12,13,14] extend the order to
# the UI maximum of 12 params (4 knobs x 3 pages); slots >= 12 are plain
# effect-owned coeff words. Cleanroom scaffold only: its _Coe/init-memcpy are
# 16 words / 64 B; the bitcrush scaffold keeps the stock 12-word table, so it
# caps at the first 9.
FREE_COEFF_SLOTS = [3, 5, 6, 7, 8, 9, 10, 11, 2, 12, 13, 14]
BITCRUSH_MAX_PARAMS = 9

# Selector value-label tables (manifest "values"). The generated GetString
# function copies one fixed-stride entry to the host's display buffer, so the
# stride bounds the label length: 8 bytes = up to 7 chars + NUL. The pedal's
# own UI truncates names/values past 5 chars unless the parameter is
# selected, so 5 is the practical ceiling and longer labels only warn. Stock
# effects use the same fixed-stride idiom.
VALUE_STRIDE = 8          # bytes per label; 2 word-copies, word-aligned
VALUE_MAX_CHARS = VALUE_STRIDE - 1
VALUE_UI_CHARS = 5        # past this the pedal truncates the on-screen text

STATE_BYTES_FLOOR = 16    # stock init zeroes this much regardless
STATE_BYTES_MAX = 192     # proven floor of the firmware per-slot scratch
AUDIO_MAX_BYTES = 1472    # largest stock .audio (corpus census)
AUDIO_ADDR = 0x7800

CALLEE_SAVED = ["A10", "A11", "A12", "A13", "A14", "A15",
                "B10", "B11", "B12", "B13", "B14"]

# --------------------------------------------------------------------------
# Scaffold anchors (bitcrush scaffold only): literal fragments of a stock
# effect's disassembly used as exact-match search/replace anchors. They are
# Zoom-derived, so they are not part of this repository; they load lazily
# from a separate module, and the cleanroom scaffold path never touches them.
# --------------------------------------------------------------------------

_ANCHORS = None


def load_scaffold_anchors():
    """Lazy-load the Zoom-derived bitcrush-scaffold anchors (private)."""
    global _ANCHORS
    if _ANCHORS is None:
        p = SCAFFOLD_DIR / "scaffold_anchors.py"
        if not p.exists():
            sys.exit(f"scaffold 'bitcrush' needs {p} (Zoom-derived anchors, "
                     "not included in this repository; see "
                     "docs/ip-and-licensing.md); use "
                     '"scaffold": "cleanroom" instead')
        import importlib.util
        spec = importlib.util.spec_from_file_location("scaffold_anchors", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _ANCHORS = mod
    return _ANCHORS

# The host smoothing service rate stock VOL/Bal register (docs/zd2-abi.md
# ctx word 4): 0x3a77c5f5 ~= 9.456e-4 as float32.
SMOOTH_RATE_HEX = 0x3A77C5F5

# Generated per-param edit handler, host-smoothed variant: instead of
# writing coeff[slot] directly (which zipper-clicks on fast sweeps of
# level-like params), register the target with the host smoothing service at
# ctx word 4, the stock VOL/Bal mechanism (docs/zd2-abi.md). Serial code;
# the delay slots are audited like the direct variant.
SMOOTH_HANDLER_TEMPLATE = """
; --- generated edit handler: param '{pname}' (index {k}) -> coeff[{slot}]
;     HOST-SMOOTHED (ctx word 4 service, stock VOL/Bal mechanism)
\t.def Aw_P{k}_edit
Aw_P{k}_edit:
           MV.L2         B4,B5             ; B5 = ctx (survives __call_stub)
           MVK.S2        160,B4
           ADD.L2        B5,B4,B4
           LDW.D2T2      *B4[0],B0         ; host get-param function
           LDW.D1T1      *A4[1],A6         ; A6 = coefficient table
           MV.L1X        B3,A1             ; save the return address
           LDW.D1T1      *A4[0],A4         ; host-call arg (as stock handlers)
           MVK.L2        {k},B4            ; parameter index {k} = '{pname}'
           NOP           1
           CALLP.S2      __call_stub,B3
 ||        MV.L2         B0,B31            ; A4 = get-param(obj, {k})
           INTSP.L1      A4,A0             ; returned value 0..{pmax} -> float
           MVKL.S1       0x{scale:08x},A3
           MVKH.S1       0x{scale:08x},A3  ; 1/{pmax} as float32
           LDW.D2T2      *+B5[{svc}],B0        ; host smooth/ramp fn (ctx word {svc})
           MPYSP.M1      A0,A3,A0          ; target = value/{pmax} -> 0..1
           MVKL.S1       0x{rate:08x},A3
           MVKH.S1       0x{rate:08x},A3   ; smoothing rate
           MVK.S1        {off},A5          ; byte offset of coeff[{slot}]
           MV.L2X        A0,B4             ; arg2 = target (float)
           ADD.L1        A6,A5,A4          ; arg1 = &coeff[{slot}]
           MV.L1         A3,A6             ; arg3 = rate
           MV.L2         B0,B31
           CALLP.S2      __call_stub,B3    ; smooth(&coeff[{slot}], target, rate)
           BNOP.S2X      A1,5              ; return
"""

# Generated value-display function for a selector param (manifest "values").
# The param-table w9 slot points here; the host calls it to render a click as
# on-screen text. The ABI (confirmed by disassembly and a corpus check):
#   A4 = the raw parameter value (click)     B4 = destination buffer
#   A6 = destination buffer length (unused here: the copy is a fixed
#        {stride} bytes, well inside the >=16-byte buffer stock functions write)
#   return via B3. Pure function: no ctx, no host services, no other params.
# It uses the fixed-stride string-table idiom.
# The value is clamped to the last entry before indexing, so a stale or
# out-of-range click can never read past the table (the host should never send
# one; this is belt-and-braces, and it is free: this runs on the UI thread,
# not in the audio loop).
GETSTRING_TEMPLATE = """
; --- generated value-display fn: param '{pname}' (index {k}), w9 -> here
;     renders click 0..{vmax} as {labels}
\t.def Aw_P{k}_str
Aw_P{k}_str:
           MVK.S1        {vmax},A0         ; A0 = last valid click
           CMPGTU.L1     A4,A0,A1          ; A1 = (click > last), unsigned
           MVKL.S2       Aw_P{k}_tab,B5
           MVKH.S2       Aw_P{k}_tab,B5    ; B5 = &label table
 [A1]      MV.L1         A0,A4             ; clamp out-of-range to the last
           SHL.S1        A4,{shift},A4     ; A4 = click * {stride} (byte offset)
           ADD.L2X       A4,B5,B5          ; B5 = &table[click]
           LDW.D2T1      *+B5[0],A5        ; the label, {stride} bytes,
           LDW.D2T1      *+B5[1],A7        ; NUL-padded in the table
           NOP           5
           STW.D2T1      A5,*+B4[0]        ; -> host display buffer
           STW.D2T1      A7,*+B4[1]
           BNOP.S2       B3,5              ; return
"""

# Generated per-param edit handler: the hardware-proven edit-handler pattern
# (get-param -> int-to-float -> scale -> store) with three holes: param index,
# 1/max scale, coeff slot.
HANDLER_TEMPLATE = """
; --- generated edit handler: param '{pname}' (index {k}) -> coeff[{slot}] ---
; the hardware-proven edit-handler pattern
	.def Aw_P{k}_edit
Aw_P{k}_edit:
           MV.L2         B4,B0             ; B0 = ctx
           MVK.S2        160,B4
           ADD.L2        B0,B4,B4
           LDW.D2T2      *B4[0],B0         ; host get-param function
           LDW.D1T1      *A4[1],A6         ; A6 = coefficient table
           MV.L1X        B3,A1             ; save the return address
           LDW.D1T1      *A4[0],A4         ; host-call arg (as stock handlers)
           MVK.L2        {k},B4            ; parameter index {k} = '{pname}'
           NOP           1
           CALLP.S2      __call_stub,B3
 ||        MV.L2         B0,B31
           INTSP.L1      A4,A0             ; returned value 0..{pmax} -> float
           MVKL.S1       0x{scale:08x},A3
           MVKH.S1       0x{scale:08x},A3  ; 1/{pmax} as float32
           NOP           1
           MPYSP.M1      A0,A3,A0          ; value/{pmax} -> 0..1
           NOP           3
           BNOP.S2X      A1,4              ; return (STW rides the last slot)
           STW.D1T1      A0,*+A6[{slot}]   ; coeff[{slot}] = 0.0 .. 1.0
"""

# Stock VOL curve handler (decoded stock behavior, independent implementation):
# Level-like knobs adopt the decoded stock curve:
# target = v/80 for v <= 80, 1 + (v-80)/40 above (VOLUME_0_80_100 replica,
# hardware-confirmed end-to-end via AwCrvPrb). Unity at the stock default
# 80, +3.52 dB (1.5x) ceiling at 100, mute at 0. The kernel consumes the
# coeff DIRECTLY as an amplitude (no 2x scaling, no kernel one-pole for
# these params). Branchless: predicated select, serial, delay slots
# audited line-by-line like the proven templates.
CURVE_HEAD = """
; --- generated edit handler: param '{pname}' (index {k}) -> coeff[{slot}]
;     STOCK VOL CURVE (unity at 80, 1.5x at 100) {tail_kind}
\t.def Aw_P{k}_edit
Aw_P{k}_edit:
           MV.L2         B4,B5             ; B5 = ctx (survives __call_stub)
           MVK.S2        160,B4
           ADD.L2        B5,B4,B4
           LDW.D2T2      *B4[0],B0         ; host get-param function
           LDW.D1T1      *A4[1],A6         ; A6 = coefficient table
           MV.L1X        B3,A1             ; save the return address
           LDW.D1T1      *A4[0],A4         ; host-call arg (as stock handlers)
           MVK.L2        {k},B4            ; parameter index {k} = '{pname}'
           NOP           1
           CALLP.S2      __call_stub,B3
 ||        MV.L2         B0,B31            ; A4 = get-param(obj, {k})
"""

CURVE_COMPUTE_SECTION = """\
; stock VOL curve: target = v<=80 ? v/80 : 1 + (v-80)/40 (zd2-abi.md, hw ok)
           INTSP.L1      A4,A7             ; f = (float)v            (+4)
           MVK.S1        81,A8
           CMPLT.L1      A4,A8,A0          ; A0 = (v < 81)
           MVKL.S1       0x3c4ccccd,A3
           MVKH.S1       0x3c4ccccd,A3     ; 1/80
           NOP           1
           MPYSP.M1      A7,A3,A9          ; a = f/80                (+4)
           MVKL.S1       0x42a00000,A3
           MVKH.S1       0x42a00000,A3     ; 80.0f
           SUBSP.L1      A7,A3,A7          ; f - 80                  (+4)
           MVKL.S1       0x3ccccccd,A3
           MVKH.S1       0x3ccccccd,A3     ; 1/40
           NOP           1
           MPYSP.M1      A7,A3,A7          ; (f-80)/40               (+4)
           MVKL.S1       0x3f800000,A3
           MVKH.S1       0x3f800000,A3     ; 1.0f
           NOP           1
           ADDSP.L1      A7,A3,A7          ; b = 1 + (f-80)/40       (+4)
           NOP           3
   [!A0]   MV.L1         A7,A9             ; target = b when v >= 81
"""

CURVE_SMOOTH_TAIL = """\
; register with the host smooth/ramp service (ctx word {svc}; REQUIRES the
; init edit CALLPs, enforced by load_manifest; docs/zd2-abi.md words 3/4)
           LDW.D2T2      *+B5[{svc}],B0    ; host smooth/ramp fn (ctx word {svc})
           MVKL.S1       0x{rate:08x},A3
           MVKH.S1       0x{rate:08x},A3   ; smoothing rate
           MVK.S1        {off},A5          ; byte offset of coeff[{slot}]
           MV.L2X        A9,B4             ; arg2 = target (curved amplitude)
           ADD.L1        A6,A5,A4          ; arg1 = &coeff[{slot}]
           MV.L1         A3,A6             ; arg3 = rate
           MV.L2         B0,B31
           CALLP.S2      __call_stub,B3    ; smooth(&coeff[{slot}], target, rate)
           BNOP.S2X      A1,5              ; return
"""

CURVE_DIRECT_TAIL = """\
           BNOP.S2X      A1,4              ; return (STW rides the last slot)
           STW.D1T1      A9,*+A6[{slot}]   ; coeff[{slot}] = curved amplitude
"""


def f32_hex(x: float) -> int:
    return struct.unpack("<I", struct.pack("<f", x))[0]


def die(msg: str) -> None:
    sys.exit(f"zd2_make_effect: ERROR: {msg}")


def warn(msg: str) -> None:
    print(f"  !! WARNING: {msg}")


def run(cmd, **kw):
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def _bash():
    """Resolve a POSIX bash for build_zd2_from_asm.sh. On Windows prefer Git
    Bash (NOT the WSL `bash.exe` on PATH, which runs a Linux userland that
    can't see the Windows-side venv/toolchain paths); override with ZOOM_BASH.
    On macOS/Linux this is just `bash`."""
    if os.name == "nt":
        override = os.environ.get("ZOOM_BASH")
        if override:
            return override
        for cand in (r"C:\Program Files\Git\bin\bash.exe",
                     r"C:\Program Files\Git\usr\bin\bash.exe"):
            if Path(cand).exists():
                return cand
    return "bash"


# --------------------------------------------------------------------------
# manifest
# --------------------------------------------------------------------------

def load_manifest(path: Path) -> dict:
    m = json.loads(path.read_text())
    problems = []

    def need(key, typ):
        if key not in m or not isinstance(m[key], typ):
            problems.append(f"missing or mistyped field '{key}'")

    need("name", str); need("filename", str); need("id", str)
    need("kernel", str); need("params", list)
    if problems:
        die("; ".join(problems))

    m.setdefault("scaffold", "cleanroom")
    if m["scaffold"] not in ("bitcrush", "cleanroom"):
        problems.append("scaffold must be 'cleanroom' (default) or 'bitcrush'")
    # symbol stem for the generated code (cleanroom names its own symbols)
    m["sym"] = re.sub(r"[^A-Za-z0-9]", "", m["name"])
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", m["sym"]):
        problems.append(f"name '{m['name']}' must start with a letter "
                        "(symbol stem)")

    if not re.fullmatch(r"[0-9a-fA-F]{8}", m["id"]):
        problems.append(f"id '{m['id']}' must be 8 hex digits")
    else:
        idv = int(m["id"], 16)
        if idv < 0x07000F07 and not m.get("allow_any_id"):
            problems.append(
                f"id {m['id']} < 07000f07: ids below are stock/used/"
                "reserved; set allow_any_id to override")
    if not re.fullmatch(r"[A-Z0-9_]{1,8}", m["filename"]):
        problems.append("filename must be 1-8 chars of A-Z 0-9 _")
    if not (1 <= len(m["name"]) <= 10) or not m["name"].isascii():
        problems.append("name must be 1-10 ascii chars")
    # param cap: 12 on cleanroom (the pedal-UI maximum, 4 knobs x 3
    # pages; needs the 16-word _Coe), 9 on the bitcrush scaffold (its stock
    # 12-word _Coe/48-B memcpy are untouched by the transform).
    free_slots = (FREE_COEFF_SLOTS if m["scaffold"] == "cleanroom"
                  else FREE_COEFF_SLOTS[:BITCRUSH_MAX_PARAMS])
    if not (1 <= len(m["params"]) <= len(free_slots)):
        problems.append(f"1..{len(free_slots)} params supported on the "
                        f"{m['scaffold']} scaffold")
    for p in m["params"]:
        nm = p.get("name", "")
        if not (1 <= len(nm) <= 12) or not nm.isascii():
            problems.append(f"param name '{nm}' must be 1-12 ascii chars")
        # a selector's max IS its label count - 1; derive it before the range
        # check so the manifest never has to repeat itself (and cannot disagree
        # with the list). An explicit max is still honoured + cross-checked in
        # the "values" block below.
        if (isinstance(p.get("values"), list) and p["values"]
                and p.get("max") is None):
            p["max"] = len(p["values"]) - 1
        if not isinstance(p.get("max"), int) or not (1 <= p["max"] <= 1000):
            problems.append(f"param '{nm}': integer max 1..1000 required")
        if not isinstance(p.get("default"), int) \
                or not (0 <= p["default"] <= p.get("max", 0)):
            problems.append(f"param '{nm}': integer default 0..max required")
        # "smooth": true -> host-smoothed handler at the stock rate;
        # a number -> host-smoothed at that rate (probe/tuning use);
        # absent/false -> direct write (right for discrete params).
        sm = p.get("smooth", False)
        if sm is True:
            p["smooth_hex"] = SMOOTH_RATE_HEX
        elif sm is False or sm is None:
            p["smooth_hex"] = None
        elif isinstance(sm, (int, float)) and 0 < sm < 1e6:
            p["smooth_hex"] = f32_hex(float(sm))
        else:
            problems.append(f"param '{nm}': smooth must be true/false or a "
                            "positive rate number")
        # "slot": explicit coefficient slot (must be one of the
        # scaffold's free slots; default = fill order, which starts at
        # [3]). Stock smooths continuously-edited levels only into coeff[2],
        # and the fill order never lands a single-param effect there, so this
        # is the escape hatch when you want a specific slot.
        sl = p.get("slot")
        if sl is not None and sl not in free_slots:
            problems.append(f"param '{nm}': slot must be one of "
                            f"{free_slots}")
        # "smooth_service": which ctx service word the smoothed handler
        # calls: 4 = the smooth service (stock VOL/Bal; default) or
        # 3 = the ramp service (identical 3-arg contract, docs/zd2-abi.md
        # words 3/4; stock drives coeff[3] levels through it at rate
        # 705.6, which you pass via "smooth": 705.6).
        svc = p.get("smooth_service", 4)
        if svc not in (3, 4):
            problems.append(f"param '{nm}': smooth_service must be 3 or 4")
        elif svc != 4 and p.get("smooth_hex") is None:
            problems.append(f"param '{nm}': smooth_service requires smooth")
        p["smooth_svc"] = svc
        # "handler_asm": "<file>" -> splice a hand-written edit handler for
        # this param instead of the generated template (an experiment escape
        # hatch). The file (path relative to the manifest) must define
        # Aw_P<index>_edit and honor the handler register contract (never
        # write A2/B2, and touch B5 only with ctx; the init keeps state
        # there across its CALLPs). Several params may share one file; it is
        # spliced once. Cleanroom scaffold only; the template landmark
        # verification is skipped for these params, so hand-review the
        # build's .text.dis before any upload.
        # "curve": "stock_vol" -> the handler maps the knob through the
        # decoded stock VOL curve (v/80 below 80, 1+(v-80)/40 above; unity
        # at 80, 1.5x at 100) and the kernel consumes coeff[slot] directly as
        # an amplitude. Combine with "smooth" for the full stock output-level
        # architecture.
        # "values": ["DUAL","COPY",...] -> a selector: the on-screen text for
        # each click, rendered by a generated GetString function hung off the
        # param-table w9 slot (the stock mechanism). Without it the pedal
        # shows the raw integer.
        # The DSP value is still the plain integer, so `max` is fixed by the
        # list length and the coefficient is knob/max as for any plain param.
        vals = p.get("values")
        if vals is not None:
            if (not isinstance(vals, list) or len(vals) < 2
                    or not all(isinstance(v, str) for v in vals)):
                problems.append(f"param '{nm}': values must be a list of 2+ "
                                "strings")
            elif m.get("scaffold") != "cleanroom":
                problems.append(f"param '{nm}': values requires the cleanroom "
                                "scaffold")
            else:
                want_max = len(vals) - 1
                if p.get("max", want_max) != want_max:
                    problems.append(
                        f"param '{nm}': max must be {want_max} (= len(values) "
                        f"- 1), got {p['max']}")
                p["max"] = want_max
                p["value_stride"] = VALUE_STRIDE   # gen_const_data reads this
                for v in vals:
                    if not v.isascii() or not v.isprintable() or not v:
                        problems.append(f"param '{nm}': value {v!r} must be "
                                        "non-empty printable ASCII")
                    elif len(v) > VALUE_MAX_CHARS:
                        problems.append(
                            f"param '{nm}': value {v!r} is {len(v)} chars; the "
                            f"{VALUE_STRIDE}-byte table stride allows "
                            f"{VALUE_MAX_CHARS} + NUL")
                    elif len(v) > VALUE_UI_CHARS:
                        warn(f"param '{nm}': value {v!r} is {len(v)} chars; "
                             f"the pedal truncates past {VALUE_UI_CHARS} unless "
                             "the parameter is selected")
                if p.get("curve") is not None:
                    problems.append(f"param '{nm}': values and curve are "
                                    "mutually exclusive")
                if p.get("smooth_hex") is not None:
                    problems.append(f"param '{nm}': values and smooth are "
                                    "mutually exclusive (a selector must step, "
                                    "not ramp, between values)")
        if len(nm) > VALUE_UI_CHARS:
            warn(f"param '{nm}': the NAME is {len(nm)} chars; the pedal "
                 f"truncates past {VALUE_UI_CHARS} unless selected")

        cv = p.get("curve")
        if cv is not None:
            if cv != "stock_vol":
                problems.append(f"param '{nm}': unknown curve '{cv}' "
                                "(supported: 'stock_vol')")
            elif p.get("max") != 100:
                problems.append(f"param '{nm}': curve 'stock_vol' requires "
                                "max == 100 (the stock VOL knob range)")
            elif m.get("scaffold") != "cleanroom":
                problems.append(f"param '{nm}': curve requires the "
                                "cleanroom scaffold")
        ha = p.get("handler_asm")
        p["handler_asm_path"] = None
        if ha is not None and cv is not None:
            problems.append(f"param '{nm}': curve and handler_asm are "
                            "mutually exclusive")
        if ha is not None:
            if not isinstance(ha, str):
                problems.append(f"param '{nm}': handler_asm must be a path")
            elif p.get("smooth_hex") is not None:
                problems.append(f"param '{nm}': handler_asm and smooth are "
                                "mutually exclusive")
            elif m.get("scaffold") != "cleanroom":
                problems.append(f"param '{nm}': handler_asm requires the "
                                "cleanroom scaffold")
            else:
                hp = (path.parent / ha).resolve()
                if not hp.is_file():
                    problems.append(f"param '{nm}': handler_asm not found: "
                                    f"{hp}")
                else:
                    p["handler_asm_path"] = str(hp)
    expl_slots = [p.get("slot") for p in m["params"]
                  if p.get("slot") is not None]
    if len(set(expl_slots)) != len(expl_slots):
        problems.append("duplicate explicit param 'slot' values")
    sb = m.setdefault("state_bytes", STATE_BYTES_FLOOR)
    if not isinstance(sb, int) or not (0 <= sb <= STATE_BYTES_MAX):
        problems.append(f"state_bytes must be 0..{STATE_BYTES_MAX} "
                        "(proven floor of the firmware scratch area)")
    elif m.get("scaffold") == "cleanroom" and sb > STATE_BYTES_MAX - 4:
        problems.append(f"state_bytes must be <= {STATE_BYTES_MAX - 4} on the "
                        "cleanroom scaffold: the init-written state guard "
                        "lives in the word after the declared state")
    # "init_edit_calls": stock-style init-time edit-handler CALLPs so the
    # user's params survive the _init re-run the host does on every chain
    # edit. Default ON for cleanroom; the stock scaffold path has no hook for
    # it (its init edit-call removal is part of the transform); migrate the
    # effect to cleanroom instead.
    iec = m.setdefault("init_edit_calls", m.get("scaffold") == "cleanroom")
    if not isinstance(iec, bool):
        problems.append("init_edit_calls must be true or false")
    elif iec and m.get("scaffold") != "cleanroom":
        problems.append("init_edit_calls requires the cleanroom scaffold "
                        "(stock scaffold keeps the edit-call removal)")
    # host smoothing depends on init_edit_calls: without the init-time edit
    # CALLPs the ctx[4] service degrades to instant writes (docs/zd2-abi.md
    # ctx word 4). Refuse the silent footgun.
    if not iec and any(p.get("smooth_hex") is not None for p in m["params"]):
        problems.append('"smooth" requires init_edit_calls: true (the '
                        "ctx[4] host smoothing service only ramps for "
                        "coeffs whose handler was CALLPed at init)")
    # Optional "dll_fields": {"<byte-offset>": <value>}: extra caller-
    # struct writes in the Dll stub (cleanroom scaffold only). Stock
    # delays/reverbs write +20/+24 (+28/+8 for some); the meaning is under
    # investigation, and probe effects use this hook to mimic them.
    # Offsets 0/4/12 are the documented fields and are banned.
    df_in = m.get("dll_fields", {})
    df = {}
    if not isinstance(df_in, dict):
        problems.append("dll_fields must be an object of offset -> value")
    else:
        for k, v in df_in.items():
            try:
                off = int(k, 0) if isinstance(k, str) else int(k)
                val = int(v, 0) if isinstance(v, str) else int(v)
            except (TypeError, ValueError):
                problems.append(f"dll_fields entry {k!r}: {v!r} not integers")
                continue
            if off in (0, 4, 12) or off % 4 or not (8 <= off <= 60):
                problems.append(f"dll_fields offset {off}: must be a word "
                                "offset in 8..60, not 0/4/12")
            if not (0 <= val <= 0xffffffff):
                problems.append(f"dll_fields +{off}: value out of u32 range")
            df[off] = val
        if df and m.get("scaffold") != "cleanroom":
            problems.append("dll_fields requires the cleanroom scaffold")
    m["dll_fields"] = df
    # Optional big-kernel keys (cleanroom only). "kernel_section": "text"
    # places the kernel in .text instead of the 1472-B .audio convention,
    # stock-precedented (DELAY_C's audio fn is in .text; the big reverbs run
    # entirely from .text, up to 72 KB). "allow_stack": true relaxes the
    # SAFE-DSP no-stack/no-callee-saved rule for the kernel, also stock-
    # precedented (B_CHORUS's audio fn opens with ADDK -264,B15). Both are
    # hardware-proven (docs/zd2-abi.md).
    ks = m.setdefault("kernel_section", "audio")
    if ks not in ("audio", "text"):
        problems.append('kernel_section must be "audio" or "text"')
    elif ks == "text" and m.get("scaffold") != "cleanroom":
        problems.append('kernel_section "text" requires the cleanroom scaffold')
    ast = m.setdefault("allow_stack", False)
    if not isinstance(ast, bool):
        problems.append("allow_stack must be true or false")
    elif ast and m.get("scaffold") != "cleanroom":
        problems.append("allow_stack requires the cleanroom scaffold")
    # "info_word0": <int> overrides the container INFO chunk's leading
    # u32 (finalize_container patches it). Across the stock corpus this word
    # is a UI/effect-class enum: 0 = normal (144 effects), 1 = graphic-EQ
    # slider bank (the 4 GEQ effects), 2 = LINE SEL alone. Leave it 0 for an
    # ordinary effect.
    iw0 = m.get("info_word0")
    if iw0 is not None and not (isinstance(iw0, int)
                                and 0 <= iw0 <= 0xFFFFFFFF):
        problems.append("info_word0 must be a u32 integer (0..0xFFFFFFFF)")
    # "init_onf_call": have _init also tail-call the _onf handler on every
    # re-init (chain edit). A word0=2 router's ctx[2]->amp gate can go stale
    # across a chain edit; re-running _onf at init re-asserts the
    # router/bypass state. Defaults true for word0=2 routers (they need it),
    # false otherwise (ordinary effects do not have the quirk). Cleanroom
    # only; register-safe (A2/B5/B2 survive the edit CALLPs).
    ionf = m.setdefault("init_onf_call", m.get("info_word0") == 2)
    if not isinstance(ionf, bool):
        problems.append("init_onf_call must be true or false")
    elif ionf and m.get("scaffold") != "cleanroom":
        problems.append("init_onf_call requires the cleanroom scaffold")
    # "descriptor_w10": the effect-descriptor (param-table entry 1) word
    # 10. Across the stock corpus this is a per-effect DSP quantity that
    # tracks reverb/delay tail magnitude; it is 0 in 68 of the 149 stock
    # effects. Default 0 (correct for a non-reverb effect); a reverb/delay
    # may set a real value.
    w10 = m.setdefault("descriptor_w10", 0)
    if not (isinstance(w10, int) and 0 <= w10 <= 0xFFFFFFFF):
        problems.append("descriptor_w10 must be a u32 integer (0..0xFFFFFFFF)")
    # "pic_geometry": [w, h] of the in-blob effectTypeImageInfo pic. Blank
    # on cleanroom builds (the pedal renders the .ZIC), so [w, h] is a
    # cosmetic interop dimension; the stock corpus uses several geometries,
    # 23x30 most common. Default [24,30] (loads fine); the blank pic buffer
    # is sized round_up_to_4(ceil(w/8)*h).
    pg = m.setdefault("pic_geometry", [24, 30])
    if not (isinstance(pg, list) and len(pg) == 2
            and all(isinstance(v, int) and 1 <= v <= 256 for v in pg)):
        problems.append("pic_geometry must be [w, h], each an int 1..256")
    else:
        m["pic_bytes"] = ((((pg[0] + 7) // 8) * pg[1]) + 3) & ~3
    # "dspload": the INFO chunk trailing float32. The pedal gates patch
    # composition on the DECLARED value against a budget of 270 raw; Guitar
    # Lab displays raw/2.7 as a % (the pedal shows no numbers). Default None
    # keeps the template default (8.0 raw = 3.0%); set it to declare an
    # honest load. finalize_container patches it.
    dl = m.get("dspload")
    if dl is not None and not (isinstance(dl, (int, float))
                               and 0 <= dl <= 270):
        problems.append("dspload must be a number 0..270 raw "
                        "(the proven budget; Guitar Lab shows raw/2.7 %)")
    # "strip": ship the container blob with .debug_*/.symtab/.strtab
    # removed (loader-safe, hardware-proven). Default ON so DIY effects ship
    # stripped; set false only when the shipped file itself must carry
    # symbols. Intermediates (.out, _rebuilt.ZD2) always keep their symbols
    # for the verify stages and debugging.
    if not isinstance(m.setdefault("strip", True), bool):
        problems.append("strip must be a boolean")
    # "const_blob": {"symbol": .., "words": .., "init": <file, optional>}
    # declares a named const data reserve (lookup tables, wavetables, patch
    # blobs) the cleanroom scaffold emits at the END of its own .const
    # input section. Rationale: a large kernel-C const
    # array forms its own .const input section and lnk6x packs same-name
    # input sections by DESCENDING SIZE, so anything bigger than the
    # scaffold's .const lands AHEAD of the param table, breaking the
    # loader contract (verify_cleanroom "table not first"). Inside the
    # scaffold's single input section, content is file-ordered: table
    # first, any blob size. The kernel declares
    # `extern const uint32_t <symbol>[];`. "init" (path relative to the
    # manifest) preloads the leading words little-endian (padded to a
    # word boundary); the remainder is zero-filled.
    CONST_BLOB_MAX_WORDS = 16384          # 64 KB, below the stock 72-KB
    cb_in = m.get("const_blob")           # reverb blobs, far above need
    if cb_in is not None:
        cb_problems = []
        if m.get("scaffold") != "cleanroom":
            cb_problems.append("const_blob requires the cleanroom scaffold")
        if not isinstance(cb_in, dict):
            cb_problems.append("const_blob must be an object")
            cb_in = {}
        sym = cb_in.get("symbol")
        words = cb_in.get("words")
        init = cb_in.get("init")
        if not (isinstance(sym, str)
                and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", sym)):
            cb_problems.append("const_blob.symbol must be a C identifier")
        elif (sym.startswith(("Aw_P", "Fx_SFX_", "_Fx_SFX_", "__",
                              "picEffectType_"))
              or sym in ("effectTypeImageInfo", m["sym"])):
            cb_problems.append(f"const_blob.symbol '{sym}' collides with "
                               "generated scaffold symbols")
        if not (isinstance(words, int)
                and 1 <= words <= CONST_BLOB_MAX_WORDS):
            cb_problems.append("const_blob.words must be an int "
                               f"1..{CONST_BLOB_MAX_WORDS}")
        init_words = []
        if init is not None and not cb_problems:
            if not isinstance(init, str):
                cb_problems.append("const_blob.init must be a file path")
            else:
                ip = (path.parent / init).resolve()
                if not ip.is_file():
                    cb_problems.append(f"const_blob.init not found: {ip}")
                elif ip.stat().st_size > words * 4:
                    cb_problems.append(
                        f"const_blob.init is {ip.stat().st_size} B > "
                        f"words*4 = {words * 4} B")
                else:
                    raw = ip.read_bytes()
                    raw += b"\0" * (-len(raw) % 4)   # pad to word boundary
                    init_words = list(
                        struct.unpack(f"<{len(raw) // 4}I", raw))
        if cb_problems:
            problems.extend(cb_problems)
        else:
            m["const_blob"] = {"symbol": sym, "words": words,
                               "init_words": init_words,
                               "init_name": init}
    m.setdefault("version", "1.00")
    m.setdefault("display", m["name"])
    m.setdefault("badge", "AW")
    m.setdefault("description", f"{m['name']} - custom effect by Waveformer.")
    if problems:
        die("; ".join(problems))

    free = [s for s in free_slots if s not in expl_slots]
    for i, p in enumerate(m["params"]):
        p["index"] = i + 2                      # table entry / host param idx
        if p.get("slot") is None:
            p["slot"] = free.pop(0)             # coefficient slot (fill order)
        p.setdefault("explanation", f"Sets {p['name']}.")
    return m


def macro_name(pname: str) -> str:
    return "SH_PARAM_" + re.sub(r"[^A-Za-z0-9]", "_", pname).upper()


# --------------------------------------------------------------------------
# generation
# --------------------------------------------------------------------------

def gen_params_header(m: dict) -> str:
    import zd2_cleanroom
    audio_fn = (load_scaffold_anchors().AUDIO_FN if m["scaffold"] == "bitcrush"
                else f"Fx_SFX_{m['sym']}")
    lines = [
        "/* generated by tools/zd2_make_effect.py; do not edit.",
        f" * effect: {m['name']} (id {m['id']})",
        " * ABI: docs/zd2-abi.md. */",
        "#ifndef SH_PARAMS_H",
        "#define SH_PARAMS_H",
        "",
        "/* the scaffold param table points at this symbol; the pragma places",
        " * the kernel in the dedicated .audio section (vaddr 0x7800), unless",
        ' * the manifest says "kernel_section": "text" (big kernels;',
        " * stock-precedented, see docs/zd2-abi.md) */",
        f"#define SH_AUDIO_FN {audio_fn}",
    ] + ([f'#pragma CODE_SECTION({audio_fn}, ".audio")']
         if m["kernel_section"] == "audio" else []) + [
        f"void {audio_fn}(void **instance, void **ctx);",
        "",
        "/* block geometry: 16 float32 frames per channel per call,",
        " * channel A at eff[0..15], channel B at eff[16..31], in place */",
        "#define SH_FRAMES        16",
        "#define SH_CH_B_OFFSET   16",
        "",
        "/* coeff[0] = firmware bypass crossfade, ramps 0..1 (docs/zd2-abi.md);",
        " * kernels should mix dry*(1-fade) + wet*fade for click-free bypass */",
        "#define SH_COEFF_BYPASS  0",
        "",
        "/* ctx word indices: the chain buses. EFF is hardware-proven",
        " * (every port). GTRIN is the pedal-input buffer that stock N_GATE",
        " * and ZNR read for DETCT=GTRIN: mono, 16 words, READ-ONLY here.",
        " * LEGACY_IN (ctx[8]) reads as ZEROS on hardware and its +64 region",
        " * is live firmware state; never use it. OUT/SHUTTLE are G-series",
        " * decode lore, unverified on the plus. */",
        "#define SH_CTX_GTRIN     0  /* pedal-input buffer (float32[16]) */",
        "#define SH_CTX_EFF       1  /* effect bus, in-place (float32[32]) */",
        "#define SH_CTX_OUT       2  /* pedal-output accumulator (unverified) */",
        "#define SH_CTX_SHUTTLE   7  /* -> current-sample word (reported) */",
        "#define SH_CTX_LEGACY_IN 8  /* zeros on MS-70CDR+; do not use */",
        "",
        f"#define SH_SAMPLE_RATE   44100.0f  /* confirmed: stock DELAY_C "
        f"computes samples = ms*441/10 (docs/zd2-abi.md) */",
        f"#define SH_STATE_BYTES   {max(m['state_bytes'], STATE_BYTES_FLOOR)}"
        "  /* zeroed at init; instance[2] */",
        "",
    ] + ([
        "/* init-written state guard: _init stores the magic at state word",
        " * SH_STATE_GUARD_WORD after zeroing. A kernel that dereferences ctx",
        " * slots beyond SH_CTX_EFF must verify it and fail closed to exact",
        " * pass-through: during chain edits the audio fn can run against a",
        " * not-yet-initialized instance. */",
        f"#define SH_STATE_GUARD       0x{zd2_cleanroom.STATE_GUARD:08x}u",
        f"#define SH_STATE_GUARD_WORD  {m['state_bytes'] // 4}",
        "",
    ] if m["scaffold"] == "cleanroom" and m["state_bytes"] > 0 else []) + [
        f"#define SH_NUM_PARAMS    {len(m['params'])}",
    ]
    for p in m["params"]:
        if p.get("curve") == "stock_vol":
            rng = "stock VOL curve amplitude 0..1.5 (unity at 80)"
        else:
            rng = "coeff 0..1"
        lines.append(
            f"#define {macro_name(p['name'])} {p['slot']}"
            f"  /* '{p['name']}' knob 0..{p['max']} -> {rng},"
            f" default {p['default']} */")
    lines += ["", "#endif /* SH_PARAMS_H */", ""]
    return "\n".join(lines)


def gen_table_entries(m: dict) -> str:
    out = []
    last_index = m["params"][-1]["index"]
    for p in m["params"]:
        w0, w1, w2 = struct.unpack(
            "<3I", p["name"].encode("ascii").ljust(12, b"\0"))
        w12 = 0x16 if p["index"] == last_index else 0x10
        out += [
            f"           .word 0x{w0:08x}\t\t; param '{p['name']}' (user param)",
            f"           .word 0x{w1:08x}",
            f"           .word 0x{w2:08x}",
            f"           .word 0x{p['max']:08x}\t\t; max",
            f"           .word 0x{p['default']:08x}\t\t; default",
            f"           .word 0x{p['max']:08x}\t\t; w5 = max (stock knob convention)",
            "           .word 0x00000000",
            f"           .word Aw_P{p['index']}_edit",
            "           .word 0x00000000",
            (f"           .word Aw_P{p['index']}_str"
             f"\t\t; w9 = value-display fn ({'/'.join(p['values'])})"
             if p.get("values") else
             "           .word 0x00000000\t\t; w9 = 0: host renders the"
             " integer"),
            "           .word 0x00000000",
            "           .word 0x00000000",
            f"           .word 0x{w12:08x}\t\t; w12"
            + ("  (0b110 = LAST user param)" if w12 == 0x16 else ""),
            "           .word 0x00000000",
        ]
    return "\n".join(out)


def param_default_coeff(p: dict) -> float:
    """ROM _Coe default for a param: the value its edit handler would
    compute. Curve params store curve(default) (an AMPLITUDE, so the
    pre-CALLP window and the host smoothing ramp both start from the
    right level); plain params store default/max."""
    d = p["default"]
    if p.get("curve") == "stock_vol":
        return d / 80.0 if d <= 80 else 1.0 + (d - 80) / 40.0
    return d / p["max"]


def gen_coe_words(m: dict) -> str:
    # cleanroom _Coe is 16 words (the 12-param maximum); the bitcrush
    # scaffold keeps the stock 12-word table its transform rewrites in place.
    import zd2_cleanroom
    n_words = (zd2_cleanroom.COE_WORDS if m["scaffold"] == "cleanroom"
               else 12)
    coe = [0] * n_words
    for p in m["params"]:
        coe[p["slot"]] = f32_hex(param_default_coeff(p))
    lines = []
    slot_notes = {0: "bypass fade (host)", 1: "bypass fade (host)",
                  2: "reserved", 4: "unknown host writer, keep 0"}
    by_slot = {p["slot"]: p for p in m["params"]}
    for i, w in enumerate(coe):
        note = (f"[{i}] '{by_slot[i]['name']}' default "
                f"{by_slot[i]['default']}/{by_slot[i]['max']}"
                if i in by_slot else f"[{i}] {slot_notes.get(i, 'unused')}")
        lines.append(f"           .word 0x{w:08x}\t\t; {note}")
    return "\n".join(lines)


def gen_handlers(m: dict) -> str:
    out, spliced = [], set()
    for p in m["params"]:
        hp = p.get("handler_asm_path")
        if hp:
            src = Path(hp).read_text()
            # the param table references Aw_P<k>_edit; the custom file must
            # define (and .def, for the verifier's symtab lookup) each label
            for want in (f"Aw_P{p['index']}_edit:",
                         f".def Aw_P{p['index']}_edit"):
                assert want in src, \
                    f"custom handler {hp} must contain '{want}'"
            if hp not in spliced:
                spliced.add(hp)
                out.append(f"\n; --- CUSTOM handler asm ({Path(hp).name}), "
                           "spliced verbatim (manifest handler_asm) ---\n"
                           + src + "\n")
        elif p.get("curve") == "stock_vol":
            smoothed = p["smooth_hex"] is not None
            tail = CURVE_SMOOTH_TAIL if smoothed else CURVE_DIRECT_TAIL
            out.append((CURVE_HEAD + CURVE_COMPUTE_SECTION + tail).format(
                pname=p["name"], k=p["index"], slot=p["slot"],
                off=p["slot"] * 4, svc=p["smooth_svc"],
                rate=p["smooth_hex"] if smoothed else 0,
                tail_kind=(f"+ HOST-SMOOTHED (ctx word {p['smooth_svc']}, "
                           "stock Out_Lv architecture)"
                           if smoothed else "+ direct write")))
        elif p["smooth_hex"] is not None:
            out.append(SMOOTH_HANDLER_TEMPLATE.format(
                pname=p["name"], k=p["index"], slot=p["slot"],
                pmax=p["max"], scale=f32_hex(1.0 / p["max"]),
                rate=p["smooth_hex"], off=p["slot"] * 4,
                svc=p["smooth_svc"]))
        else:
            out.append(HANDLER_TEMPLATE.format(
                pname=p["name"], k=p["index"], slot=p["slot"],
                pmax=p["max"], scale=f32_hex(1.0 / p["max"])))
        # selector params additionally get a w9 value-display function
        if p.get("values"):
            out.append(GETSTRING_TEMPLATE.format(
                pname=p["name"], k=p["index"], vmax=p["max"],
                stride=VALUE_STRIDE, shift=VALUE_STRIDE.bit_length() - 1,
                labels="/".join(p["values"])))
    return "".join(out)


def handler_landmarks(p: dict) -> tuple:
    """dis6x-normalized landmark instructions for one generated handler
    (direct/smooth/curve variant), shared by both verifiers."""
    if p.get("curve") == "stock_vol":
        frags = (f"MVK.L2 {p['index']},B4",
                 "INTSP.L1 A4,A7", "CMPLT.L1 A4,A8,A0",
                 "MVKH.S1 0x3c4c0000,A3",     # 1/80
                 "MVKH.S1 0x42a00000,A3",     # 80.0f
                 "SUBSP.L1 A7,A3,A7",
                 "MVKH.S1 0x3ccc0000,A3",     # 1/40
                 "ADDSP.L1 A7,A3,A7",
                 "[!A0] MV.L1 A7,A9")
        if p["smooth_hex"] is not None:
            return frags + (f"LDW.D2T2 *+B5[{p['smooth_svc']}],B0",
                            f"MVKH.S1 0x{p['smooth_hex'] & 0xFFFF0000:08x},A3",
                            "ADD.L1 A6,A5,A4", "BNOP.S2X A1,5")
        return frags + (f"STW.D1T1 A9,*+A6[{p['slot']}]", "BNOP.S2X A1,4")
    if p["smooth_hex"] is not None:
        return (f"MVK.L2 {p['index']},B4",
                f"LDW.D2T2 *+B5[{p['smooth_svc']}],B0",
                f"MVK.S1 0x{p['slot'] * 4:04x},A5",
                "ADD.L1 A6,A5,A4",
                f"MVKH.S1 0x{p['smooth_hex'] & 0xFFFF0000:08x},A3",
                "INTSP.L1 A4,A0", "MPYSP.M1 A0,A3,A0",
                "BNOP.S2X A1,5")
    return (f"MVK.L2 {p['index']},B4",
            f"STW.D1T1 A0,*+A6[{p['slot']}]",
            "CALLP.S2 __call_stub", "INTSP.L1 A4,A0",
            "MPYSP.M1 A0,A3,A0", "BNOP.S2X A1,4")


def transform_scaffold(m: dict, scaffold_asm: str) -> str:
    anch = load_scaffold_anchors()
    lines = scaffold_asm.split("\n")

    # 1. .audio -> .ref (the C kernel object defines the audio fn)
    start = lines.index('\t.sect ".audio"')
    end = lines.index('\t.sect ".text"')
    assert start < end, "unexpected section order"
    lines[start:end] = [
        f"\t.ref {anch.AUDIO_FN}\t\t; audio fn comes from the C kernel object"]

    # 2. param table: replace the 5 stock user entries (entry 2 'Bit' ..
    #    end of table) with the generated ones
    i0 = lines.index(anch.TABLE_ENTRY2_FIRST_WORD)
    i1 = i0
    while not lines[i1].lstrip().startswith(".dwendtag"):
        i1 += 1
        assert i1 - i0 < 120, "param table end not found"
    lines[i0:i1] = gen_table_entries(m).split("\n")

    s = "\n".join(lines)

    # 3. Dll entry count 7 -> 2 + N
    n_entries = 2 + len(m["params"])
    assert s.count(anch.DLL_COUNT_OLD) == 1, \
        "Dll entry-count site not found once"
    s = s.replace(anch.DLL_COUNT_OLD,
                  f"           MVK.L1        {n_entries},A0\t\t"
                  f"; {n_entries} param-table entries (generated)\n")

    # 4. remove init's stock edit-handler calls: the host param-value store
    #    is unseeded at init time, so these would clobber the ROM defaults
    #    (hardware-proven the hard way)
    assert s.count(anch.INIT_EDIT_CALLS) == 1, \
        "init edit-call block not found once"
    s = s.replace(anch.INIT_EDIT_CALLS,
                  "           ; five stock edit-handler CALLPs removed:"
                  " ROM _Coe defaults must survive init\n")

    # 5. widen init's first state memset to cover state_bytes
    sb = m["state_bytes"]
    if sb > 8:
        assert s.count(anch.MEMSET_OLD) == 1, \
            "init memset site not found once"
        s = s.replace(anch.MEMSET_OLD,
                      "           MV.L1         A0,A4\n"
                      f"           MVK.S1        {sb},A6\t\t"
                      f"; zero {sb} B of state (manifest state_bytes)\n")

    # 6. rewrite the ROM coefficient defaults
    lines = s.split("\n")
    ci = lines.index(f"_{anch.AUDIO_FN}_Coe:")
    for j in range(12):
        assert re.fullmatch(r" {11}\.word 0x[0-9a-f]{8}", lines[ci + 1 + j]), \
            f"_Coe word {j} not where expected"
    lines[ci + 1: ci + 13] = gen_coe_words(m).split("\n")
    s = "\n".join(lines)

    # 7. splice the generated edit handlers at the end of .text
    lines = s.split("\n")
    cpos = lines.index('\t.sect ".const"')
    lines[cpos:cpos] = gen_handlers(m).split("\n")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------

def dis_sections(path: Path) -> dict:
    txt = subprocess.run([str(TI / "dis6x"), "--all", str(path)],
                         capture_output=True, text=True).stdout
    sects, cur = {}, None
    for l in txt.splitlines():
        mm = re.match(r"\s*(?:TEXT|DATA) Section (\S+)", l)
        if mm:
            cur = sects.setdefault(mm.group(1), [])
            continue
        if cur is not None and l.strip():
            cur.append(l)
    return sects


def norm(lines) -> list:
    out = []
    for l in lines:
        l = re.sub(r"^[0-9a-f]{8}\s+(?:[0-9a-f]{4,8}\s+)?", "", l)
        l = re.sub(r"\s*\(PC[+-]\d+ = 0x[0-9a-f]+\)", "", l)
        l = re.sub(r"\s+", " ", l).strip()
        if l:
            out.append(l)
    return out


def cancel_mvk_reresolution(removed: Counter, added: Counter) -> list:
    """Linker layout shifts re-resolve some MVK/MVKL/MVKH immediates that
    patch_asm baked in as constants. Cancel removed/added pairs that are the
    same instruction modulo the immediate; report what was cancelled."""
    cancelled = []
    pat = re.compile(r"^(\|\| )?(MVKL?H?\.S[12X]+) (0x[0-9a-f]+|\d+),(\S+)$")
    for r in list(removed):
        mr = pat.match(r)
        if not mr:
            continue
        for a in list(added):
            if added[a] <= 0 or removed[r] <= 0:
                continue
            ma = pat.match(a)
            if (ma and ma.group(1) == mr.group(1)
                    and ma.group(2) == mr.group(2)
                    and ma.group(4) == mr.group(4)):
                n = min(removed[r], added[a])
                removed[r] -= n
                added[a] -= n
                cancelled.append(f"{r}  ->  {a}  (x{n})")
                if removed[r] <= 0:
                    break
    return cancelled


def verify(m: dict, builddir: Path, report: list) -> None:
    from elftools.elf.elffile import ELFFile
    anch = load_scaffold_anchors()

    base_p = SCAFFOLD_DIR / "BITCRUSH.out"
    built_p = builddir / f"{m['filename']}.out"
    base = ELFFile(open(base_p, "rb"))
    built = ELFFile(open(built_p, "rb"))

    def dynsyms(e):
        d = e.get_section_by_name(".dynsym")
        return {s.name: s["st_value"] for s in d.iter_symbols() if s.name}

    db, dm = dynsyms(base), dynsyms(built)

    # --- dynsym: exactly the generated handlers appear; the 6 stock symbols
    # whose only references were removed (table w7/w9, init CALLPs) drop out
    # of .dynsym, which is fine: the loader does no name lookups (census).
    # Their code stays in .text (the stream diff below proves it).
    new_syms = set(dm) - set(db)
    want_new = {f"Aw_P{p['index']}_edit" for p in m["params"]}
    assert new_syms == want_new, f"unexpected new dynsyms: {new_syms ^ want_new}"
    want_lost = {f"{anch.AUDIO_FN}_Bit_edit", f"{anch.AUDIO_FN}_SampleDiv_edit",
                 f"{anch.AUDIO_FN}_Tone_edit", f"{anch.AUDIO_FN}_Bal_edit",
                 f"{anch.AUDIO_FN}_Out_Lv", anch.GETSTRING_SYM}
    lost = set(db) - set(dm)
    assert lost == want_lost, f"unexpected dynsym loss: {lost ^ want_lost}"
    # The scaffold's Dll entry symbol is absent from .dynsym in every
    # rebuild (the loader enters via e_entry); find it in .symtab instead.
    symtab = {s.name: s["st_value"]
              for s in built.get_section_by_name(".symtab").iter_symbols()
              if s.name}
    assert built.header["e_entry"] == symtab["Dll_BitCrush"], "e_entry mismatch"
    assert dm[anch.AUDIO_FN] == AUDIO_ADDR, ".audio fn not at 0x7800"
    report.append(f"dynsym: +{len(want_new)} generated handlers; "
                  f"e_entry=Dll_BitCrush @ {symtab['Dll_BitCrush']:#x}")

    # --- relocation census: loader contract
    bad = []
    reloc_count = 0
    for sec in built.iter_sections():
        if sec["sh_type"] == "SHT_RELA" and sec["sh_size"]:
            for r in sec.iter_relocations():
                reloc_count += 1
                if r["r_info_type"] not in OK_RELOC_TYPES:
                    bad.append((sec.name, hex(r["r_offset"]),
                                r["r_info_type"]))
    assert not bad, f"relocs outside the loader contract: {bad}"
    report.append(f"relocs: {reloc_count}, all ABS32/ABS_L16/ABS_H16")

    # --- param table structural check against the manifest
    const = built.get_section_by_name(".const").data()
    n_entries = 2 + len(m["params"])
    base_const = base.get_section_by_name(".const").data()
    # entries 0+1 (OnOff + descriptor) byte-identical to stock scaffold
    assert const[:0x70] == base_const[:0x70], "entries 0/1 changed!"
    for p in m["params"]:
        e = struct.unpack("<14I", const[p["index"] * 0x38:
                                        p["index"] * 0x38 + 0x38])
        want_name = struct.unpack(
            "<3I", p["name"].encode().ljust(12, b"\0"))
        assert e[0:3] == want_name, f"{p['name']}: packed name wrong"
        assert e[3] == p["max"] and e[4] == p["default"] and e[5] == p["max"]
        assert e[7] == dm[f"Aw_P{p['index']}_edit"], \
            f"{p['name']}: handler pointer wrong"
        want_w12 = 0x16 if p is m["params"][-1] else 0x10
        assert e[12] == want_w12, f"{p['name']}: w12 {e[12]:#x} != {want_w12:#x}"
        assert e[6] == e[8] == e[9] == e[10] == e[11] == e[13] == 0
    # descriptor entry w7/w8 still point at init + audio fn
    desc = struct.unpack("<14I", const[0x38:0x70])
    assert desc[7] == dm[f"{anch.AUDIO_FN}_init"], "descriptor init ptr wrong"
    assert desc[8] == AUDIO_ADDR, "descriptor audio ptr wrong"
    report.append(f"param table: {n_entries} entries match the manifest; "
                  "end marker on last param; entries 0/1 stock")

    # --- ROM coefficient defaults
    coe_off = dm[f"_{anch.AUDIO_FN}_Coe"] - 0x80000000
    coe = struct.unpack("<12I", const[coe_off:coe_off + 48])
    for p in m["params"]:
        want = f32_hex(param_default_coeff(p))
        assert coe[p["slot"]] == want, \
            f"_Coe[{p['slot']}] = {coe[p['slot']]:#x}, want {want:#x}"
    for i in (0, 1, 2, 4):
        if i not in {p["slot"] for p in m["params"]}:
            assert coe[i] == 0, f"_Coe[{i}] must stay 0"
    report.append("_Coe: defaults match manifest (default/max as float32)")

    # --- .text instruction stream: only the expected changes
    sb_stream = norm(dis_sections(base_p)[".text"])
    sm_all = norm(dis_sections(built_p)[".text"])
    first_handler = f"Aw_P{m['params'][0]['index']}_edit:"
    hidx = sm_all.index(first_handler)
    core, handlers = sm_all[:hidx], sm_all[hidx:]

    removed = Counter(sb_stream) - Counter(core)
    added = Counter(core) - Counter(sb_stream)
    expect_removed = Counter(load_scaffold_anchors().INIT_REMOVED_NORM)
    expect_removed["MVK.L1 7,A0"] += 1
    expect_added = Counter([f"MVK.L1 {2 + len(m['params'])},A0"])
    if m["state_bytes"] > 8:
        expect_removed["MVK.L1 8,A6"] += 1
        expect_added[f"MVK.S1 0x{m['state_bytes']:04x},A6"] += 1
    for k in list(expect_removed):
        n = min(removed[k], expect_removed[k])
        removed[k] -= n
        expect_removed[k] -= n
    for k in list(expect_added):
        n = min(added[k], expect_added[k])
        added[k] -= n
        expect_added[k] -= n
    expect_removed += Counter()
    expect_added += Counter()
    assert not expect_removed, f"expected removals absent: {expect_removed}"
    assert not expect_added, f"expected additions absent: {expect_added}"
    cancelled = cancel_mvk_reresolution(removed, added)
    removed += Counter()
    added += Counter()
    for c in (removed, added):
        for k in list(c):
            if k in ("NOP", "|| NOP") or re.fullmatch(r"NOP \d", k):
                assert c[k] <= 8, f"NOP drift too large: {k} x{c[k]}"
                del c[k]
    assert not removed and not added, (
        f"unexpected .text stream diff:\n  removed: {dict(removed)}"
        f"\n  added: {dict(added)}\n  cancelled pairs: {cancelled}")
    report.append(f".text: stock stream preserved; init edit-calls removed; "
                  f"{len(cancelled)} MVK immediates re-resolved by layout "
                  f"shift; generated handlers appended")

    # --- generated handlers: every instruction accounted for by the template
    (builddir / f"{m['filename']}.handlers.dis").write_text(
        "\n".join(handlers) + "\n")
    for p in m["params"]:
        lbl = f"Aw_P{p['index']}_edit:"
        i = handlers.index(lbl)
        block = "\n".join(handlers[i:i + (50 if p.get("curve") else 26)])
        for frag in handler_landmarks(p):
            assert frag in block, \
                f"handler {lbl} missing '{frag}':\n{block}"
    report.append(f"handlers: {len(m['params'])} generated "
                  f"({sum(1 for p in m['params'] if p['smooth_hex']) or 'no'}"
                  " host-smoothed), template instructions verified in the "
                  "linked binary")

    # --- .audio SAFE-DSP checks
    a = built.get_section_by_name(".audio")
    assert a["sh_addr"] == AUDIO_ADDR, ".audio not at 0x7800"
    assert a["sh_size"] <= AUDIO_MAX_BYTES, \
        f".audio {a['sh_size']} B exceeds stock max {AUDIO_MAX_BYTES}"
    audio = dis_sections(built_p).get(".audio", [])
    body = "\n".join(audio)
    (builddir / f"{m['filename']}.audio.dis").write_text(body + "\n")
    assert "B15" not in body, ".audio uses the stack (B15); unproven, ban"
    for reg in CALLEE_SAVED:
        assert not re.search(rf"\b{reg}\b", body), \
            f".audio touches callee-saved {reg}"
    calls = [l for l in audio if "CALL" in l]
    assert not calls, f".audio makes calls: {calls}"
    report.append(f".audio: {a['sh_size']} B at 0x7800 (max {AUDIO_MAX_BYTES});"
                  " SAFE-DSP ok (leaf, no stack, no callee-saved regs)")


def verify_cleanroom(m: dict, builddir: Path, report: list) -> None:
    """Verifier for 'scaffold': 'cleanroom' builds: the whole
    blob is generated, so instead of diffing against a baseline it checks
    every structure against the manifest + docs/zd2-abi.md contract."""
    import io
    from elftools.elf.elffile import ELFFile
    import zd2_cleanroom as cr
    import zoomzt2

    s = m["sym"]
    built_p = builddir / f"{m['filename']}.out"
    built = ELFFile(open(built_p, "rb"))

    def symdict(secname):
        sec = built.get_section_by_name(secname)
        return {y.name: y["st_value"] for y in sec.iter_symbols() if y.name}

    dm, symtab = symdict(".dynsym"), symdict(".symtab")

    # --- entry + audio placement
    assert built.header["e_entry"] == symtab[f"Dll_{s}"], "e_entry mismatch"
    kaddr = symtab[f"Fx_SFX_{s}"]
    if m["kernel_section"] == "audio":
        assert kaddr == AUDIO_ADDR, ".audio fn not at 0x7800"
    else:
        # the linker may order the kernel before the scaffold; a non-zero
        # e_entry is stock-normal (DELAY_C's is 0xc6c) and the e_entry
        # assert above already pins the Dll symbol
        assert 0 <= kaddr < AUDIO_ADDR, \
            f"text-placed kernel at {kaddr:#x}, expected inside .text"
    report.append(f"entry: e_entry = Dll_{s} @ {symtab[f'Dll_{s}']:#x}; "
                  f"audio fn Fx_SFX_{s} @ {kaddr:#x}"
                  f" ({m['kernel_section']})")
    if m["dll_fields"]:
        report.append("dll_fields (EXPERIMENTAL caller-struct writes): "
                      + "  ".join(f"+{o}=0x{v:x}"
                                  for o, v in sorted(m["dll_fields"].items())))

    # --- relocation census: loader contract
    bad, reloc_count = [], 0
    for sec in built.iter_sections():
        if sec["sh_type"] == "SHT_RELA" and sec["sh_size"]:
            for r in sec.iter_relocations():
                reloc_count += 1
                if r["r_info_type"] not in OK_RELOC_TYPES:
                    bad.append((sec.name, hex(r["r_offset"]),
                                r["r_info_type"]))
    assert not bad, f"relocs outside the loader contract: {bad}"
    report.append(f"relocs: {reloc_count}, all ABS32/ABS_L16/ABS_H16")

    # --- section census: only the expected alloc sections, no uninit data
    UNWANTED = {".bss", ".neardata", ".far", ".fardata", ".cinit", ".stack",
                ".sysmem", ".cio", ".data", ".switch"}
    alloc = {sec.name for sec in built.iter_sections()
             if sec["sh_flags"] & 0x2 and sec["sh_size"]}      # SHF_ALLOC
    assert not (alloc & UNWANTED), f"unexpected data sections: {alloc & UNWANTED}"
    text = built.get_section_by_name(".text")
    text_cap = 768 + 128 * len(m["params"])   # scaffold + per-handler budget
    text_cap += 96 * sum(1 for p in m["params"] if p.get("curve"))
    for hp in sorted({p["handler_asm_path"] for p in m["params"]
                      if p.get("handler_asm_path")}):
        # custom handler asm: budget 4 B per instruction-looking line + slack
        n = sum(1 for l in Path(hp).read_text().splitlines()
                if l.strip() and not l.strip().startswith((";", ".", "*"))
                and not l.strip().endswith(":"))
        text_cap += 4 * n + 256
    if m["kernel_section"] == "text":
        text_cap += 16384                     # big-kernel budget (<< the 72 KB
                                              # stock max, keeps .text < 0x7800)
    assert text["sh_addr"] == 0 and text["sh_size"] <= text_cap, \
        f".text at {text['sh_addr']:#x}, {text['sh_size']} B (cap {text_cap})"
    report.append(f"sections: {sorted(alloc)}; .text {text['sh_size']} B")

    # --- param/function table vs manifest (entries 0/1 from the ABI doc)
    const = built.get_section_by_name(".const").data()
    cbase = built.get_section_by_name(".const")["sh_addr"]
    assert symtab[s] == cbase, "table not first in .const"

    def entry(k):
        return struct.unpack_from("<14I", const, symtab[s] - cbase + k * 0x38)

    e0 = entry(0)
    assert e0[0:3] == cr.pack_name_words("OnOff"), "entry 0 name != OnOff"
    assert e0[3] == 1 and e0[7] == symtab[f"Fx_SFX_{s}_onf"], "entry 0 wrong"
    assert all(v == 0 for i, v in enumerate(e0) if i not in (0, 1, 3, 7))
    e1 = entry(1)
    assert e1[0:3] == cr.pack_name_words(m["name"][:12]), "entry 1 name wrong"
    assert e1[3] == 0xFFFFFFFF and e1[5] == 1, "entry 1 sentinel/w5 wrong"
    assert e1[7] == symtab[f"Fx_SFX_{s}_init"], "entry 1 init ptr wrong"
    assert e1[8] == symtab[f"Fx_SFX_{s}"], "entry 1 audio ptr wrong"
    assert e1[10] == m["descriptor_w10"], \
        f"entry 1 w10 = {e1[10]:#x}, want {m['descriptor_w10']:#x}"
    for p in m["params"]:
        e = entry(p["index"])
        assert e[0:3] == struct.unpack("<3I",
                                       p["name"].encode().ljust(12, b"\0"))
        assert e[3] == p["max"] and e[4] == p["default"] and e[5] == p["max"]
        assert e[7] == symtab[f"Aw_P{p['index']}_edit"], \
            f"{p['name']}: handler pointer wrong"
        want_w12 = 0x16 if p is m["params"][-1] else 0x10
        assert e[12] == want_w12, f"{p['name']}: w12 {e[12]:#x} != {want_w12:#x}"
        assert e[6] == e[8] == e[10] == e[11] == e[13] == 0
        # w9 = value-display fn for a selector, else 0 (host renders the int)
        if p.get("values"):
            assert e[9] == symtab[f"Aw_P{p['index']}_str"], \
                f"{p['name']}: w9 does not point at its value-display fn"
        else:
            assert e[9] == 0, f"{p['name']}: w9 must be 0 for a plain param"
    report.append(f"param table: {2 + len(m['params'])} entries match the "
                  "manifest + ABI doc; end marker on last param")

    # --- selector label tables: the bytes the pedal will actually render
    selectors = [p for p in m["params"] if p.get("values")]
    for p in selectors:
        stride = p["value_stride"]
        base = symtab[f"Aw_P{p['index']}_tab"] - cbase
        for v, label in enumerate(p["values"]):
            got = const[base + v * stride: base + (v + 1) * stride]
            want = label.encode("ascii").ljust(stride, b"\0")
            assert got == want, \
                (f"{p['name']}: label {v} in .const is {got!r}, want {want!r}")
            assert got[len(label)] == 0, f"{p['name']}: label {v} not NUL-term"
    if selectors:
        report.append(
            "selector labels: " + "; ".join(
                f"{p['name']} = {'/'.join(p['values'])}" for p in selectors)
            + f" (w9 -> Aw_P<k>_str, {selectors[0]['value_stride']}-byte"
              " stride, NUL-terminated)")

    # --- effectTypeImageInfo + pic (304-byte struct, blank pic)
    ii = symtab["effectTypeImageInfo"] - cbase
    pic = symtab[f"picEffectType_{s}"] - cbase
    pic_w, pic_h = m["pic_geometry"]
    pic_bytes = m["pic_bytes"]
    assert struct.unpack_from("<3I", const, ii) == \
        (pic_w, pic_h, symtab[f"picEffectType_{s}"]), "imageinfo wrong"
    assert const[ii + 12: ii + 304] == bytes(292), "imageinfo tail not zero"
    assert const[pic: pic + pic_bytes] == bytes(pic_bytes), "pic not blank"
    report.append(f"imageinfo: {{{pic_w},{pic_h},->pic}} + zero tail; "
                  f"pic blank ({pic_bytes} B, generic geometry)")

    # --- ROM coefficient defaults (16 words, the 12-param maximum;
    # EVERY unassigned slot must be 0, which subsumes the reserved
    # [0]/[1]/[4] check)
    coe = struct.unpack_from(f"<{cr.COE_WORDS}I", const,
                             symtab[f"_Fx_SFX_{s}_Coe"] - cbase)
    assigned = {p["slot"] for p in m["params"]}
    for p in m["params"]:
        want = f32_hex(param_default_coeff(p))
        assert coe[p["slot"]] == want, \
            f"_Coe[{p['slot']}] = {coe[p['slot']]:#x}, want {want:#x}"
    for i in range(cr.COE_WORDS):
        if i not in assigned:
            assert coe[i] == 0, f"_Coe[{i}] must stay 0"
    report.append(f"_Coe: {cr.COE_WORDS} words; defaults match manifest "
                  "(default/max as float32); unassigned slots zero")

    # --- optional const_blob: in .const, AFTER _Coe (so the table stayed
    # first, asserted above), sized as declared, init words + zero fill
    cb = m.get("const_blob")
    if cb:
        bsym = cb["symbol"]
        assert bsym in symtab, f"const_blob symbol {bsym} not in symtab"
        boff = symtab[bsym] - cbase
        nbytes = cb["words"] * 4
        assert boff >= symtab[f"_Fx_SFX_{s}_Coe"] - cbase + cr.COE_BYTES, \
            f"const_blob at .const+{boff:#x} not after _Coe"
        assert boff + nbytes <= len(const), \
            f"const_blob {nbytes} B overruns .const ({len(const)} B)"
        want_init = b"".join(struct.pack("<I", w) for w in cb["init_words"])
        got = const[boff:boff + nbytes]
        assert got[:len(want_init)] == want_init, \
            "const_blob init words do not match the init file"
        assert got[len(want_init):] == bytes(nbytes - len(want_init)), \
            "const_blob zero-fill region not zero"
        report.append(
            f"const_blob: '{bsym}' {cb['words']} words at .const+{boff:#x} "
            f"(after _Coe; {len(cb['init_words'])} init words from "
            f"{cb['init_name'] or 'none'}, rest zero); table still first")

    # --- .text: landmark instructions of every generated function, dumped
    # for review; immediates as dis6x renders them (S-unit MVK = 0xNNNN)
    stream = norm(dis_sections(built_p)[".text"])
    (builddir / f"{m['filename']}.text.dis").write_text("\n".join(stream) + "\n")
    body = "\n".join(stream)
    landmarks = {
        f"Dll_{s}": [f"MVK.L1 {2 + len(m['params'])},A0",
                     "STB.D1T1 A0,*+A4[0]", "STW.D1T2 B0,*+A4[1]",
                     "STW.D1T1 A1,*+A4[3]"],
        f"Fx_SFX_{s}_init": ["MVK.S2 0x00ac,B4",
                             f"MVK.S1 0x{cr.COE_BYTES:04x},A6"],
        # dis6x renders MVKL/MVKH halves separately (low / high-shifted)
        f"Fx_SFX_{s}_onf": ["MVK.S2 0x00a0,B4", "MVK.S1 0x6666,A6",
                            "MVKH.S1 0x44300000,A6",
                            "MVKH.S2 0x3f800000,B7"],
        "__call_stub": ["B.S2 B31", "ADDKPC.S2"],
    }
    if m["state_bytes"] > 0:
        import zd2_cleanroom
        g = zd2_cleanroom.STATE_GUARD
        sb = m["state_bytes"]
        # guard write (MVKL/MVKH halves as dis6x renders them). For a guard word
        # beyond 31 the scaffold forms the address explicitly (STW *+base[ucst5]
        # only encodes word offset 0..31), so match that 3-instruction form.
        if (sb // 4) <= 31:
            guard_frags = [f"STW.D1T1 A5,*+A1[{sb // 4}]"]
        else:
            guard_frags = ["MV.L1 A1,A6", f"ADDK.S1 {sb},A6", "STW.D1T1 A5,*+A6[0]"]
        landmarks[f"Fx_SFX_{s}_init"] += [
            "MVK.S2 0x00b0,B4", f"MVK.S1 0x{sb:04x},A6",
            f"MVK.S1 0x{g & 0xffff:04x},A5",
            f"MVKH.S1 0x{g & 0xffff0000:08x},A5"] + guard_frags
    if m["init_edit_calls"]:
        landmarks[f"Fx_SFX_{s}_init"] += [
            f"CALLP.S2 Aw_P{p['index']}_edit,B3" for p in m["params"]]
    for off, val in m["dll_fields"].items():
        # MVKL/MVKH halves as dis6x renders them: the MVKH immediate is
        # high16 << 16 with a 4-hex-digit minimum (0 -> "0x0000")
        landmarks[f"Dll_{s}"] += [
            f"MVK.S1 0x{val & 0xffff:04x},A2",
            f"MVKH.S1 0x{val & 0xffff0000:04x},A2",
            f"STW.D1T1 A2,*+A4[{off // 4}]"]
    for fn, frags in landmarks.items():
        i = stream.index(f"{fn}:")
        # init needs a wider window: guard write + one CALLP block per param
        # (+2 more instructions when the guard word is beyond 31, the explicit
        # address form; see the state-guard landmark branch above)
        win = 40 + (3 * len(m["params"]) + 6 if fn.endswith("_init") else 0)
        if fn.endswith("_init") and m["state_bytes"] > 0 and (m["state_bytes"] // 4) > 31:
            win += 2
        block = "\n".join(stream[i:i + win])
        for frag in frags:
            assert frag in block, f"{fn} missing '{frag}':\n{block}"
    n_custom = 0
    for p in m["params"]:
        lbl = f"Aw_P{p['index']}_edit:"
        if p.get("handler_asm_path"):
            # hand-written handler: no template to match; the param-table
            # check above already pinned e[7] == symtab[Aw_P<k>_edit] (two
            # params may share one handler address, so the label may appear
            # only once in the dis6x stream); flag for hand review
            assert f"Aw_P{p['index']}_edit" in symtab, f"{lbl} not in symtab"
            n_custom += 1
            report.append(f"param '{p['name']}': CUSTOM handler asm "
                          f"({Path(p['handler_asm_path']).name}): template "
                          "landmark check SKIPPED; hand-review "
                          f"{m['filename']}.text.dis before upload")
            continue
        i = stream.index(lbl)
        block = "\n".join(stream[i:i + (50 if p.get("curve") else 26)])
        for frag in handler_landmarks(p):
            assert frag in block, f"handler {lbl} missing '{frag}':\n{block}"
    # the generated w9 value-display functions (selector params)
    for p in [q for q in m["params"] if q.get("values")]:
        lbl = f"Aw_P{p['index']}_str:"
        i = stream.index(lbl)
        block = "\n".join(stream[i:i + 20])
        shift = p["value_stride"].bit_length() - 1
        for frag in (f"MVK.S1 0x{p['max']:04x},A0",   # last valid click
                     "CMPGTU.L1 A4,A0,A1",            # range clamp
                     "[ A1] MV.L1 A0,A4",             # ...applied
                     f"SHL.S1 A4,0x{shift:x},A4",     # click -> byte offset
                     "ADD.L2X B5,A4,B5",              # -> &table[click]
                     "LDW.D2T1 *+B5[0],A5", "LDW.D2T1 *+B5[1],A7",
                     "STW.D2T1 A5,*+B4[0]", "STW.D2T1 A7,*+B4[1]",
                     "BNOP.S2 B3,5"):
            assert frag in block, f"display fn {lbl} missing '{frag}':\n{block}"
    n_str = len([q for q in m["params"] if q.get("values")])
    report.append(f".text: all {4 + len(m['params']) - n_custom + n_str} "
                  "generated functions carry their landmark instructions "
                  f"(full dump: {m['filename']}.text.dis)")

    # --- provenance: nothing stock enters this build. The blob is the
    # generated scaffold plus your compiled kernel, and the container is
    # synthesized from scratch (tools/zd2_from_scratch.py), so there is no
    # stock input anywhere in the chain to diff against.
    report.append("provenance: generated blob + from-scratch container; "
                  "no stock file enters the build")

    # --- kernel SAFE-DSP checks (same discipline as the scaffold path).
    # audio placement: check the whole .audio section. text placement: slice
    # the kernel function out of the .text disassembly (label to next label).
    if m["kernel_section"] == "audio":
        a = built.get_section_by_name(".audio")
        assert a["sh_addr"] == AUDIO_ADDR, ".audio not at 0x7800"
        assert a["sh_size"] <= AUDIO_MAX_BYTES, \
            f".audio {a['sh_size']} B exceeds stock max {AUDIO_MAX_BYTES}"
        kernel = dis_sections(built_p).get(".audio", [])
        ksize, kwhere = a["sh_size"], "at 0x7800"
    else:
        tstream = norm(dis_sections(built_p)[".text"])
        # function boundaries = real symbol labels ($C$... are local branch
        # targets inside a function and must not end the slice)
        starts = [i for i, l in enumerate(tstream)
                  if re.match(r"^[A-Za-z_]\S*:$", l)]
        k0 = tstream.index(f"Fx_SFX_{s}:")
        k1 = min((i for i in starts if i > k0), default=len(tstream))
        kernel = tstream[k0:k1]
        ksize, kwhere = len(kernel) * 4, "in .text (upper size bound)"
    body = "\n".join(kernel)
    (builddir / f"{m['filename']}.audio.dis").write_text(body + "\n")
    calls = [l for l in kernel if "CALL" in l]
    assert not calls, f"kernel makes calls: {calls}"
    if m["allow_stack"]:
        # stack + callee-saved use allowed (stock-precedented: B_CHORUS's
        # audio fn uses B15; the compiler pairs every save with a restore)
        safe_note = "SAFE-DSP relaxed: leaf, stack ALLOWED (allow_stack)"
    else:
        assert "B15" not in body, \
            "kernel uses the stack (B15); set allow_stack to permit"
        for reg in CALLEE_SAVED:
            assert not re.search(rf"\b{reg}\b", body), \
                f"kernel touches callee-saved {reg}"
        safe_note = "SAFE-DSP ok (leaf, no stack, no callee-saved regs)"
    report.append(f"kernel: ~{ksize} B {kwhere}; {safe_note}")


# --------------------------------------------------------------------------
# container finalization
# --------------------------------------------------------------------------

def finalize_container(m: dict, builddir: Path, report: list) -> Path:
    import crcmod
    import zoomzt2

    src = builddir / f"{m['filename']}_rebuilt.ZD2"
    dst = builddir / f"{m['filename']}.ZD2"
    config = zoomzt2.ZD2.parse_file(str(src))

    config["id"] = int(m["id"], 16)
    config["group"] = int(m["id"], 16) >> 24
    # name is CString + namepad filling to 11 bytes (decode_effect.py's
    # --force-name recipe)
    config["name"] = m["name"][:11]
    config["namepad"] = (b"\x00" * (10 - len(m["name"]))
                         if len(m["name"]) < 11 else None)
    config["version"] = m["version"]

    # PRME/PRMJ parameter JSON, regenerated wholesale in the stock format.
    # Grammar quirk: PRMJ carries bytes in 'data', PRME an ascii str in 'xml'.
    entries = b",\r\n".join(
        b'\t\t{  \r\n'
        b'\t\t   "name":"' + p["name"].encode("ascii") + b'",\r\n'
        b'\t\t   "explanation":"' + p["explanation"].encode("ascii") + b'",\r\n'
        b'\t\t   "blackback":false,\r\n'
        b'\t\t   "pedal":false\r\n'
        b'\t\t}' for p in m["params"])
    body = b'{  \r\n\t"Parameters":[  \r\n' + entries + b'\r\n\t]\r\n}'
    json.loads(body.decode("ascii"))          # must stay valid JSON
    for sect, field in (("PRME", "xml"), ("PRMJ", "data")):
        config[sect][field] = (body.decode("ascii") if field == "xml"
                               else body)
        config[sect]["length"] = len(body)

    # TXE1/TXJ1 effect description (ascii is valid Shift-JIS for TXJ1)
    desc = m["description"]
    assert desc.isascii(), "description must be ascii"
    config["TXE1"]["description"] = desc
    config["TXE1"]["peekdescription"] = desc
    config["TXE1"]["length"] = len(desc) + 1     # keep a trailing NUL
    config["TXJ1"]["data"] = desc.encode("ascii") + b"\0"
    config["TXJ1"]["length"] = len(desc) + 1

    # The template's container ICON is a blank placeholder; put the frame
    # make_icon drew into the ZIC there (it runs first), matched by section
    # size, or leave it blank if no ZIC frame matches.
    if m["scaffold"] == "cleanroom":
        import convert_zic
        zicp = builddir / f"{m['filename']}.ZIC"
        assert zicp.exists(), "ICON swap requires the ZIC to be built first"
        zic = convert_zic.ZIC.parse(zicp.read_bytes())
        ilen = config["ICON"]["length"]
        frame = next((bytes(d.data) for ic, d in zip(zic.icons, zic.datas)
                      if ic.bytes == ilen), None)
        config["ICON"]["data"] = frame if frame is not None else bytes(ilen)
        report.append("container ICON: "
                      + ("the drawn ZIC frame placed" if frame is not None
                         else f"left blank, {ilen} B (no size match)"))

    # Optional INFO chunk leading-word override (manifest "info_word0").
    # The container default is word0 = 0; a routing effect may declare a
    # different class here (see the info_word0 notes in load_manifest).
    iw0 = m.get("info_word0")
    if iw0 is not None:
        d = bytearray(config["INFO"]["data"])
        struct.pack_into("<I", d, 0, int(iw0))
        config["INFO"]["data"] = bytes(d)
        report.append(f"container INFO word0 overridden to 0x{int(iw0):08x} "
                      f"(default 0)")

    # Optional honest DSP-load declaration (manifest "dspload"). The pedal
    # composes patches against the DECLARED value (budget 270 raw, Guitar
    # Lab displays raw/2.7 %); a build otherwise inherits the container
    # default.
    dl = m.get("dspload")
    if dl is not None:
        config["INFO"]["dspload"] = float(dl)
        report.append(f"container INFO dspload set to {float(dl):.2f} raw "
                      f"= {float(dl) / 2.7:.1f}% of the 270 budget "
                      f"(container default kept otherwise)")

    # Strip DWARF + static symbols from the shipped blob (manifest "strip",
    # default ON; loader-safe, hardware-proven). The .out and _rebuilt.ZD2
    # intermediates
    # keep their symbols for the verify stages; only the final container
    # ships stripped. strip_elf self-checks; verify_strip is the
    # independent controlled diff.
    if m["strip"]:
        import zd2_strip
        blob = bytes(config["DATA"]["data"])
        stripped, srep = zd2_strip.strip_elf(blob)
        zd2_strip.verify_strip(blob, stripped)
        config["DATA"]["data"] = stripped
        config["DATA"]["length"] = len(stripped)
        report.append(
            f"blob stripped (verified): {srep['old_size']} -> "
            f"{srep['new_size']} B "
            f"(-{100 * srep['stripped_bytes'] / srep['old_size']:.1f}%), "
            f"removed {', '.join(srep['stripped'])}")
    else:
        report.append('blob NOT stripped (manifest "strip": false): '
                      "the shipped file carries DWARF/symtab")

    built = zoomzt2.ZD2.build(config)
    # CRC recipe (poly/init/xorOut over data[12:-16]) re-expressed from
    # mungewell/zoom-zt2 decode_effect.py (MIT).
    crc32 = crcmod.Crc(0x104C11DB7, rev=True, initCrc=0x00000000,
                       xorOut=0xFFFFFFFF)
    crc32.update(built[12:-16])
    config["checksum"] = crc32.crcValue ^ 0xFFFFFFFF
    dst.write_bytes(zoomzt2.ZD2.build(config))
    # The shipped file must satisfy the stock-corpus envelope invariants
    # (header @4 = 120, valid checksum, MS Plus target bit, sections tiling
    # exactly to the trailer). The installer refuses uploads that fail this;
    # failing the build here catches a regression at the desk instead.
    import zd2_from_scratch
    zd2_from_scratch.verify_envelope(dst.read_bytes())
    report.append(f"container: id 0x{m['id']}, name '{m['name']}', "
                  f"{len(m['params'])}-param PRME/PRMJ, TXE1/TXJ1 set, "
                  f"CRC 0x{config['checksum']:08x}, envelope invariants OK")
    return dst


def make_icon(m: dict, builddir: Path, report: list) -> Path:
    """Fresh 1-bit icon frames (all pixels drawn here) in a ZIC built from
    scratch (the standard two-frame DIY geometry, tools/zd2_from_scratch.py)."""
    from PIL import Image, ImageDraw
    import zd2_from_scratch

    zic = builddir / f"{m['filename']}.ZIC"
    zic.write_bytes(zd2_from_scratch.blank_zic())
    for old in builddir.glob("icon_*.png"):
        old.unlink()
    run([PY, ROOT / "zoom-zt2" / "convert_zic.py", "-p", "icon", zic.name],
        cwd=builddir)

    frames = sorted(builddir.glob("icon_*.png"))
    assert frames, "convert_zic produced no frames"
    for p in frames:
        w, h = Image.open(p).size
        img = Image.new("L", (w, h), 255)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, w - 1, h - 1], outline=0, width=2)
        # effect name, one word per line, centered
        words = m["display"].split()
        ys = h // 2 - 7 * len(words)
        for j, word in enumerate(words):
            tw = d.textlength(word)
            d.text(((w - tw) // 2, ys + 14 * j), word, fill=0)
        # badge, bottom-right (the DIY-effect convention)
        bw, bh = max(18, w // 4), 14
        d.rectangle([w - bw - 2, h - bh - 2, w - 2, h - 2],
                    fill=0)
        d.text((w - bw + 2, h - bh), m["badge"], fill=255)
        img.convert("1").save(p)
    run([PY, ROOT / "zoom-zt2" / "convert_zic.py", "-r", "-p", "icon",
         zic.name], cwd=builddir)
    # The pedal parses the icon too; assert its structure here so a
    # regression fails the build, not the install (the installer refuses
    # a malformed icon independently).
    zd2_from_scratch.verify_zic(zic.read_bytes())
    report.append(f"icon: {len(frames)} fresh frames (drawn here), "
                  f"'{m['display']}' + {m['badge']} badge, "
                  f"structure invariants OK")
    return zic


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Build a ZD2 effect from a C kernel + manifest "
                    "(pedal-safe, local only)")
    ap.add_argument("manifest", type=Path)
    args = ap.parse_args()

    mpath = args.manifest.resolve()
    m = load_manifest(mpath)
    effect_dir = mpath.parent
    builddir = effect_dir / "build"
    if builddir.exists():
        shutil.rmtree(builddir)
    builddir.mkdir()

    name, fname = m["name"], m["filename"]
    print(f"== zd2_make_effect: {name} (0x{m['id']}, {fname}.ZD2) ==")
    print(f"   params: " + ", ".join(
        f"{p['name']}[{p['index']}]->coeff[{p['slot']}]"
        f" 0..{p['max']} def {p['default']}" for p in m["params"]))

    report = []
    cleanroom = m["scaffold"] == "cleanroom"
    if not cleanroom:
        # The bitcrush scaffold transforms a stock Zoom effect binary. Its
        # anchors and baseline are Zoom-derived and not part of this
        # repository, so the path fails closed here.
        sys.exit('scaffold "bitcrush" is not supported in this repository '
                 '(it transforms a stock Zoom effect, and its Zoom-derived '
                 'anchors are not included); use "scaffold": "cleanroom"')

    if cleanroom:
        print("== 1. clean-room scaffold ==")
        import zd2_cleanroom
        (builddir / "ZD2.cmd").write_text(zd2_cleanroom.ZD2_CMD)
    else:
        print("== 1. baseline scaffold ==")
        if not ((SCAFFOLD_DIR / "BITCRUSH.asm").exists()
                and (SCAFFOLD_DIR / "BITCRUSH.out").exists()):
            run(["bash", SCAFFOLD_DIR / "rebuild.sh"])
        else:
            print("   (reusing the verified BITCRUSH baseline)")
        shutil.copy(SCAFFOLD_DIR / "ZD2.cmd", builddir / "ZD2.cmd")

    print("== 2. generate sh_params.h + compile the kernel ==")
    (builddir / "sh_params.h").write_text(gen_params_header(m))
    kernel = (effect_dir / m["kernel"]).resolve()
    assert kernel.exists(), f"kernel not found: {kernel}"
    includes = [builddir, effect_dir, effect_dir.parent / "common",
                TI.parent / "include"]
    # opt_for_space=3 is the ZoomMultistompZDL-proven recipe; without it
    # -O2 unrolls e.g. PurestDrive to 3648 B (over the 1472 B .audio cap),
    # with it 672 B. Override per manifest if an effect needs the cycles.
    ofs = m.get("opt_for_space", 3)
    # opt_level defaults to 2. A large kernel can exceed the caller-saved register
    # file at -O2 (aggressive allocation + software pipelining keep many loop-
    # carried/invariant values live at once), spilling to callee-saved regs ->
    # __c6xabi_push_rts -> a stack frame, which is NOT SAFE-DSP. -O1 uses less
    # aggressive allocation and stays leaf/frame-free (Pressure5). Also accepts a
    # manifest "kernel_cflags" list for any other one-off cl6x flags.
    olvl = m.get("opt_level", 2)
    extra_cflags = m.get("kernel_cflags", [])
    assert isinstance(extra_cflags, list), "kernel_cflags must be a list of strings"
    run([TI / "cl6x", "--c99", f"--opt_level={olvl}", "--silicon_version=6740",
         "--abi=eabi", "--endian=little", "--object_format=elf",
         "--symdebug:none", "--mem_model:const=data",
         "--mem_model:data=far_aggregates"]
        + ([f"--opt_for_space={ofs}"] if ofs is not None else [])
        + list(extra_cflags)
        + [f"--include_path={d}" for d in includes if d.is_dir()]
        + ["-c", kernel, f"--output_file={builddir / 'kernel_c.obj'}"],
        cwd=builddir)

    if cleanroom:
        print("== 3. generate the clean-room asm ==")
        import zd2_cleanroom
        (builddir / f"{fname}.asm").write_text(
            zd2_cleanroom.gen_cleanroom_asm(
                m, gen_handlers(m), gen_table_entries(m), gen_coe_words(m)))
        entry, soname = f"Dll_{m['sym']}", f"ZDL_SFX_{m['sym']}.out"
    else:
        print("== 3. transform the scaffold asm ==")
        scaffold = (SCAFFOLD_DIR / "BITCRUSH.asm").read_text()
        (builddir / f"{fname}.asm").write_text(transform_scaffold(m, scaffold))
        print("   all anchors matched exactly once")
        entry, soname = "Dll_BitCrush", "ZDL_SFX_BitCrush.out"

    print("== 4. assemble + link + pack (from-scratch container) ==")
    # The container the blob gets packed into is synthesized from scratch:
    # the three undecoded header fields ship zeroed (hardware-proven; see
    # tools/zd2_from_scratch.py), so nothing from a stock effect file enters
    # the build.
    import zd2_from_scratch
    template = builddir / f"{fname}_template.ZD2"
    template.write_bytes(zd2_from_scratch.build_template(
        name=m["name"], eid=int(m["id"], 16),
        version=m.get("version", "1.00")))
    # --no-strip here: the verify stages want symbol-rich intermediates;
    # the SHIPPED container is stripped once, in finalize_container
    # (manifest "strip", default true).
    script = ROOT / "tools" / "build_zd2_from_asm.sh"
    bash_args = [script, fname, entry, soname, template, builddir,
                 builddir / "kernel_c.obj", "--no-strip"]
    if os.name == "nt":
        # Git Bash needs forward-slash (C:/...) paths, not Windows backslashes.
        bash_args = [a.as_posix() if isinstance(a, Path) else a
                     for a in bash_args]
    run([_bash(), *bash_args])

    print("== 5. verify ==")
    if cleanroom:
        verify_cleanroom(m, builddir, report)
    else:
        verify(m, builddir, report)
    for line in report:
        print(f"   OK  {line}")

    print("== 6. icon ==")
    n = len(report)
    make_icon(m, builddir, report)

    print("== 7. finalize container (identity, PRME/PRMJ, TXE1, ICON) ==")
    dst = finalize_container(m, builddir, report)

    print("== 8. final validation ==")
    run([PY, ROOT / "zoom-zt2" / "decode_effect.py", "-s", "-V", dst])
    run([PY, ROOT / "tools" / "zd2_roundtrip.py", dst])
    for line in report[n:]:
        print(f"   OK  {line}")

    sharing = (
        "- CLEAN-ROOM build: the code blob contains NO "
        "Zoom-authored code, and the container, icon, text and params are "
        "yours (the container is synthesized from scratch; the three "
        "undecoded header fields ship zeroed, hardware-proven). "
        "MIT-shareable ONCE hardware-validated. "
        "See docs/ip-and-licensing.md.\n")
    dis_files = (f"- `build/{fname}.text.dis`: full generated .text "
                 "(review: every function in it is generated)\n")
    (builddir / "REPORT.md").write_text(
        f"# Build report: {name} (0x{m['id']})\n\n"
        f"Built by tools/zd2_make_effect.py from {mpath.name}; "
        "scaffold: CLEAN-ROOM (tools/zd2_cleanroom.py), container "
        "synthesized from scratch (tools/zd2_from_scratch.py).\n\n"
        + "\n".join(f"- {r}" for r in report)
        + "\n\n## Parameters\n\n"
        + "| # | name | range | default | coeff slot | handler |\n"
          "|---|---|---|---|---|---|\n"
        + "\n".join(
            f"| {p['index']} | {p['name']} | 0..{p['max']} | {p['default']} "
            f"| [{p['slot']}] | "
            + (f"smoothed (ctx[{p['smooth_svc']}] rate "
               f"0x{p['smooth_hex']:08x}) |"
               if p["smooth_hex"] else "direct |")
            for p in m["params"])
        + "\n\n## Files\n\n"
        f"- `build/{fname}.ZD2` + `build/{fname}.ZIC`: the upload pair\n"
        f"- `build/{fname}.audio.dis`: C kernel disassembly (review)\n"
        + dis_files
        + "\n## Reminders\n\n"
        + sharing +
        "- Upload under the safety protocol only (SAFETY.md): "
        "safe_connect first, live patch only, never save, readback compare.\n"
        + (f"- dspload declared {float(m['dspload']):.2f} raw = "
           f"{float(m['dspload']) / 2.7:.1f}% on-device; the pedal "
           "TRUSTS this for patch-composition gating (270-raw budget).\n"
           if m.get("dspload") is not None else
           "- dspload in INFO is inherited from the container default, "
           "NOT estimated for this kernel; the pedal TRUSTS the "
           "declaration (270-raw budget), so pick an honest value "
           "(manifest `dspload` key, raw = on-device % x 2.7).\n"))

    print(f"\n== {name} ready: {builddir / (fname + '.ZD2')} ==")
    print(f"   report: {builddir / 'REPORT.md'}")
    print("   Upload only under the safety protocol (ask-gated).")


if __name__ == "__main__":
    main()
