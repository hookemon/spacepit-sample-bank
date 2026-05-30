#!/usr/bin/env python3
"""Turn a recording into a PERFECT loop: start dead on the first transient, then trim to EXACTLY
N bars at the given BPM (pure math) — so it drops onto the DAW grid with no dead air and loops.

You tell it the BPM + bars (you know them — you recorded at that tempo). It finds the downbeat
and does the arithmetic: samples = bars * (60/bpm) * beats_per_bar * sample_rate.

  python perfect-loop.py --wav take.wav --bpm 120 --bars 4

Tip: leave a beat or two of tail past the last bar when you record — that gives a seamless
sample-continuous wrap (the loop's start is blended from the audio just past the loop point).
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import soundfile as sf


def find_transient(mono: np.ndarray, sr: int, thresh_db: float = -40.0) -> int:
    """First sample above the gate (relative to peak) = the downbeat. Snapped back to the nearest
    zero-crossing within ~3ms so the loop starts clean, with no leading silence and no edge click."""
    a = np.abs(mono)
    pk = float(a.max()) or 1.0
    thr = pk * (10.0 ** (thresh_db / 20.0))
    idx = np.where(a > thr)[0]
    if len(idx) == 0:
        return 0
    onset = int(idx[0])
    lo = max(1, onset - int(0.003 * sr))
    for j in range(onset, lo, -1):
        if (mono[j - 1] <= 0 < mono[j]) or (mono[j - 1] >= 0 > mono[j]):
            return j
    return onset


def make_loop(audio: np.ndarray, sr: int, bpm: float, bars: int, meter: int = 4, xfade_ms: float = 14.0):
    """Returns (loop, onset, loop_len). Transient-aligned, EXACTLY bars long, seam-wrapped."""
    mono = audio.mean(axis=1) if audio.ndim > 1 else audio
    onset = find_transient(mono, sr)
    loop_len = int(round(bars * (60.0 / bpm) * meter * sr))   # the whole point: exact bar math
    end = onset + loop_len
    if end > len(audio):
        short = (end - len(audio)) / sr
        raise ValueError(f"recording is {short:.2f}s too short — need {loop_len/sr:.3f}s of audio "
                         f"from the transient. Record a little more.")
    loop = audio[onset:end].astype(np.float64).copy()
    # sample-continuous wrap: blend the loop's head with the audio just PAST the loop point, so
    # loop[0] == audio[end] (continuous with loop[-1] == audio[end-1]). Needs a bit of tail.
    X = int(min(round(xfade_ms / 1000 * sr), loop_len // 8, len(audio) - end))
    wrapped = False
    if X > 8:
        cont = audio[end:end + X].astype(np.float64)
        if cont.ndim == 1:
            cont = cont[:, None]
        fin = np.linspace(0.0, 1.0, X)[:, None]
        loop[:X] = loop[:X] * fin + cont * (1.0 - fin)
        wrapped = True
    pk = float(np.max(np.abs(loop)))
    if pk > 0.999:
        loop *= 0.999 / pk
    return loop.astype(np.float32), onset, loop_len, wrapped


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wav", required=True, help="the recording to loop")
    ap.add_argument("--bpm", type=float, required=True, help="the tempo you recorded at")
    ap.add_argument("--bars", type=int, default=4)
    ap.add_argument("--meter", type=int, default=4, help="beats per bar (4 = 4/4)")
    ap.add_argument("--xfade-ms", type=float, default=14.0, help="seam wrap length (needs tail)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    wav = Path(args.wav).expanduser()
    if not wav.exists():
        print(f"✗ not found: {wav}"); sys.exit(1)
    audio, sr = sf.read(str(wav), always_2d=True)
    try:
        loop, onset, loop_len, wrapped = make_loop(audio, sr, args.bpm, args.bars, args.meter, args.xfade_ms)
    except ValueError as e:
        print(f"✗ {e}"); sys.exit(2)

    out = Path(args.out).expanduser() if args.out else wav.with_name(
        f"{wav.stem}_loop_{int(args.bpm)}bpm_{args.bars}bar.wav")
    sf.write(str(out), loop, sr, subtype=sf.info(str(wav)).subtype)
    print(f"✓ {out.name}")
    print(f"  transient: trimmed {onset/sr*1000:.0f}ms of lead-in — loop starts ON the downbeat")
    print(f"  length:    exactly {args.bars} bars @ {args.bpm}bpm = {loop_len/sr:.3f}s = {loop_len} samples")
    print(f"  wrap:      {'seamless (blended from tail)' if wrapped else 'no tail — bar-exact but record a beat extra for a seamless seam'}")


if __name__ == "__main__":
    main()
