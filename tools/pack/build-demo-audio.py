#!/usr/bin/env python3
"""Build a 'pack tour' demo audio from selected loops in the pack.

Concatenates 4-6 loops back-to-back with crossfades. The output is the demo audio
that plays on the landing page + Gumroad listing — a 30-60 sec preview of the pack.

Usage:
  build-demo-audio.py --pack releases/spacepit-grandmother-vol1
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import soundfile as sf


# Curated tour — 5 loops that showcase the pack's range
TOUR = [
    # (filename in audio/loops/, label, duration_to_use_sec)
    ("house-classic_124bpm_4bars.wav",     "house-classic",     7.5),
    ("hiphop-soul_90bpm_8bars.wav",        "hiphop-soul",       11.0),
    ("dub-am-em_75bpm_8bars.wav",          "dub-am-em",         12.0),
    ("trap-140_sixteenth_140bpm_4bars.wav","trap-140 hi-hat",    5.5),
    ("future-rnb_80bpm_8bars.wav",         "future-rnb",        12.0),
]

CROSSFADE_SEC = 0.5  # crossfade between adjacent loops


def fade_in(audio: np.ndarray, samples: int) -> np.ndarray:
    """Apply a linear fade-in to the first `samples` samples."""
    n = min(samples, len(audio))
    if n == 0:
        return audio
    ramp = np.linspace(0, 1, n, endpoint=False)
    out = audio.copy()
    if out.ndim == 1:
        out[:n] *= ramp
    else:
        out[:n] *= ramp[:, None]
    return out


def fade_out(audio: np.ndarray, samples: int) -> np.ndarray:
    """Apply a linear fade-out to the last `samples` samples."""
    n = min(samples, len(audio))
    if n == 0:
        return audio
    ramp = np.linspace(1, 0, n, endpoint=False)
    out = audio.copy()
    if out.ndim == 1:
        out[-n:] *= ramp
    else:
        out[-n:] *= ramp[:, None]
    return out


def truncate_or_pad(audio: np.ndarray, target_samples: int) -> np.ndarray:
    """Truncate or zero-pad to exactly target_samples."""
    if len(audio) > target_samples:
        return audio[:target_samples]
    pad = target_samples - len(audio)
    if audio.ndim == 1:
        return np.concatenate([audio, np.zeros(pad)])
    return np.concatenate([audio, np.zeros((pad, audio.shape[1]))])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", required=True, help="path to the pack folder")
    ap.add_argument("--output", default=None, help="output WAV path (default: <pack>/demo.wav)")
    ap.add_argument("--crossfade-sec", type=float, default=CROSSFADE_SEC)
    args = ap.parse_args()

    pack_dir = Path(args.pack).resolve()
    if not pack_dir.exists():
        print(f"pack not found: {pack_dir}", file=sys.stderr); sys.exit(1)

    loops_dir = pack_dir / "audio" / "loops"
    if not loops_dir.exists():
        print(f"loops dir not found: {loops_dir}", file=sys.stderr); sys.exit(1)

    output_path = Path(args.output).resolve() if args.output else pack_dir / "demo.wav"

    print(f"=== building demo audio ===")
    print(f"  pack:    {pack_dir}")
    print(f"  loops:   {loops_dir}")
    print(f"  output:  {output_path}\n")

    # Load each tour loop, trim to its duration, capture sample rate
    sr = None
    segments = []
    for fname, label, target_dur in TOUR:
        path = loops_dir / fname
        if not path.exists():
            print(f"  ⚠ skip (not found): {fname}")
            continue
        audio, this_sr = sf.read(str(path))
        if sr is None:
            sr = this_sr
        elif this_sr != sr:
            print(f"  ⚠ skip ({fname}): sample rate {this_sr} != {sr}")
            continue
        target_samples = int(target_dur * sr)
        audio = truncate_or_pad(audio, target_samples)
        segments.append((label, audio))
        print(f"  ✓ {label:<20}  {target_dur:.1f}s  ({fname})")

    if not segments:
        print("no segments to combine", file=sys.stderr); sys.exit(1)

    # Build combined audio with crossfades
    crossfade_samples = int(args.crossfade_sec * sr)
    total_samples = sum(len(s[1]) for s in segments) - crossfade_samples * (len(segments) - 1)

    # Pick output channels: use stereo (2-channel) regardless
    out = np.zeros((total_samples, 2))
    pos = 0
    for i, (label, audio) in enumerate(segments):
        # Ensure stereo
        if audio.ndim == 1:
            audio = np.stack([audio, audio], axis=1)
        elif audio.shape[1] == 1:
            audio = np.repeat(audio, 2, axis=1)
        elif audio.shape[1] > 2:
            audio = audio[:, :2]

        # Apply fades for crossfade
        if i > 0:
            audio = fade_in(audio, crossfade_samples)
        if i < len(segments) - 1:
            audio = fade_out(audio, crossfade_samples)

        # Place in output
        end = pos + len(audio)
        out[pos:end] += audio
        pos += len(audio) - crossfade_samples

    # Normalize to a safe peak ~-3 dBFS to avoid clipping
    peak = float(np.max(np.abs(out)))
    if peak > 0:
        target_peak = 0.708  # -3 dBFS
        if peak > target_peak:
            out *= (target_peak / peak)
            scaled_peak = target_peak
        else:
            scaled_peak = peak
    else:
        scaled_peak = 0

    # Save
    sf.write(str(output_path), out, sr, subtype="PCM_24")

    duration = total_samples / sr
    print(f"\n✓ demo built: {output_path}")
    print(f"   duration: {duration:.1f}s")
    print(f"   peak:     {20*np.log10(max(1e-10, scaled_peak)):+.1f} dBFS")
    print(f"   segments: {len(segments)}")


if __name__ == "__main__":
    main()
