#!/usr/bin/env python3
"""Slice one Ableton recording (all 13 notes played in sequence) into per-note WAVs.

The companion to the 13-note capture MIDI: you record the synth in Ableton in ONE pass
(each note held with a silent gap between), bounce it to a WAV, and this splits it back
into the named per-note files the bench/pack pipeline expects — no terminal capture needed.

Notes are found by the silent gaps between them (not by fixed timing), so it doesn't matter
what tempo you recorded at or how much lead-in silence there is. It expects a known count
(default 13: C2->C5 in minor thirds) and refuses to write if it finds a different number,
so you never get mislabeled samples.

  python slice-take.py --wav take.wav --instrument jp8000 --patch sub-bass
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

NOTE_NAMES = ["c", "cs", "d", "ds", "e", "f", "fs", "g", "gs", "a", "as", "b"]


def midi_to_name(midi: int) -> str:
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


def note_range(start_midi: int, end_midi: int, step: int) -> list[int]:
    return list(range(start_midi, end_midi + 1, step))


def find_segments(mono: np.ndarray, sr: int, gate_db: float = -34.0,
                  merge_gap_s: float = 0.30, min_note_s: float = 0.30):
    """Return [(start_sample, end_sample), ...] — one per sounding region.
    gate_db is relative to the recording's peak. Gaps shorter than merge_gap are bridged so
    a note that momentarily dips doesn't split; runs shorter than min_note are dropped (blips)."""
    frame = int(0.010 * sr)                       # 10 ms analysis frames
    n_frames = max(1, len(mono) // frame)
    rms = np.array([
        np.sqrt(np.mean(mono[i * frame:(i + 1) * frame] ** 2) + 1e-12)
        for i in range(n_frames)
    ])
    peak = float(rms.max())
    if peak <= 0:
        return []
    gate = peak * (10.0 ** (gate_db / 20.0))
    sounding = rms > gate

    # bridge short silent gaps inside a note
    merge_frames = int(merge_gap_s / 0.010)
    i = 0
    while i < len(sounding):
        if not sounding[i]:
            j = i
            while j < len(sounding) and not sounding[j]:
                j += 1
            if (j - i) <= merge_frames and i > 0 and j < len(sounding):
                sounding[i:j] = True
            i = j
        else:
            i += 1

    # collect runs of sounding frames
    segs = []
    i = 0
    min_frames = int(min_note_s / 0.010)
    while i < len(sounding):
        if sounding[i]:
            j = i
            while j < len(sounding) and sounding[j]:
                j += 1
            if (j - i) >= min_frames:
                segs.append((i * frame, min(len(mono), j * frame)))
            i = j
        else:
            i += 1
    return segs


def time_segments(mono: np.ndarray, sr: int, expected: int, gate_db: float = -40.0):
    """Fallback for SUSTAINED sounds (pads, strings — anything with a long release) whose tails
    bleed across the gaps so silence-splitting can't separate the notes (it sees one big region).
    We KNOW the capture fired `expected` evenly-spaced notes, so divide the sounding span into
    `expected` equal windows — time-based, works no matter how much the notes ring together."""
    a = np.abs(mono)
    peak = float(a.max()) if a.size else 0.0
    if peak <= 0:
        return []
    idx = np.where(a > peak * (10.0 ** (gate_db / 20.0)))[0]
    if len(idx) == 0:
        return []
    first, last = int(idx[0]), int(idx[-1])
    win = (last - first) / float(expected)
    return [(int(first + i * win), int(first + (i + 1) * win)) for i in range(expected)]


def _zero_cross(mono: np.ndarray, idx: int, search: int) -> int:
    """Nearest zero crossing to idx within `search` samples — cut there so the trim won't click."""
    lo = max(1, idx - search)
    hi = min(len(mono) - 1, idx + search)
    best, bestd = idx, search + 1
    for j in range(lo, hi):
        if (mono[j - 1] <= 0 < mono[j]) or (mono[j - 1] >= 0 > mono[j]):
            d = abs(j - idx)
            if d < bestd:
                bestd, best = d, j
    return best


def trim_silence(clip: np.ndarray, sr: int, gate_db: float = -42.0,
                 pre_ms: float = 3.0, tail_ms: float = 15.0) -> np.ndarray:
    """Delete leading + trailing dead air so silence never reaches the sampler (Nick's rule).
    Keeps a few ms before the first sound (zero-cross aligned) so the transient isn't clipped,
    and a short tail after the last sound so the decay isn't chopped hard."""
    mono = clip.mean(axis=1) if clip.ndim > 1 else clip
    a = np.abs(mono)
    peak = float(a.max()) if a.size else 0.0
    if peak <= 0:
        return clip
    thr = peak * (10.0 ** (gate_db / 20.0))
    idx = np.where(a > thr)[0]
    if len(idx) == 0:
        return clip
    first, last = int(idx[0]), int(idx[-1])
    zc = int(0.005 * sr)
    start = _zero_cross(mono, max(0, first - int(pre_ms / 1000 * sr)), zc)
    end = _zero_cross(mono, min(len(mono), last + int(tail_ms / 1000 * sr)), zc)
    if end <= start:
        return clip
    return clip[start:end]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wav", required=True, help="the one-pass recording (all notes)")
    ap.add_argument("--instrument", required=True, help="instrument slug, e.g. jp8000")
    ap.add_argument("--patch", required=True, help="patch slug, e.g. sub-bass")
    ap.add_argument("--chain", default="raw")
    ap.add_argument("--bank-root", default=None)
    ap.add_argument("--start-note", default="C2", help="lowest note (default C2)")
    ap.add_argument("--end-note", default="C5", help="highest note (default C5)")
    ap.add_argument("--step", type=int, default=3, help="semitones between notes (default 3)")
    ap.add_argument("--velocity", type=int, default=100)
    ap.add_argument("--rr", type=int, default=1)
    ap.add_argument("--gate-db", type=float, default=-34.0, help="silence gate vs peak")
    ap.add_argument("--pre-ms", type=float, default=20.0, help="keep this much before each onset")
    ap.add_argument("--tail-ms", type=float, default=400.0, help="keep this much release tail after a note")
    ap.add_argument("--dry-run", action="store_true", help="report the split, don't write")
    args = ap.parse_args()

    # note name -> midi (C4 = 60)
    def name_to_midi(name: str) -> int:
        name = name.strip().lower()
        letter = name[0]
        idx = 1
        semis = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}[letter]
        if idx < len(name) and name[idx] in ("#", "s"):
            semis += 1; idx += 1
        octave = int(name[idx:])
        return semis + (octave + 1) * 12

    notes = note_range(name_to_midi(args.start_note), name_to_midi(args.end_note), args.step)
    expected = len(notes)

    wav = Path(args.wav).expanduser()
    if not wav.exists():
        print(f"✗ not found: {wav}"); sys.exit(1)
    audio, sr = sf.read(str(wav))
    mono = audio.mean(axis=1) if audio.ndim > 1 else audio

    segs = find_segments(mono, sr, gate_db=args.gate_db)
    print(f"recording: {wav.name}  ({len(mono)/sr:.1f}s @ {sr}Hz)")
    print(f"expected {expected} notes ({args.start_note}->{args.end_note} step {args.step}); found {len(segs)} sounding regions")
    for k, (s, e) in enumerate(segs):
        nm = midi_to_name(notes[k]) if k < expected else "?"
        print(f"  {k+1:2d}. {s/sr:6.2f}s -> {e/sr:6.2f}s  ({(e-s)/sr:.2f}s)  -> {nm}")

    if len(segs) != expected:
        # Sustained sounds (pads, strings) bleed across the gaps → silence-splitting sees one big
        # region. We KNOW the capture fired `expected` evenly-spaced notes, so slice by time instead.
        print(f"\n  silence-split found {len(segs)} (need {expected}) — sustained sound; slicing by time.")
        segs = time_segments(mono, sr, expected, gate_db=args.gate_db)
        for k, (s, e) in enumerate(segs):
            nm = midi_to_name(notes[k]) if k < expected else "?"
            print(f"  {k+1:2d}. {s/sr:6.2f}s -> {e/sr:6.2f}s  ({(e-s)/sr:.2f}s)  -> {nm}")
    if len(segs) != expected:
        print(f"\n✗ couldn't split into {expected} (got {len(segs)}). Check the recording. Nothing written.")
        sys.exit(2)

    if args.dry_run:
        print("\n[dry-run] split looks right — re-run without --dry-run to write.")
        return

    root = Path(args.bank_root) if args.bank_root else Path(__file__).resolve().parents[2]
    out_dir = root / "instruments" / args.instrument / "patches" / args.patch / args.chain
    out_dir.mkdir(parents=True, exist_ok=True)
    pre = int(args.pre_ms / 1000 * sr)
    tail = int(args.tail_ms / 1000 * sr)
    subtype = sf.info(str(wav)).subtype

    written = 0
    for k, (s, e) in enumerate(segs):
        start = max(0, s - pre)
        # end = note end + tail, but never run into the next note
        end = min(len(mono), e + tail)
        if k + 1 < len(segs):
            end = min(end, segs[k + 1][0] - int(0.05 * sr))
        clip = audio[start:end]
        clip = trim_silence(clip, sr)   # delete front + back dead air — silence never ships
        nm = midi_to_name(notes[k])
        fname = f"{args.instrument}_{args.patch}_{nm}_v{args.velocity}_rr{args.rr}.wav"
        sf.write(str(out_dir / fname), clip, sr, subtype=subtype)
        written += 1
        print(f"  wrote {fname}  ({clip.shape[0]/sr:.2f}s)")

    print(f"\n✓ {written} notes -> {out_dir}")
    print(f"  next: run BUILD PACK for {args.instrument} and the loops/pack rebuild from these.")


if __name__ == "__main__":
    main()
