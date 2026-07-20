#!/usr/bin/env bash
# Build a ZD2 effect from a (already assembled/reassemblable) TI C6000 asm
# file. Shared backend: assemble, link and pack. Called by
# tools/zd2_make_effect.py.
#
# Pedal-safe: runs only local TI tools + decode_effect.py; no MIDI. Lives in
# tools/ and is auto-approved. It does NOT upload anything to the pedal.
#
# Usage:
#   tools/build_zd2_from_asm.sh <name> <entry_sym> <soname> <container.ZD2> <workdir> [extra_objs] [--no-strip]
# where:
#   <name>       basename of <workdir>/<name>.asm  (must already be patched)
#   <entry_sym>  ELF entry symbol, e.g. Dll_MyEffect
#   <soname>     DT_SONAME to set, e.g. ZDL_SFX_MyEffect
#   <container.ZD2>  the container the linked blob is packed into
#                (zd2_make_effect.py synthesizes this from scratch via
#                tools/zd2_from_scratch.py; any valid ZD2 works)
#   <workdir>    directory holding <name>.asm and where outputs are written
#   [extra_objs] optional space-separated additional objects to link
#                (e.g. a cl6x-compiled C audio function)
#   [--no-strip] keep DWARF/.symtab/.strtab in the packaged blob. DEFAULT is
#                to STRIP them from <name>_rebuilt.ZD2 (loader-safe,
#                hardware-proven), so DIY effects ship stripped. Use
#                --no-strip for callers that strip later themselves
#                (tools/zd2_make_effect.py strips at container finalize).
#                The .out always keeps its symbols either way.
#
# Produces in <workdir>: <name>.obj, <name>.out, <name>_rebuilt.ZD2
# Then validates CRC and round-trips the container.
#
# NOTE: asm6x 8.5.0 cannot emit 16-bit compact instructions from hand-edited
# asm, so a rebuilt .out is not byte-identical to a factory (8.3.12-compiled)
# effect; it is functionally equivalent (identical instruction stream +
# symbols). Verify modifications with the controlled-diff method (baseline
# .out vs modified .out), not against the original bytes.

set -euo pipefail

# filter the --no-strip flag out of the positional args (may appear anywhere)
STRIP=1
ARGS=()
for a in "$@"; do
    if [ "$a" = "--no-strip" ]; then STRIP=0; else ARGS+=("$a"); fi
done
set -- "${ARGS[@]}"

if [ "$#" -lt 5 ] || [ "$#" -gt 6 ]; then
    grep '^#' "$0" | sed 's/^# \{0,1\}//'
    exit 2
fi

NAME="$1"; ENTRY="$2"; SONAME="$3"; CONTAINER="$4"; WORKDIR="$5"; EXTRA_OBJS="${6:-}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# TI CGT install root: ZOOM_TI_CGT overrides it (if your compiler lives
# elsewhere); the default is the macOS 8.5.0 install path. Under Git Bash on
# Windows, "$TI/asm6x" resolves asm6x.exe automatically (MSYS appends .exe).
TI="${ZOOM_TI_CGT:-/Applications/ti/ti-cgt-c6000_8.5.0.LTS}/bin"
# venv interpreter: Windows (Git Bash) has .venv/Scripts/python.exe, POSIX has
# .venv/bin/python3.
if [ -x "$ROOT/.venv/Scripts/python.exe" ]; then
    PY="$ROOT/.venv/Scripts/python.exe"
else
    PY="$ROOT/.venv/bin/python3"
fi
DECODE="$ROOT/zoom-zt2/decode_effect.py"
CMD="$WORKDIR/ZD2.cmd"

[ -f "$WORKDIR/$NAME.asm" ] || { echo "missing $WORKDIR/$NAME.asm"; exit 1; }
[ -f "$CMD" ] || { echo "missing linker command file $CMD"; exit 1; }

echo "== assemble =="
# asm6x ignores --output_file and always writes <basename>.obj next to source.
( cd "$WORKDIR" && "$TI/asm6x" --silicon_version=6740 --abi=eabi "$NAME.asm" )

echo "== link (dynamic shared object) =="
"$TI/lnk6x" \
    --dynamic=lib \
    --entry_point="$ENTRY" \
    --soname="$SONAME" \
    --stack_size=0 --heap_size=0 \
    --forced_static_binding=on \
    --retain="Fx_*" --retain="_Fx_*" \
    --retain=__pop_rts --retain=__push_rts --retain=__call_stub \
    --retain="$ENTRY" \
    --output_file="$WORKDIR/$NAME.out" \
    "$CMD" "$WORKDIR/$NAME.obj" $EXTRA_OBJS

# decode_effect.py's --donor flag names the ELF that donates the new DATA
# blob; the positional argument is the container it is packed into.
echo "== pack into ZD2 container ($CONTAINER) =="
"$PY" "$DECODE" --donor "$WORKDIR/$NAME.out" --donor-elf \
    -o "$WORKDIR/${NAME}_rebuilt.ZD2" "$CONTAINER"

if [ "$STRIP" = 1 ]; then
    echo "== strip DWARF/symtab from the packaged blob (default; --no-strip to keep) =="
    "$PY" "$ROOT/tools/zd2_strip.py" "$WORKDIR/${NAME}_rebuilt.ZD2" \
        -o "$WORKDIR/${NAME}_rebuilt.stripped.ZD2"
    mv "$WORKDIR/${NAME}_rebuilt.stripped.ZD2" "$WORKDIR/${NAME}_rebuilt.ZD2"
else
    echo "== NOT stripping (--no-strip): blob keeps DWARF/symtab =="
fi

echo "== validate CRC + round-trip =="
"$PY" "$DECODE" -s -V "$WORKDIR/${NAME}_rebuilt.ZD2" | tail -2
"$PY" "$ROOT/tools/zd2_roundtrip.py" "$WORKDIR/${NAME}_rebuilt.ZD2"

echo "== done: $WORKDIR/${NAME}_rebuilt.ZD2 =="
