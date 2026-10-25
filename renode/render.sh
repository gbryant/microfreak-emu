#!/bin/sh
# Render one note to a WAV with the Renode harness.
#   renode/render.sh FLASH.img NOTE OUT.wav [BOOT_S] [LEAD_S] [LENGTH_S]
# Build FLASH.img first:
#   uv run python -m mfemu.mkflash -o flash.img \
#       --presets-from CAPTURE.jsonl --max-presets 512
# About 80-110 seconds of wall clock for the default 17 s, host load
# depending.  No hooks, no watchpoints -- see render.resc.
set -e
here=$(cd "$(dirname "$0")" && pwd)
flash=$(cd "$(dirname "$1")" && pwd)/$(basename "$1"); note=$2; out=$3
boot=${4:-14}; lead=${5:-1}; length=${6:-2}
tmp=$(mktemp -t mfrender); raw=$tmp.raw; resc=$tmp.resc
# Variables must be set before render.resc's `?=` defaults are read, and
# Renode runs -e commands after the script, so wrap it.
cat > "$resc" <<RESC
\$flash = @$flash
\$note = $note
\$out = @$raw
\$boot = "$boot"
\$lead = "$lead"
\$length = "$length"
include @$here/render.resc
RESC
# The default came from a hardcoded ~/Downloads/Renode.app, which disagreed
# with paths.py's search -- on a machine with Renode in /Applications this
# script looked in the wrong place while every `mfemu` command found it. Ask
# paths.py instead, so there is one answer to "where is Renode".
R=${RENODE:-$MF_RENODE}
if [ -z "$R" ]; then
    # `|| R=` is required, not defensive: `set -e` is on, and an assignment
    # whose command substitution exits non-zero terminates the script -- so
    # without it, a machine with no python3 (or no src/ tree) died here
    # instead of falling through to the default below. Checked by running it.
    R=$(cd "$here/.." && python3 -c \
        'import sys; sys.path.insert(0, "src"); import mfemu.paths as P; print(P.RENODE)' \
        2>/dev/null) || R=
fi
R=${R:-$HOME/Downloads/Renode.app/Contents/MacOS}
# Up to 1.16 the macOS build was mono + bin/Renode.exe; from 1.17 it is a
# native `renode`. This used to launch the mono form unconditionally, so a
# 1.17 install failed here while `mfemu render` worked -- see
# mfrender.renode_command, which this mirrors. Native first: a 1.17 tree has
# no bin/Renode.exe, and a 1.16 tree has no `renode`.
if [ -x "$R/renode" ]; then
    set -- "$R/renode"
elif [ -f "$R/bin/Renode.exe" ]; then
    set -- mono "$R/bin/Renode.exe"
else
    echo "no Renode in $R -- looked for \`renode\` (1.17+) and" \
         "\`bin/Renode.exe\` (1.16 and earlier). Set RENODE or MF_RENODE." >&2
    exit 1
fi
# No --config here, unlike mfrender: this path renders and exits, and never
# saves or loads a snapshot, which is the only thing that flag is needed for.
(cd "$R" && "$@" --disable-xwt --console --plain "$resc" -e quit)
python3 - "$raw" "$out" <<'PY'
import sys, wave
raw, out = sys.argv[1:3]
data = open(raw, 'rb').read()
w = wave.open(out, 'wb'); w.setnchannels(2); w.setsampwidth(2); w.setframerate(96000)
w.writeframes(data); w.close()
print('wrote', out, len(data)//4, 'frames')
PY
rm -f "$raw" "$resc" "$tmp"
