#!/usr/bin/env python3
"""Bake seamless sustain loops into multisample WAVs — the bank's loop engine.

Designed to run unattended across the whole library. For each WAV it:
  1. Decides loopability — sustained sound vs percussive (decays away -> one-shot).
  2. Picks a LONG loop (slow, natural movement, no fast cycling).
  3. Chooses the loop END so it matches the loop START on BOTH:
        - level  (envelope within a couple dB)  -> no pump / breathing
        - waveform phase (cross-correlation, both channels) -> no click
  4. Bakes a linear crossfade at the matched seam for the final polish.
No envelope-flattening: the natural tone/shimmer is left untouched (and there's
no gain step at the loop boundary, which was the end-glitch).

Loop points are written to loops.json so the Ableton/SFZ builders use the exact
region with the crossfade already in the audio.

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


def _estimate_f0(mono: np.ndarray, sr: int) -> float:
    """Rough fundamental (Hz) via autocorrelation on a steady chunk; 0 if unsure."""
    a = int(0.6 * sr)
    seg = mono[a:a + int(0.2 * sr)] if len(mono) > a + int(0.2 * sr) else mono
    seg = seg - seg.mean()
    if seg.size < 64 or np.sqrt(np.mean(seg * seg)) < 1e-5:
        return 0.0
    ac = np.correlate(seg, seg, mode="full")[seg.size - 1:]
    lo, hi = int(sr / 1000), min(int(sr / 30), len(ac) - 1)   # search 30..1000 Hz
    if hi <= lo:
        return 0.0
    lag = lo + int(np.argmax(ac[lo:hi]))
    return sr / lag if lag > 0 else 0.0


def attack_end_sample(mono: np.ndarray, sr: int) -> int:
    """Sample where the attack has SETTLED — the first moment the envelope reaches
    within 1.5 dB of its peak. Robust to beating/detune and to fades (it's relative to
    peak, not a drift test). The loop must start AFTER this so the crossfade pre-roll is
    steady tone, not still-rising attack — that's what made slow-attack pads (Jupiter Pad
    @1.3s attack) loop sloppily when the start was hardcoded to 1.0s."""
    hop = int(0.005 * sr)
    win = int(0.02 * sr)
    n = len(mono)
    if n < win * 2:
        return 0
    env = np.array([np.sqrt(np.mean(mono[i:i + win] ** 2)) for i in range(0, n - win, hop)])
    edb = 20 * np.log10(env + 1e-9)
    peak = float(edb.max())
    for i in range(len(edb)):
        if edb[i] >= peak - 1.5:
            return i * hop
    return int(np.argmax(edb)) * hop


def _find_long_loop(audio: np.ndarray, sr: int):
    """Return (loop_start, loop_end) frames for a level+phase matched long loop,
    or None when the sample is too short or percussive (should ring out)."""
    mono = audio.mean(axis=1) if audio.ndim > 1 else audio
    n = len(mono)
    if n < int(2.0 * sr):
        return None

    win = int(0.03 * sr)
    envdb = 20 * np.log10(
        np.array([np.sqrt(np.mean(mono[i:i + win] ** 2)) for i in range(0, n - win, win)]) + 1e-9
    )
    nwin = len(envdb)

    def edb(idx: int) -> float:
        return float(envdb[min(nwin - 1, max(0, idx // win))])

    attack = min(int(0.40 * sr), int(0.25 * n))
    tail_guard = int(0.30 * sr)
    region_hi = n - tail_guard
    if region_hi - attack < int(1.0 * sr):
        return None

    # NOTE: loop-vs-one-shot is decided per patch by the manifest "loop" flag
    # (passed via --no-loop), NOT guessed here. The old heuristic compared the
    # start of the note to its END — but the end is the release tail, which always
    # decays, so it wrongly flagged sustained sounds (mega-saw, pads) as one-shots.
    rz = _rising_zc(mono)
    W = int(0.04 * sr)                       # 40ms correlation window
    # Start the loop AFTER the measured attack (+0.3s of steady tone), not a hardcoded
    # 1.0s. A slow-attack pad (Jupiter Pad ~1.3s) was being looped mid-swell at 1.0s, so
    # the crossfade blended rising audio against rising audio = smear. Now both crossfade
    # regions sit in genuine steady state. Bounded so there's room for a long loop.
    atk = attack_end_sample(mono, sr)
    # Floor at 1.0s (the value that already worked for fast-attack sounds — keeps fat-lead
    # etc. unchanged), and push LATER only when the attack runs past it (the slow pads).
    loop_start_min = int(min(max(atk + int(0.30 * sr), int(1.0 * sr)), 0.45 * n))
    s_cand = rz[rz >= max(loop_start_min, W)]
    if len(s_cand) == 0:
        return None
    S = int(s_cand[0])
    sdb = edb(S)

    # long loop: look for the end in the late region
    search_lo = max(S + int(2.0 * sr), region_hi - int(2.5 * sr))
    e_cand = rz[(rz >= search_lo) & (rz <= region_hi)]
    if len(e_cand) == 0:
        e_cand = rz[(rz >= S + int(1.0 * sr)) & (rz <= region_hi)]
    if len(e_cand) == 0:
        return None

    # 1) keep only ends whose LEVEL matches the start (no pump); widen tol if needed
    chosen = None
    for tol in (1.5, 2.5, 4.0, 8.0):
        cands = [int(E) for E in e_cand if abs(edb(int(E)) - sdb) <= tol]
        if cands:
            chosen = cands
            break
    if not chosen:
        chosen = [int(E) for E in e_cand]

    # 2) among those, pick the best WAVEFORM-phase match (no click), both channels
    nch = audio.shape[1] if audio.ndim > 1 else 1

    def chan(c):
        return audio[:, c] if audio.ndim > 1 else audio

    refs = []
    for c in range(nch):
        r = chan(c)[S - W:S].astype(np.float64)
        r = r - r.mean()
        refs.append((r, float(np.sqrt(np.sum(r * r))) + 1e-12))

    best, best_E = -1e9, None
    for E in chosen:
        if E < W:
            continue
        tot = 0.0
        for c in range(nch):
            seg = chan(c)[E - W:E].astype(np.float64)
            seg = seg - seg.mean()
            sn = float(np.sqrt(np.sum(seg * seg))) + 1e-12
            tot += float(np.sum(refs[c][0] * seg)) / (refs[c][1] * sn)
        if tot > best:
            best, best_E = tot, E
    if best_E is None:
        return None
    return S, best_E


def _find_short_loop(audio: np.ndarray, sr: int):
    """For sounds with no flat sustain (a sub that fades the whole way): lock a SHORT
    loop of a few whole wave-cycles where the level barely moves -> a seamless cycle
    that HOLDS. The natural fade plays in up to the loop start, then it sustains there."""
    mono = audio.mean(axis=1) if audio.ndim > 1 else audio
    n = len(mono)
    f0 = _estimate_f0(mono, sr)
    if f0 <= 0:
        return None
    P = sr / f0                                    # period in samples
    rz = _rising_zc(mono)
    attack = min(int(0.5 * sr), int(0.25 * n))
    s_cand = rz[rz >= attack]
    if len(s_cand) == 0:
        return None
    S = int(s_cand[0])
    ncyc = max(4, int(round(0.08 * sr / P)))       # ~80ms worth of whole cycles
    target_E = S + int(round(ncyc * P))
    e_cand = rz[(rz > S + int(0.4 * ncyc * P)) & (rz < S + int(2.5 * ncyc * P))]
    if len(e_cand) == 0:
        return None
    E = int(e_cand[int(np.argmin(np.abs(e_cand - target_E)))])
    if E - S < int(0.02 * sr):                     # need at least ~20ms
        return None
    return S, E


def _f0_near(mono: np.ndarray, sr: int, at: int) -> float:
    """Fundamental (Hz) estimated right at the loop-start region via autocorrelation."""
    seg = mono[at:at + int(0.2 * sr)].astype(np.float64)
    seg = seg - seg.mean()
    if seg.size < 64 or np.sqrt(np.mean(seg * seg)) < 1e-5:
        return 0.0
    ac = np.correlate(seg, seg, "full")[seg.size - 1:]
    lo, hi = int(sr / 800), min(int(sr / 30), len(ac) - 1)
    return sr / (lo + int(np.argmax(ac[lo:hi]))) if hi > lo else 0.0


def _seam_jump(audio: np.ndarray, S: int, E: int):
    """Crossfade-0 forward-loop seam: the sample jump from E-1 back to S, vs a natural
    one-sample step at S. If the seam is no bigger than a natural step, the loop is
    seamless WITHOUT any crossfade (the Samples From Mars approach)."""
    a = audio
    s1 = min(len(a) - 1, S + 1)
    if a.ndim > 1:
        return float(np.max(np.abs(a[S] - a[E - 1]))), float(np.max(np.abs(a[s1] - a[S])))
    return abs(a[S] - a[E - 1]), abs(a[s1] - a[S])


def _phase_locked_loop(audio: np.ndarray, sr: int):
    """SFM recipe: a forward loop of a whole number of fundamental cycles between two
    rising zero-crossings, deep in the sustain, level-matched. Seamless with NO crossfade
    when the waveform repeats cleanly (subs, 303, sine, simple bass).

    Scans several loop lengths (long for a flat sound, shorter for one that still drifts)
    and returns the (S, E, level_match_dB) with the best combined seam + level. A short
    phase-locked loop avoids the PUMP a long loop would get on a sound that isn't dead flat."""
    mono = audio.mean(axis=1) if audio.ndim > 1 else audio
    n = len(mono); sr = int(sr)
    rz = _rising_zc(mono)
    atk = attack_end_sample(mono, sr)
    s_cand = rz[rz >= max(atk + int(0.10 * sr), int(0.30 * n))]
    if len(s_cand) == 0:
        return None
    S = int(s_cand[0])
    f0 = _f0_near(mono, sr, S)
    if f0 <= 0:
        return None
    P = sr / f0
    win = int(0.03 * sr)
    edb = lambda i: 20 * np.log10(np.sqrt(np.mean(mono[i:i + win] ** 2)) + 1e-9)
    sdb = edb(S)
    best = None
    for secs in (1.0, 0.7, 0.5, 0.35, 0.2):                  # prefer long, shrink if it drifts
        ncyc = max(8, round(secs * sr / P))
        target_E = S + int(round(ncyc * P))
        if target_E >= n - int(0.2 * sr):
            continue
        cand = rz[(rz >= target_E - int(2 * P)) & (rz <= target_E + int(2 * P)) & (rz < n - int(0.2 * sr))]
        for E in cand:
            E = int(E)
            cyc = (E - S) / P
            lvl = abs(edb(E) - sdb)
            seam = float(np.max(np.abs(audio[S] - audio[E - 1]))) if audio.ndim > 1 else abs(mono[S] - mono[E - 1])
            score = seam + lvl * 0.5 + abs(cyc - round(cyc)) * 0.2    # click + pump + phase
            if best is None or score < best[0]:
                best = (score, S, E, lvl)
    if best is None:
        return None
    return best[1], best[2], best[3]


def find_loop(audio: np.ndarray, sr: int):
    """Returns (loop_start, loop_end, crossfade_samples) or None.

    Hybrid, self-selecting (reverse-engineered from Samples From Mars):
      1. Try a phase-locked forward loop with NO crossfade — if the raw seam is as clean
         as a natural sample step, use it (subs, 303, simple bass). This is the real fix:
         crossfading was a band-aid for imperfect points; here the points are perfect.
      2. Else a long level+phase matched loop WITH a 7% crossfade to hide the residual
         seam (rich/detuned saws and pads — these already sounded good that way).
      3. Else a short whole-cycle loop, no crossfade (fading sounds that won't level-match).
    """
    pl = _phase_locked_loop(audio, sr)
    if pl:
        S, E, lvl = pl
        seam, nat = _seam_jump(audio, S, E)
        # accept only if BOTH clean: no click (seam) AND no pump (level within ~1.5 dB)
        if seam <= max(nat * 2.5, nat + 1e-4) and lvl <= 1.5:
            return S, E, 0                          # clean -> forward, NO crossfade
    ll = _find_long_loop(audio, sr)
    if ll:
        mono = audio.mean(axis=1) if audio.ndim > 1 else audio
        win = int(0.03 * sr)
        S, E = ll
        sdb = 20 * np.log10(np.sqrt(np.mean(mono[S:S + win] ** 2)) + 1e-9)
        edb_ = 20 * np.log10(np.sqrt(np.mean(mono[max(0, E - win):E] ** 2)) + 1e-9)
        if abs(sdb - edb_) <= 4.0:
            xf = min(int(0.07 * (E - S)), max(0, S - int(0.45 * sr)))
            return S, E, xf
    sl = _find_short_loop(audio, sr)
    if sl:
        return sl[0], sl[1], 0
    if ll:
        xf = min(int(0.07 * (ll[1] - ll[0])), max(0, ll[0] - int(0.45 * sr)))
        return ll[0], ll[1], xf
    return None


def bake_linear_xfade(audio: np.ndarray, S: int, E: int, X: int) -> np.ndarray:
    """Linear crossfade the X frames before E with the X before S (per channel).
    Right kind once the ends are already level+phase matched."""
    i = np.arange(X)
    a = i / X
    g_out, g_in = 1.0 - a, a
    if audio.ndim > 1:
        for c in range(audio.shape[1]):
            audio[E - X:E, c] = audio[E - X:E, c] * g_out + audio[S - X:S, c] * g_in
    else:
        audio[E - X:E] = audio[E - X:E] * g_out + audio[S - X:S] * g_in
    return audio


def loop_metrics(audio: np.ndarray, sr: int, S: int, E: int) -> dict:
    """Quantify loop quality for verification."""
    mono = audio.mean(axis=1) if audio.ndim > 1 else audio
    win = int(0.03 * sr)
    edb = lambda idx: 20 * np.log10(np.sqrt(np.mean(mono[idx:idx + win] ** 2)) + 1e-9)
    seam_jump = float(np.max(np.abs(audio[S] - audio[E - 1]))) if audio.ndim > 1 else float(abs(mono[S] - mono[E - 1]))
    nat_jump = float(np.max(np.abs(audio[S] - audio[S - 1]))) if audio.ndim > 1 else float(abs(mono[S] - mono[S - 1]))
    return {
        "len_s": (E - S) / sr,
        "level_match_db": abs(edb(S) - edb(max(0, E - win))),
        "seam_jump": seam_jump,
        "natural_jump": nat_jump,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="patch chain dir of WAVs to loop")
    ap.add_argument("--xfade-ms", type=float, default=150.0, help="crossfade length at the seam")
    ap.add_argument("--no-loop", action="store_true", help="force one-shot (no loop) — for percussive patches (plucks, bells)")
    ap.add_argument("--dry-run", action="store_true", help="report loop choice + metrics, don't write")
    args = ap.parse_args()

    d = Path(args.dir).resolve()
    wavs = sorted(d.glob("*.wav"))
    loops = {}
    looped = 0
    for w in wavs:
        audio, sr = sf.read(str(w))
        lp = None if args.no_loop else find_loop(audio, sr)
        if not lp:
            loops[w.name] = None
            print(f"  {w.name}: one-shot (ring out)")
            continue
        S, E, xf = lp
        m = loop_metrics(audio, sr, S, E)
        # Points + the recommended crossfade (0 = phase-locked, seamless on its own; the
        # SFM way). Ableton handles the crossfade on the pristine WAV — nothing baked in.
        loops[w.name] = {"start": int(S), "end": int(E), "crossfade": int(xf)}
        looped += 1
        kind = "forward, NO crossfade (phase-locked)" if xf == 0 else f"forward + {xf/sr*1000:.0f}ms crossfade"
        print(f"  {w.name}: loop {S/sr:.2f}->{E/sr:.2f}s ({m['len_s']:.1f}s)  "
              f"level-match {m['level_match_db']:.2f}dB  · {kind}")

    if not args.dry_run:
        (d / "loops.json").write_text(json.dumps(loops, indent=2))
    print(f"{looped}/{len(wavs)} looped" + ("  [dry-run]" if args.dry_run else f" · sidecar -> {d/'loops.json'}"))


if __name__ == "__main__":
    main()
