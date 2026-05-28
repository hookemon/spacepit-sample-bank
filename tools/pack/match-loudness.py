#!/usr/bin/env python3
"""Measure + match perceived loudness across an instrument's patches.

Peak-normalizing (what the cleaner does) keeps nothing clipping, but a dense
7-saw and a pure sub bass that both peak at -0.5 dBFS are NOT equally loud to
the ear — the saw is much louder. This matches every patch to a common loudness
target so switching patches in Ableton feels even, while holding a true-peak
safety ceiling so nothing clips.

Loudness proxy: the loudest 300 ms short-term RMS of each note (works for both
sustained pads and percussive plucks/bells), aggregated per patch by median.
Gain is per-patch (preserves each patch's natural note-to-note dynamics); the
per-note spread is reported so we can spot any patch that needs evening.

  check:  match-loudness.py --dir instruments/jp8000/patches --dry-run
  bake:   match-loudness.py --dir <pack>/audio/multisamples --write-sidecar
"""
import argparse
import json
import math
from pathlib import Path

import numpy as np
import soundfile as sf


def db(x: float) -> float:
    return 20 * math.log10(x + 1e-12)


def note_loudness(path: Path):
    """Return (loudest-300ms RMS linear, true peak linear) for one WAV."""
    a, sr = sf.read(str(path))
    mono = a.mean(axis=1) if a.ndim > 1 else a
    peak = float(np.max(np.abs(a))) if a.size else 0.0
    n = len(mono)
    if n == 0:
        return 0.0, 0.0
    win = min(int(0.30 * sr), n) or 1
    hop = max(1, int(0.10 * sr))
    best = 0.0
    for i in range(0, max(1, n - win + 1), hop):
        r = float(np.sqrt(np.mean(mono[i:i + win] ** 2)))
        if r > best:
            best = r
    return best, peak


def measure_patch(chain_dir: Path):
    louds, peaks = [], []
    for w in sorted(chain_dir.glob("*.wav")):
        l, p = note_loudness(w)
        louds.append(l)
        peaks.append(p)
    if not louds:
        return None
    return {
        "n": len(louds),
        "loud_db": db(float(np.median(louds))),
        "peak_db": db(float(np.max(peaks))),
        "spread_db": db(max(louds)) - db(min(louds)),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True,
                    help="instrument patches dir (instruments/<slug>/patches) or a pack multisamples dir")
    ap.add_argument("--chain", default="raw")
    ap.add_argument("--ceiling", type=float, default=-1.0, help="true-peak ceiling dBFS")
    ap.add_argument("--target", default="median",
                    help="'median' (match patches to each other) or a dBFS number")
    ap.add_argument("--dry-run", action="store_true", help="report only, don't write the sidecar")
    ap.add_argument("--write-sidecar", action="store_true",
                    help="write loudness-gains.json next to --dir (consumed by build-pack)")
    args = ap.parse_args()

    base = Path(args.dir).resolve()
    patches = {}
    for pd in sorted(base.iterdir()):
        if not pd.is_dir():
            continue
        cd = pd / args.chain
        if cd.is_dir():
            m = measure_patch(cd)
            if m:
                patches[pd.name] = m
    if not patches:
        print(f"no patches with WAVs under {base} (chain={args.chain})")
        return

    louds = [m["loud_db"] for m in patches.values()]
    target = float(np.median(louds)) if args.target == "median" else float(args.target)
    bank_spread = max(louds) - min(louds)

    print(f"target {target:.1f} dB · ceiling {args.ceiling:.1f} dBFS · {len(patches)} patches "
          f"· current patch-to-patch spread {bank_spread:.1f} dB")
    print(f"  {'patch':16s} {'loud':>6} {'peak':>6} {'want':>6} {'applied':>8} {'final':>6}  notes")
    gains = {}
    for name, m in sorted(patches.items(), key=lambda kv: kv[1]["loud_db"]):
        want = target - m["loud_db"]
        headroom = args.ceiling - m["peak_db"]       # max up-gain before peak hits ceiling
        applied = min(want, headroom)
        gains[name] = round(applied, 2)
        final = m["loud_db"] + applied
        flag = "  PEAK-LIMITED" if applied < want - 0.05 else ""
        spread = f"  spread {m['spread_db']:.1f}dB" if m["spread_db"] > 3.0 else ""
        print(f"  {name:16s} {m['loud_db']:6.1f} {m['peak_db']:6.1f} {want:+6.1f} {applied:+8.2f} {final:6.1f}{spread}{flag}")

    if args.write_sidecar and not args.dry_run:
        out = base / "loudness-gains.json"
        out.write_text(json.dumps(
            {"target_db": target, "ceiling_db": args.ceiling, "chain": args.chain, "gains_db": gains},
            indent=2))
        print(f"→ wrote {out}")


if __name__ == "__main__":
    main()
