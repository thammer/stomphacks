#!/usr/bin/env python3
"""Clean-room ZD2 scaffold generator.

Emits a COMPLETE effect assembly file (Dll entry stub, _init, _onf,
__call_stub, param/function table, effectTypeImageInfo + pic, _Coe), written
from the documented ABI (docs/zd2-abi.md), with no Zoom-authored instructions
or data. Together with the generated edit handlers and the C kernel this
makes the whole code blob original work, so the built ZD2 is MIT-shareable
(docs/ip-and-licensing.md).

Used by tools/zd2_make_effect.py when a manifest sets "scaffold":
"cleanroom". Pedal-safe: pure text generation, no MIDI, no file I/O.

Code style: fully serial (no parallel pairs except where the C6000
branch-delay architecture forces them, inside __call_stub), with an explicit
NOP for every load delay slot. The register budget relies on the __call_stub
preservation contract (docs/zd2-abi.md): A0-A2, A6, A7, B0-B7 survive host
calls.

Provenance note on __call_stub: its instruction sequence is dictated by the
preserved-register contract plus the C6000's 5 branch delay slots, so any
implementation converges on the same shape as TI's run-time-support routine
of the same name. Written here from the documented contract.
"""

from __future__ import annotations

import struct

# Descriptor entry 1 word 10: a per-effect DSP quantity that tracks
# reverb/delay TAIL MAGNITUDE. Across the stock corpus it is 0 in 68 of the
# 149 effects; non-zero values (~13..127) scale with tail/decay size.
# Default 0, the correct value for an effect with no tail. A reverb/delay
# may set a real value via the manifest "descriptor_w10" key; gen_const_data
# reads it with this as the fallback.
DESCRIPTOR_W10 = 0

# _Coe ROM coefficient-defaults size. 16 words for the 12-param maximum:
# the 12 free slots [3,5,6,7,8,9,10,11,2,12,13,14] plus the reserved
# [0]/[1]/[4] and the never-assigned [15] round up to 16. Stock-precedented
# on both axes: coeff tables far bigger than 48 B are normal (stock delays
# and reverbs copy 120..624 B), and slots >= 12 are plain effect-owned
# words. The live coeff-table grant looks fixed at >= 624 B for every effect
# (no size declaration exists in any blob), so a 64-B copy is comfortably
# inside every observed bound.
COE_WORDS = 16
COE_BYTES = COE_WORDS * 4

# Init-written state guard: _init stores this magic at state byte offset ==
# state_bytes (the word AFTER the kernel's declared state). Kernels that
# dereference ctx slots beyond ctx[1] must check it and fail closed to exact
# pass-through: during the chain-edit transition the audio fn can run
# against an instance whose coeff table/state is not yet initialized.
# Requires state_bytes <= 188 so the guard stays inside the proven
# >=192-byte firmware scratch area.
STATE_GUARD = 0x57464731        # 'WFG1'

# effectTypeImageInfo (docs/zd2-abi.md): {width, height, ->pic} + 73 zero
# words = 304 bytes; the pic is BLANK on cleanroom builds (the pedal renders
# the separate .ZIC file, not this in-blob pic). The [w, h] is therefore a
# cosmetic interop dimension, not creative expression: the stock corpus uses
# several geometries (23x30 most common, 24x30 next), so 24x30 is no Zoom
# signature. Default [24,30] (a generic dimension that loads
# fine); overridable per manifest "pic_geometry": [w, h]. The blank pic
# buffer is sized round_up_to_4(ceil(w/8)*h), so 24x30 = 92 bytes.
# gen_const_data reads m["pic_geometry"]/m["pic_bytes"] with these as
# fallbacks. (The drawn icon lives in the .ZIC, unrelated to this blank
# in-blob geometry.)
PIC_W, PIC_H = 24, 30
IMAGEINFO_TAIL_WORDS = 73
PIC_BYTES = 92

ZD2_CMD = """\
/* Clean-room linker command file, written from the memory
   map in docs/zd2-abi.md: code at 0, .audio at 0x7800 (stock convention,
   max seen 1472 B), data at 0x80000000. */
MEMORY
{
    CODE   o = 0x00000000  l = 0x00007800
    AUDIO  o = 0x00007800  l = 0x00002000
    DATA   o = 0x80000000  l = 0x10000000
}

SECTIONS
{
    .text     > CODE
    .audio    > AUDIO
    .const    > DATA
    .data     > DATA
    .switch   > DATA
    .far      > DATA
    .fardata  > DATA
}
"""


def pack_name_words(name: str) -> tuple[int, int, int]:
    """Pack an ascii name into the 3 leading words of a param-table entry."""
    return struct.unpack("<3I", name.encode("ascii").ljust(12, b"\0"))


def _entry_words(words, comment_first: str) -> list[str]:
    out = []
    for i, w in enumerate(words):
        line = (f"           .word {w}" if isinstance(w, str)
                else f"           .word 0x{w:08x}")
        if i == 0:
            line += f"\t\t; {comment_first}"
        out.append(line)
    return out


def gen_text_functions(m: dict) -> str:
    """Dll + _init + _onf + __call_stub, from docs/zd2-abi.md."""
    s = m["sym"]
    n_entries = 2 + len(m["params"])
    sb = m["state_bytes"]

    # Optional extra caller-struct writes (manifest "dll_fields": byte
    # offset -> 32-bit value). 89 of the 149 stock effects write fields
    # beyond +0/+4/+12; the meaning is still under investigation, and this
    # hook exists so probe effects can mimic them.
    extra = ""
    for off, val in sorted(m.get("dll_fields", {}).items()):
        extra += (f"           MVKL.S1       0x{val:08x},A2\n"
                  f"           MVKH.S1       0x{val:08x},A2\n"
                  f"           STW.D1T1      A2,*+A4[{off // 4}]"
                  f"        ; +{off} caller-struct field (dll_fields)\n")

    dll = f"""
; --- Dll_{s}: loader entry (e_entry). Contract (docs/zd2-abi.md "Entry
; points"): A4 -> caller struct; store entry count (byte) at +0, param/
; function table pointer at +4, effectTypeImageInfo pointer at +12.
\t.def Dll_{s}
\t.align 32
Dll_{s}:
           MVK.L1        {n_entries},A0    ; {n_entries} param-table entries
           MVKL.S2       {s},B0
           MVKH.S2       {s},B0            ; B0 = &{s} (the table)
           MVKL.S1       effectTypeImageInfo,A1
           MVKH.S1       effectTypeImageInfo,A1
           STB.D1T1      A0,*+A4[0]        ; +0  entry count (byte)
           STW.D1T2      B0,*+A4[1]        ; +4  table pointer
           STW.D1T1      A1,*+A4[3]        ; +12 image info pointer
{extra}           BNOP.S2       B3,5              ; return
"""

    memset_block = f"""
; state area: (*ctx+176)(state, 0, {sb}) via the host memset
           MVK.S2        176,B4
           ADD.L2        B5,B4,B4
           LDW.D2T2      *B4[0],B0         ; B0 = host memset (ctx+176)
           MVK.L2        0,B4              ; fill value 0
           MVK.S1        {sb},A6           ; {sb} bytes (manifest state_bytes)
           NOP           2
           MV.L2         B0,B31            ; (load completed: issue+5)
           MV.L1         A1,A4             ; dest = state (A1 survived the call)
           CALLP.S2      __call_stub,B3    ; memset(state, 0, {sb})
""" if sb > 0 else """
; state_bytes = 0: no state to zero
"""

    # state guard: magic at state word sb/4, written AFTER the memset so a
    # kernel can distinguish "init has run" from transition-window garbage.
    # Only meaningful with state.
    # STW *+base[ucst5] encodes a word offset of only 0..31 for a general base
    # register, so for a guard word beyond 31 (state_bytes > 124) the address
    # must be formed explicitly. A1 (state base) is dead after this store, but
    # the computation goes into A6 (also dead here: it held the memset size)
    # to leave A1 intact and keep the small-state path byte-identical.
    if sb > 0 and (sb // 4) <= 31:
        guard_store = f"           STW.D1T1      A5,*+A1[{sb // 4}]   ; A1 = state (survived the calls)"
    else:
        guard_store = (f"           MV.L1         A1,A6             ; A6 = state base\n"
                       f"           ADDK.S1       {sb},A6           ; A6 = &state[{sb // 4}] (offset > 31 words)\n"
                       f"           STW.D1T1      A5,*A6            ; guard word")
    guard_block = f"""
; state guard: state[{sb // 4}] = 0x{STATE_GUARD:08x} ('WFG1'), the init-has-run
; marker for kernels with extended-ctx derefs
           MVKL.S1       0x{STATE_GUARD:08x},A5
           MVKH.S1       0x{STATE_GUARD:08x},A5
{guard_store}
""" if sb > 0 else ""

    # init-time edit-handler CALLPs, the stock protocol: after the defaults
    # land, call every edit handler once (A4 = instance, B4 = ctx) so the
    # host's CURRENT param values re-materialize into the coeffs. Without
    # this, every chain edit (which re-runs _init on all instances) silently
    # reverts the params to ROM defaults. Manifest "init_edit_calls": false
    # restores the old behavior if the host store turns out to be unseeded at
    # first add.
    edit_calls = ""
    if m.get("init_edit_calls", True) and m["params"]:
        edit_calls = ("\n; re-materialize user params: CALLP every edit "
                      "handler (stock init protocol)\n")
        for p in m["params"]:
            k = p["index"]
            edit_calls += (
                f"           MV.L1         A2,A4             ; instance\n"
                f" ||        MV.L2         B5,B4             ; ctx\n"
                f"           CALLP.S2      Aw_P{k}_edit,B3   "
                f"; materialize '{p['name']}'\n")

    # init tail: plain return, or (init_onf_call) tail-call _onf to re-assert
    # the router/bypass state on every re-init. A word0=2 router's
    # ctx[2]->amp gate can go stale across a chain edit while a stock LINE
    # SEL's persists; re-running _onf at init re-establishes it. Register
    # safety: A2 (instance), B5 (ctx) and B2 (the saved return) all survive the
    # memcpy/memset/edit CALLPs: __call_stub preserves A0-A2 & B0-B7, and
    # the edit handler saves its own return in A1 and never touches A2/B5/B2.
    # B2 goes in as _onf's return, so _onf tail-returns straight to init's
    # caller.
    if m.get("init_onf_call", False):
        init_end = f"""
; re-assert the router/bypass state on every re-init.
           MV.L1         A2,A4             ; A4 = instance (_onf arg)
 ||        MV.L2         B5,B4             ; B4 = ctx (_onf arg)
 ||        MV.S2         B2,B3             ; B3 = init's return (tail-call target)
           BNOP.S1       Fx_SFX_{s}_onf,5  ; tail-call _onf (does NOT return here)"""
    else:
        init_end = ("           BNOP.S2       B2,5              "
                    "; return")

    init = f"""
; --- Fx_SFX_{s}_init: called when the effect is added to the chain AND on
; every chain edit (insert/delete re-runs _init on all instances).
; Contract (docs/zd2-abi.md): copy the {COE_BYTES}-byte _Coe ROM defaults
; into the live coeff table via the host memcpy at ctx+172, zero the state
; area via the host memset at ctx+176, then CALLP each edit handler to
; re-materialize the user's current param values, and mark the state area
; initialized.
\t.def Fx_SFX_{s}_init
\t.align 32
Fx_SFX_{s}_init:
           MV.L2         B3,B2             ; B2 = return address (survives __call_stub)
           MV.L2         B4,B5             ; B5 = ctx (survives __call_stub)
           MV.L1         A4,A2             ; A2 = instance (survives __call_stub)
           LDW.D1T1      *A4[1],A0         ; A0 = live coeff table
           LDW.D1T1      *A4[2],A1         ; A1 = per-slot state area
; coeff defaults: (*ctx+172)(coeff, _Coe, {COE_BYTES}) via the host memcpy
           MVK.S2        172,B4
           ADD.L2        B5,B4,B4
           LDW.D2T2      *B4[0],B0         ; B0 = host memcpy (ctx+172)
           MVKL.S2       _Fx_SFX_{s}_Coe,B4
           MVKH.S2       _Fx_SFX_{s}_Coe,B4 ; src = ROM defaults
           MVK.S1        {COE_BYTES},A6            ; {COE_WORDS} coeff words
           NOP           1
           MV.L2         B0,B31            ; (load completed: issue+5)
           MV.L1         A0,A4             ; dest = coeff table
           CALLP.S2      __call_stub,B3    ; memcpy(coeff, _Coe, {COE_BYTES})
{memset_block}{guard_block}{edit_calls}{init_end}
"""

    onf = f"""
; --- Fx_SFX_{s}_onf: bypass toggle. Contract (docs/zd2-abi.md): fetch
; OnOff (param 0) via the host get-param at ctx+160,
; then register two host fade ramps (ctx word 3) at rate 705.6f:
; ON: coeff[0]->1.0, coeff[1]->0.0; OFF: coeff[0]->0.0, coeff[1]->1.0.
; The kernel mixes dry*(1-coeff[0]) + wet*coeff[0] for click-free bypass.
\t.def Fx_SFX_{s}_onf
\t.align 32
Fx_SFX_{s}_onf:
           MV.L2         B3,B2             ; B2 = return address (survives calls)
           MV.L2         B4,B5             ; B5 = ctx
           LDW.D1T1      *A4[1],A7         ; A7 = coeff table (survives calls)
           LDW.D1T1      *A4[0],A4         ; host param object (get-param arg)
           MVK.S2        160,B4
           ADD.L2        B5,B4,B4
           LDW.D2T2      *B4[0],B0         ; B0 = host get-param (ctx+160)
           MVK.L2        0,B4              ; parameter index 0 = OnOff
           NOP           3
           MV.L2         B0,B31            ; (load completed: issue+5)
           CALLP.S2      __call_stub,B3    ; A4 = get-param(obj, 0)
           MV.L2X        A4,B1             ; B1 = on/off (predicate reg)
           LDW.D2T2      *+B5[3],B6        ; B6 = host ramp-register fn (ctx[3])
           MVKL.S1       0x44306666,A6
           MVKH.S1       0x44306666,A6     ; A6 = 705.6f ramp rate (44100*16/1000)
           MVKL.S2       0x3f800000,B7
           MVKH.S2       0x3f800000,B7     ; B7 = 1.0f
           MVK.L2        0,B4              ; coeff[0] target if OFF: 0.0
   [ B1]   MV.L2         B7,B4             ; coeff[0] target if ON:  1.0
           MV.L1         A7,A4             ; &coeff[0]
           MV.L2         B6,B31
           CALLP.S2      __call_stub,B3    ; ramp(&coeff[0], t0, 705.6)
           MVK.L2        0,B4              ; coeff[1] target if ON:  0.0
   [!B1]   MV.L2         B7,B4             ; coeff[1] target if OFF: 1.0
           ADD.L1        A7,4,A4           ; &coeff[1]
           MV.L2         B6,B31            ; (B1/B6/B7/A6/A7 survived the call)
           CALLP.S2      __call_stub,B3    ; ramp(&coeff[1], t1, 705.6)
           BNOP.S2       B2,5              ; return
"""

    call_stub = """
; --- __call_stub: indirect host call, target in B31. Contract
; (docs/zd2-abi.md): preserves A0-A2, A6, A7, B0-B7 across
; the call; args/return in the EABI regs (A4, B4, A6 / A4). The parallel
; pairs and slot counts are forced by the 5 branch delay slots.
\t.def __call_stub
\t.align 32
__call_stub:
           STW.D2T1      A2,*B15--[2]
           B.S2          B31               ; enter host fn after 5 delay slots
 ||        STDW.D2T1     A7:A6,*B15--[1]
           STDW.D2T1     A1:A0,*B15--[1]
           STDW.D2T2     B7:B6,*B15--[1]
           STDW.D2T2     B5:B4,*B15--[1]
           STDW.D2T2     B1:B0,*B15--[1]
           STDW.D2T2     B3:B2,*B15--[1]   ; saves the caller's B3...
 ||        ADDKPC.S2     __cr_stub_ret,B3,0 ; ...while pointing host's B3 here
__cr_stub_ret:
           LDDW.D2T2     *++B15[1],B3:B2
           LDDW.D2T2     *++B15[1],B1:B0
           LDDW.D2T2     *++B15[1],B5:B4
           LDDW.D2T2     *++B15[1],B7:B6
           LDDW.D2T1     *++B15[1],A1:A0
           B.S2          B3                ; back to the CALLP site
 ||        LDDW.D2T1     *++B15[1],A7:A6
           LDW.D2T1      *++B15[2],A2
           NOP           4
"""

    return dll + init + onf + call_stub


def gen_const_data(m: dict, table_entries_asm: str, coe_words_asm: str) -> str:
    """.const: table + effectTypeImageInfo + pic + _Coe [+ const_blob]."""
    s = m["sym"]
    onoff = pack_name_words("OnOff")
    ename = pack_name_words(m["name"][:12])
    # metadata with defaults set here but overridable per
    # manifest: the descriptor w10 and the blank in-blob pic geometry.
    w10 = m.get("descriptor_w10", DESCRIPTOR_W10)
    pic_w, pic_h = m.get("pic_geometry", [PIC_W, PIC_H])
    pic_bytes = m.get("pic_bytes", PIC_BYTES)

    lines = ['', '\t.sect ".const"', "\t.align 8"]

    # entry 0: OnOff (docs/zd2-abi.md param-table layout; w7 -> _onf)
    lines += [f"\t.def {s}", f"{s}:"]
    lines += _entry_words(
        [onoff[0], onoff[1], onoff[2], 1, 0, 0, 0,
         f"Fx_SFX_{s}_onf", 0, 0, 0, 0, 0, 0],
        "entry 0: 'OnOff', w3 max 1, w7 = onf handler")
    # entry 1: effect descriptor (w3 = 0xffffffff sentinel, w7 = init,
    # w8 = audio fn, w10 = per-effect float of unknown meaning)
    lines += _entry_words(
        [ename[0], ename[1], ename[2], 0xFFFFFFFF, 0, 1, 0,
         f"Fx_SFX_{s}_init", f"Fx_SFX_{s}", 0, w10, 0, 0, 0],
        f"entry 1: '{m['name']}' descriptor, w7 init, w8 audio fn")
    # entries 2..: the user parameters (same generator as the proven builds)
    lines.append(table_entries_asm)

    # effectTypeImageInfo: {w, h, ->pic} + 73 zero words = 304 B
    lines += ["", "\t.def effectTypeImageInfo", "effectTypeImageInfo:",
              f"           .word 0x{pic_w:08x}\t\t; pic width",
              f"           .word 0x{pic_h:08x}\t\t; pic height",
              f"           .word picEffectType_{s}"]
    lines += ["           .word 0x00000000"] * IMAGEINFO_TAIL_WORDS

    # pic: blank; the pedal displays the .ZIC, not this in-blob pic
    lines += ["", f"\t.def picEffectType_{s}", f"picEffectType_{s}:"]
    lines += ["           .word 0x00000000"] * (pic_bytes // 4)

    # ROM coefficient defaults
    lines += ["", f"\t.def _Fx_SFX_{s}_Coe", f"_Fx_SFX_{s}_Coe:",
              coe_words_asm]

    # Optional named const reserve (manifest "const_blob"):
    # emitted LAST in the scaffold's own .const input section, so the
    # param table stays first regardless of blob size. A kernel-C const
    # array of comparable size would instead form its own .const input
    # section, and lnk6x packs same-name input sections by DESCENDING
    # SIZE, so a big kernel array lands ahead of the table and breaks
    # the loader contract (verify_cleanroom "table not first in
    # .const"). The kernel references this data via
    # `extern const uint32_t <symbol>[];`.
    cb = m.get("const_blob")
    if cb:
        n_init = len(cb["init_words"])
        lines += ["", "\t.align 8",
                  f"\t.def {cb['symbol']}",
                  f"{cb['symbol']}:\t\t; const_blob: {cb['words']} words"
                  f" ({n_init} init, rest zero)"]
        words = cb["init_words"] + [0] * (cb["words"] - n_init)
        lines += [f"           .word 0x{w:08x}" for w in words]

    # Selector value-label tables (manifest "values"): one fixed-stride,
    # NUL-padded string table per selector param, indexed by the generated
    # Aw_P<k>_str display function the param table's w9 points at (the stock
    # GetString idiom). Emitted LAST for the same reason as const_blob: the
    # param table must stay
    # FIRST in .const (loader contract, verify_cleanroom "table not first").
    for p in m["params"]:
        vals = p.get("values")
        if not vals:
            continue
        stride = p["value_stride"]      # set by zd2_make_effect (VALUE_STRIDE)
        lines += ["", "\t.align 8", f"\t.def Aw_P{p['index']}_tab",
                  f"Aw_P{p['index']}_tab:\t\t; '{p['name']}' labels,"
                  f" {stride}-byte stride"]
        for v, label in enumerate(vals):
            packed = label.encode("ascii").ljust(stride, b"\0")
            body = ",".join(str(b) for b in packed)
            lines.append(f"           .byte {body}\t; {v} = '{label}'")
    return "\n".join(lines) + "\n"


def gen_cleanroom_asm(m: dict, handlers_asm: str, table_entries_asm: str,
                      coe_words_asm: str) -> str:
    s = m["sym"]
    header = f"""\
; ===========================================================================
; {m['name']} (id 0x{m['id']}): CLEAN-ROOM ZD2 scaffold
; Generated by tools/zd2_cleanroom.py via tools/zd2_make_effect.py.
; Every instruction and data word in this file is written fresh from the
; documented ABI (docs/zd2-abi.md). The audio kernel Fx_SFX_{s} comes from
; the manifest's C source. No Zoom-authored code anywhere in the blob.
; ===========================================================================
; the .compiler_opts line makes asm6x emit ELF/EABI (without it: TI-COFF)
\t.compiler_opts --abi=eabi --endian=little --mem_model:const=data --mem_model:data=far_aggregates --object_format=elf --silicon_version=6740

\t.ref Fx_SFX_{s}\t\t; audio kernel (C object, placed in .audio)

\t.sect ".text"
"""
    return (header + gen_text_functions(m) + "\n" + handlers_asm
            + gen_const_data(m, table_entries_asm, coe_words_asm))
