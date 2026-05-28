#!/usr/bin/env python3
"""Clean multisample WAVs for shipping: trim dead air at start, gentle fade-out at end, normalize.

Operates IN-PLACE on a directory tree of WAV files. Writes a backup of the originals to
<dir>_original/ on first run (skipped if backup exists).

Usage:
  clean-wavs.py --dir releases/.../audio/multisamples
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import numpy as np
import soundfile as sf


def find_note_onset(audio: np.ndarray, sample_rate: int, threshold_db: float = -45.0) -> int:
    """Return the sample index where audio first exceeds threshold_db.

    Walks forward in 10ms windows checking RMS — robust to sub-audible noise floor.
    Returns 0 if never exceeded (very quiet file)."""
    block = max(1, int(0.010 * sample_rate))  # 10ms
    threshold = 10 ** (threshold_db / 20)
    for i in range(0, len(audio) - block, block):
        rms = float(np.sqrt(np.mean(audio[i:i+block] ** 2)))
        if rms > threshold:
            # found the block; back up to find precise onset within
            for j in range(i, i + block):
                if abs(audio[j]) > threshold:
                    return j
            return i
    return 0


def find_note_offset(audio: np.ndarray, sample_rate: int, threshold_db: float = -55.0) -> int:
    """Return the sample index where audio last exceeds threshold_db, walking backward."""
    block = max(1, int(0.010 * sample_rate))
    threshold = 10 ** (threshold_db / 20)
    for i in range(len(audio) - block, 0, -block):
        rms = float(np.sqrt(np.mean(audio[i:i+block] ** 2)))
        if rms > threshold:
            return min(len(audio), i + block * 2)  # leave a bit of headroom past
    return len(audio)


def clean_one(path: Path, lead_in_ms: float = 5.0, fade_out_ms: float = 50.0,
              target_peak_db: float = -3.0) -> dict:
    """Trim + fade + normalize one WAV. Returns stats dict.

    - lead_in_ms: ms of silence to keep before note onset (avoids cutting transient)
    - fade_out_ms: linear fade-out applied to last N ms (avoids click at sample end)
    - target_peak_db: per-file peak normalization (skipped if 0 or None)
    """
    audio, sr = sf.read(str(path))
    is_stereo = audio.ndim > 1

    # Use mono mix for onset/offset detection
    mono = audio.mean(axis=1) if is_stereo else audio

    onset = find_note_onset(mono, sr)
    offset = find_note_offset(mono, sr)

    if offset <= onset:
        # silent / weird file — skip
        return {"path": str(path), "skipped": "silent", "len_in": len(audio), "len_out": len(audio)}

    # back off onset by lead-in
    start = max(0, onset - int(lead_in_ms * sr / 1000))
    end = min(len(audio), offset)

    # slice
    if is_stereo:
        trimmed = audio[start:end].copy()
    else:
        trimmed = audio[start:end].copy()

    # fade-out
    fade_samples = min(int(fade_out_ms * sr / 1000), len(trimmed))
    if fade_samples > 0:
        ramp = np.linspace(1.0, 0.0, fade_samples)
        if is_stereo:
            trimmed[-fade_samples:] *= ramp[:, None]
        else:
            trimmed[-fade_samples:] *= ramp

    # normalize per-file to target_peak_db
    if target_peak_db is not None:
        peak = float(np.max(np.abs(trimmed)))
        if peak > 0:
            target_peak_linear = 10 ** (target_peak_db / 20)
            gain = target_peak_linear / peak
            trimmed *= gain

    # save back (24-bit)
    sf.write(str(path), trimmed, sr, subtype="PCM_24")

    return {
        "path": str(path),
        "skipped": None,
        "len_in_sec": len(audio) / sr,
        "len_out_sec": len(trimmed) / sr,
        "trimmed_ms_start": start * 1000 / sr,
        "trimmed_ms_end": (len(audio) - end) * 1000 / sr,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="directory to clean (walks recursively)")
    ap.add_argument("--lead-in-ms", type=float, default=5.0)
    ap.add_argument("--fade-out-ms", type=float, default=50.0)
    ap.add_argument("--target-peak-db", type=float, default=-3.0,
                    help="normalize each file to this peak. Set to 0 to skip normalize.")
    ap.add_argument("--no-normalize", action="store_true")
    ap.add_argument("--backup", action="store_true", help="back up to <dir>_original/ before cleaning")
    args = ap.parse_args()

    src = Path(args.dir).resolve()
    if not src.exists():
        print(f"directory not found: {src}", file=sys.stderr); sys.exit(1)

    if args.backup:
        backup = src.parent / f"{src.name}_original"
        if not backup.exists():
            print(f"backing up to {backup}")
            shutil.copytree(src, backup)
        else:
            print(f"backup already exists at {backup} — skipping backup step")

    target_peak = None if args.no_normalize else args.target_peak_db

    wavs = sorted(src.rglob("*.wav"))
    print(f"processing {len(wavs)} WAVs...")

    total_saved_sec = 0.0
    skipped = 0
    for i, wav in enumerate(wavs):
        result = clean_one(wav, args.lead_in_ms, args.fade_out_ms, target_peak)
        if result.get("skipped"):
            skipped += 1
            continue
        saved = result["len_in_sec"] - result["len_out_sec"]
        total_saved_sec += saved
        rel = wav.relative_to(src)
        if (i + 1) % 25 == 0 or i == len(wavs) - 1:
            print(f"  [{i+1}/{len(wavs)}] {rel}  ({result['len_in_sec']:.2f}s → {result['len_out_sec']:.2f}s, saved {saved*1000:.0f}ms)")

    print(f"\n✓ cleaned {len(wavs) - skipped} files")
    print(f"  total dead air removed: {total_saved_sec:.1f}s")
    if skipped:
        print(f"  ⚠ skipped {skipped} silent files")


if __name__ == "__main__":
    main()
