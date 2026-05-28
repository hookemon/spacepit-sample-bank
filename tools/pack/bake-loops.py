#!/usr/bin/env python3
"""Bake seamless sustain loops into multisample WAVs.

For each WAV: pick a loop start after the attack, then CROSS-CORRELATION-match a
loop end in the late steady region so the waveform AND the beating phase line up
(this is what kills the audible "loop" on rich/detuned sounds). Bake a short
LINEAR crossfade at the seam — the right kind once the two ends already match —
so the jump is click-free. Record the loop points to loops.json so the Ableton
(and future SFZ/Decent) builders use the exact same region. Decaying / short
percussive samples are detected and left alone as one-shots.

  usage: bake-loops.py --dir instruments/jp8000/patches/supersaw-1/raw
"""
import argparse
import json
from pathlib import Path

import numpy as np
import soundfile as sf


def _rising_zc(mono: np.ndarray) -> np.ndarray:
    pos = mono > 0
    return np.where((~pos[:-1]) & (pos[1:]))[0]


def find_loop(audio: np.ndarray, sr: int):
    """Return (loop_start, loop_end) sample frames, or None for a one-shot."""
    mono = audio.mean(axis=1) if audio.ndim > 1 else audio
    n = len(mono)
    if n < int(1.2 * sr):
        return None
    attack = min(int(0.5 * sr), int(0.30 * n))
    tail_guard = int(0.30 * sr)
    min_loop = int(1.0 * sr)
    search_window = int(1.5 * sr)   # search the last ~1.5s of steady region for the match
    W = int(0.04 * sr)              # 40ms correlation window
    if attack < W:
        return None

    rz = _rising_zc(mono)
    s_cand = rz[rz >= attack]
    if len(s_cand) == 0:
        return None
    S = int(s_cand[0])
    if S < W:
        return None

    search_hi = n - tail_guard
    search_lo = max(S + min_loop, search_hi - search_window)
    e_cand = rz[(rz >= search_lo) & (rz <= search_hi)]
    if len(e_cand) == 0:
        return None

    nch = audio.shape[1] if audio.ndim > 1 else 1

    def chan(c):
        return audio[:, c] if audio.ndim > 1 else audio

    refs = []
    for c in range(nch):
        r = chan(c)[S - W:S].astype(np.float64)
        r = r - r.mean()
        refs.append((r, float(np.sqrt(np.sum(r * r))) + 1e-12))

    best, best_E = -1e9, None
    for Ec in e_cand:
        Ec = int(Ec)
        if Ec < W:
            continue
        tot = 0.0
        for c in range(nch):
            seg = chan(c)[Ec - W:Ec].astype(np.float64)
            seg = seg - seg.mean()
            sn = float(np.sqrt(np.sum(seg * seg))) + 1e-12
            tot += float(np.sum(refs[c][0] * seg)) / (refs[c][1] * sn)
        if tot > best:
            best, best_E = tot, Ec
    if best_E is None:
        return None

    # decay guard — compare loop's first half vs second half over long windows so
    # chorus/beating swings don't fool it; skip looping if it's decaying away.
    mid = (S + best_E) // 2
    rms_first = float(np.sqrt(np.mean(mono[S:mid] ** 2)))
    rms_second = float(np.sqrt(np.mean(mono[mid:best_E] ** 2)))
    if rms_first <= 0 or rms_second < 0.5 * rms_first:
        return None
    return S, best_E


def bake_linear_xfade(audio: np.ndarray, S: int, E: int, X: int) -> np.ndarray:
    """Linear (equal-gain) crossfade the X frames before E with the X before S."""
    i = np.arange(X)
    a = i / X
    g_out = 1.0 - a
    g_in = a
    if audio.ndim > 1:
        for c in range(audio.shape[1]):
            audio[E - X:E, c] = audio[E - X:E, c] * g_out + audio[S - X:S, c] * g_in
    else:
        audio[E - X:E] = audio[E - X:E] * g_out + audio[S - X:S] * g_in
    return audio


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="patch chain dir of WAVs to loop")
    ap.add_argument("--xfade-ms", type=float, default=40.0, help="crossfade length at the seam")
    args = ap.parse_args()

    d = Path(args.dir).resolve()
    wavs = sorted(d.glob("*.wav"))
    loops = {}
    looped = 0
    for w in wavs:
        audio, sr = sf.read(str(w))
        lp = find_loop(audio, sr)
        if not lp:
            loops[w.name] = None
            print(f"  {w.name}: one-shot (ring out)")
            continue
        S, E = lp
        X = int(min(args.xfade_ms / 1000 * sr, (E - S) // 4, S))
        if X > 0:
            audio = bake_linear_xfade(audio, S, E, X)
            sf.write(str(w), audio, sr, subtype="PCM_24")
        loops[w.name] = {"start": int(S), "end": int(E)}
        looped += 1
        print(f"  {w.name}: loop {S/sr:.2f}->{E/sr:.2f}s  ({(E-S)/sr:.2f}s long, {X/sr*1000:.0f}ms xfade)")

    (d / "loops.json").write_text(json.dumps(loops, indent=2))
    print(f"{looped}/{len(wavs)} looped · sidecar -> {d/'loops.json'}")


if __name__ == "__main__":
    main()
