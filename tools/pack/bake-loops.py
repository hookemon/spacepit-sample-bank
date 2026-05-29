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
import os
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


def accurate_period(mono: np.ndarray, sr: int, at: int) -> float:
    """Sub-sample fundamental period via parabolic interpolation of the autocorrelation peak.
    Accuracy matters: over a long loop the period error multiplies, so a rough integer-lag
    estimate drifts off a whole cycle. Parabolic refine gets it to a fraction of a sample."""
    seg = mono[at:at + int(0.3 * sr)].astype(np.float64)
    seg = seg - seg.mean()
    if seg.size < 128 or np.sqrt(np.mean(seg * seg)) < 1e-5:
        return 0.0
    ac = np.correlate(seg, seg, "full")[seg.size - 1:]
    lo, hi = int(sr / 400), min(int(sr / 30), len(ac) - 2)
    if hi <= lo:
        return 0.0
    k = lo + int(np.argmax(ac[lo:hi]))
    y0, y1, y2 = ac[k - 1], ac[k], ac[k + 1]
    d = (y0 - 2 * y1 + y2)
    return k + (0.5 * (y0 - y2) / d if d != 0 else 0.0)


def _repeat_at_loop(mono: np.ndarray, sr: int):
    """How well the waveform matches itself ~one loop (~0.7s) later. High (>=0.80) = the sound
    REPEATS over a loop length (sub, 303, simple bass) -> an integer-cycle loop with NO
    crossfade is seamless. Low = detuned/beating (supersaw, pads, leads) that never line up
    -> needs a crossfade to mask the seam. This is what routes each patch automatically."""
    n = len(mono); S = int(0.40 * n); P = accurate_period(mono, sr, S)
    if P <= 0:
        return 0.0, 0.0
    for secs in (0.7, 0.5, 0.4):
        N = int(round(secs * sr / P)); E = S + int(round(N * P)); W = int(max(0.05 * sr, 8 * P))
        if E + W < n:
            a1 = mono[S:S + W] - np.mean(mono[S:S + W]); a2 = mono[E:E + W] - np.mean(mono[E:E + W])
            return float(np.sum(a1 * a2) / ((np.sqrt(np.sum(a1 * a1)) * np.sqrt(np.sum(a2 * a2))) + 1e-12)), P
    return 0.0, P


def _integer_cycle_loop(audio: np.ndarray, sr: int, P: float):
    """Loop end = EXACT integer number of cycles from start: E = S + round(N*P), with a
    sub-sample-accurate period P. No zero-crossing snap on E — snapping pushes E off the
    whole-cycle alignment and THAT is the click. Because E is a whole number of periods
    after S, it lands at the same phase as S automatically (S is at a rising zero crossing,
    so E is too). Among the phase-locked candidates, pick the one whose level best matches S
    — a short loop on a decaying sub barely drifts, so no flattening is needed."""
    mono = audio.mean(axis=1) if audio.ndim > 1 else audio
    n = len(mono); sr = int(sr)
    rz = _rising_zc(mono)
    s_cand = rz[rz >= int(0.40 * n)]
    if len(s_cand) == 0 or P <= 0:
        return None
    S = int(s_cand[0])
    best = None
    win = int(0.03 * sr)
    rms_s_db = 20 * np.log10(float(np.sqrt(np.mean(mono[S:S + win] ** 2)) + 1e-9))
    for N in range(max(4, int(0.08 * sr / P)), int(1.4 * sr / P)):
        E = S + int(round(N * P))               # exact whole cycles — no snap
        if E + win >= n - int(0.1 * sr):
            break
        residual = abs(round(N * P) - N * P)              # sub-sample phase error, 0..0.5
        rms_e_db = 20 * np.log10(float(np.sqrt(np.mean(mono[E - win:E] ** 2)) + 1e-9))
        level_db = abs(rms_s_db - rms_e_db)
        # phase alignment dominates (a click is worse than a tiny level step); among
        # well-aligned candidates, prefer the best level match.
        score = residual * 2.0 + max(0.0, level_db - 1.5) * 0.10
        if best is None or score < best[0]:
            best = (score, E)
    return (S, best[1]) if best else None


def _single_cycle_loop(audio, sr):
    """Loop exactly ONE period, rising-ZC to rising-ZC. The classic hardware-sampler way to
    sustain a tone: one cycle repeated = a steady pitch, no beating accumulates, no crossfade
    needed (both ends sit at a rising zero crossing one period apart). Best for a clean but
    slightly-detuned sub where longer loops click/warble."""
    mono = audio.mean(axis=1) if audio.ndim > 1 else audio
    n = len(mono)
    P = accurate_period(mono, sr, int(0.40 * n))
    if P <= 0:
        return None
    rz = _rising_zc(mono)
    s_cand = rz[rz >= int(0.45 * n)]              # steady region, past attack
    if len(s_cand) == 0:
        return None
    S = int(s_cand[0])
    target = S + P
    cand = rz[rz > S]
    if len(cand) == 0:
        return None
    E = int(cand[np.argmin(np.abs(cand - target))])   # rising ZC closest to one period later
    if E <= S:
        return None
    return S, E, 0, 'integer'


def _integer_with_xfade(audio, sr, P):
    """Integer-cycle loop + short (~12ms) in-phase crossfade. On a flat periodic tone the
    crossfade blends identical samples (no change); on a decaying tone (the JP sub decays
    the whole note) it smooths the level step that would otherwise click. Integer-cycle
    keeps it in phase, so it never combs into a 'vowel'. Measured: 0xf=42x seam, 12ms=3x."""
    ic = _integer_cycle_loop(audio, sr, P)
    if not ic:
        return None
    S, E = ic
    xf = max(0, min(int(0.012 * sr), (E - S) // 2, S))
    return S, E, xf, 'integer'


def find_loop(audio: np.ndarray, sr: int, force=None):
    """Return (loop_start, loop_end, crossfade_samples, route). route is 'integer' or
    'crossfade' so the majority-vote in main() can re-route outliers reliably (the
    crossfade value alone is ambiguous — fallbacks also use 0). force overrides the
    discriminator."""
    if os.environ.get("BAKE_SINGLE_CYCLE"):
        sc = _single_cycle_loop(audio, sr)
        if sc:
            return sc
    mono = audio.mean(axis=1) if audio.ndim > 1 else audio
    rep, P = _repeat_at_loop(mono, sr)
    use_integer = (rep >= 0.80 and P > 0) if force is None else (force == 'integer')
    if use_integer and P > 0:
        r = _integer_with_xfade(audio, sr, P)
        if r:
            return r
    # rich/detuned -> long level-matched loop WITH crossfade (keeps Euro SAW + pads clean)
    ll = _find_long_loop(audio, sr)
    if ll:
        win = int(0.03 * sr); S, E = ll
        sdb = 20 * np.log10(np.sqrt(np.mean(mono[S:S + win] ** 2)) + 1e-9)
        edb_ = 20 * np.log10(np.sqrt(np.mean(mono[max(0, E - win):E] ** 2)) + 1e-9)
        if abs(sdb - edb_) <= 4.0:
            xf = min(int(0.07 * (E - S)), max(0, S - int(0.45 * sr)))
            return S, E, xf, 'crossfade'
    # forced integer but the long path was requested? fall back to integer anyway so a
    # majority-integer patch stays consistent.
    if force == 'integer' and P > 0:
        r = _integer_with_xfade(audio, sr, P)
        if r:
            return r
    sl = _find_short_loop(audio, sr)
    if sl:
        return sl[0], sl[1], 0, 'crossfade'
    if ll:
        xf = min(int(0.07 * (ll[1] - ll[0])), max(0, ll[0] - int(0.45 * sr)))
        return ll[0], ll[1], xf, 'crossfade'
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

    # Manual loops from the bench loop editor — Nick set these by ear, they WIN over any
    # auto-detection. {filename: {start,end,crossfade}}.
    manual = {}
    mlf = d / "manual_loops.json"
    if mlf.exists():
        try:
            manual = json.loads(mlf.read_text())
            if manual:
                print(f"  [manual loops: {len(manual)} note(s) set by ear — overriding auto]")
        except Exception:
            manual = {}

    # Pass 1 — compute routing for every note independently (manual notes skip auto entirely)
    pass1 = []
    for w in wavs:
        audio, sr = sf.read(str(w))
        if w.name in manual and not args.no_loop:
            mm = manual[w.name]
            # Force crossfade 0 — the loop editor auditions a HARD loop (Web Audio), so the
            # build must hard-loop too or it won't match what Nick heard. Ableton Crossfade=0
            # = the same end→start jump. This is the editor-vs-build fix.
            lp = (int(mm["start"]), int(mm["end"]), 0, 'manual')
        else:
            lp = None if args.no_loop else find_loop(audio, sr)
        pass1.append((w, audio, sr, lp))

    # Majority vote — if ≥60% of notes agree on a route, force the outliers to match.
    # Keeps every note in a patch consistent (no mix of xfade/no-xfade within the same
    # patch, which Nick can hear and has to manually fix). Routes by the explicit 'route'
    # tag, not the crossfade value (fallbacks also use 0, which fooled the old check).
    if not args.no_loop:
        n_integer = sum(1 for _, _, _, lp in pass1 if lp and lp[3] == 'integer')
        n_xfade   = sum(1 for _, _, _, lp in pass1 if lp and lp[3] == 'crossfade')
        total_looped = n_integer + n_xfade
        if total_looped > 0:
            if n_integer / total_looped >= 0.60 and n_xfade > 0:
                majority = 'integer'
            elif n_xfade / total_looped >= 0.60 and n_integer > 0:
                majority = 'crossfade'
            else:
                majority = None
            if majority:
                print(f"  [majority-vote: {majority} ({n_integer}i/{n_xfade}x) — re-routing outliers]")
                pass1 = [
                    (w, audio, sr, find_loop(audio, sr, force=majority)
                     if (lp and lp[3] not in ('manual', majority)) else lp)
                    for w, audio, sr, lp in pass1
                ]

    for w, audio, sr, lp in pass1:
        if not lp:
            loops[w.name] = None
            print(f"  {w.name}: one-shot (ring out)")
            continue
        S, E, xf, route = lp
        m = loop_metrics(audio, sr, S, E)
        tag = f"{route}/{xf}smp-xf" if xf else f"{route}/0xf"
        loops[w.name] = {"start": int(S), "end": int(E), "crossfade": int(xf)}
        looped += 1
        print(f"  {w.name}: [{tag}] loop {S/sr:.2f}->{E/sr:.2f}s ({m['len_s']:.1f}s)  "
              f"level-match {m['level_match_db']:.2f}dB")

    if not args.dry_run:
        (d / "loops.json").write_text(json.dumps(loops, indent=2))
    print(f"{looped}/{len(wavs)} looped" + ("  [dry-run]" if args.dry_run else f" · sidecar -> {d/'loops.json'}"))


if __name__ == "__main__":
    main()
