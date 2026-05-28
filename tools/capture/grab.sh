#!/usr/bin/env bash
# grab.sh — capture one patch, auto-trim the dead air, verify. One move per patch.
#
#   usage: grab.sh <instrument> <patch> [note-range] [step]
#   e.g.:  grab.sh jp8000 mega-saw C2-C5 3
#
# Captures whatever sound is currently dialed on the synth (no PC fired — you
# dial the patch by hand so the name is always truthful). Ranges are C-rooted
# so they drop into Ableton's Sampler clean (default root C3). Trim = remove
# front silence to a 5ms lead-in + tail fade; levels left natural (no normalize).
# Trims in place — no backup copies left behind.
set -euo pipefail

INST="${1:?need instrument slug}"
PATCH="${2:?need patch name}"
RANGE="${3:-C2-C5}"
STEP="${4:-3}"
GAIN="${5:-3}"   # make-up gain dB so the patch opens hot ("rip right as you open it")

ROOT="/Users/nickhook/projects/spacepit-sample-bank"
PY="$ROOT/tools/capture/.venv/bin/python"
cd "$ROOT"

echo "=== grab: $INST / $PATCH  ($RANGE step $STEP) ==="
"$PY" tools/capture/capture-synth.py \
  --instrument "$INST" --patch "$PATCH" --chain raw \
  --midi-port mio --midi-channel 1 \
  --audio-device TX-6 --input-channels 1,2 \
  --note-range "$RANGE" --step "$STEP" \
  --velocities 100 --round-robins 1 \
  --sustain-sec 4 --tail-sec 2.5 \
  --sample-rate 48000 --bit-depth 24 --gain-db "$GAIN" -y 2>&1 | tail -27

# (trim + clip-safe make-up gain now happen inside capture-synth.py's post-clean)

"$PY" - "$INST" "$PATCH" <<'PYEOF'
import soundfile as sf, numpy as np, glob, os, sys
inst, patch = sys.argv[1], sys.argv[2]
files = sorted(glob.glob(f'instruments/{inst}/patches/{patch}/raw/*.wav'))
print(f'\n--- {patch}: {len(files)} trimmed WAVs ---')
bad = 0
for f in files:
    a, sr = sf.read(f); m = a.mean(axis=1) if a.ndim > 1 else a
    thr = 10**(-45/20)
    idx = int(np.argmax(np.abs(m) > thr)) if np.any(np.abs(m) > thr) else -1
    front = idx*1000/sr if idx >= 0 else -1
    peak = 20*np.log10(float(np.max(np.abs(m))) + 1e-9)
    flag = ''
    if peak < -40: flag = '  <-- SILENT'; bad += 1
    name = os.path.basename(f).replace(f'{inst}_{patch}_', '').replace('_v100_rr1.wav', '')
    print(f'  {name:<6} {len(a)/sr:5.2f}s  front={front:4.1f}ms  peak={peak:6.1f}dB{flag}')
print('  ✓ all good' if not bad else f'  ⚠ {bad} SILENT — recheck the synth/level')
PYEOF
echo "=== done: $INST / $PATCH ==="
