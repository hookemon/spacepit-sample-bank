#!/usr/bin/env python3
"""Generate test MIDI files for multisampling a hardware synth.

Each generated .mid file plays a sequence of notes with consistent duration +
gap between them, designed to be:
  1. Dropped into Ableton on a MIDI track routed to your synth
  2. Audio recorded on another track receiving the synth's output
  3. The recorded WAV chopped + assigned to Sampler with roots + key zones

Output: drops .mid files into tools/capture/midi/.

Usage:
    python make-test-midi.py                      # all preset modes
    python make-test-midi.py --mode minimal       # just the 4-note Ableton workflow
    python make-test-midi.py --mode chromatic     # every note across the range
    python make-test-midi.py --range a2-a5 --step 12 --sustain 4 --gap 1
"""
import argparse
import os
from pathlib import Path

import mido


NOTE_NAMES = ["c", "c#", "d", "d#", "e", "f", "f#", "g", "g#", "a", "a#", "b"]


def parse_note(name: str) -> int:
    """Convert 'a3' or 'c#4' to MIDI note number (a3 = 57, c4 = 60)."""
    name = name.strip().lower()
    if "#" in name:
        letter, octave = name[:2], int(name[2:])
    else:
        letter, octave = name[:1], int(name[1:])
    base = NOTE_NAMES.index(letter)
    return (octave + 1) * 12 + base


def note_to_name(midi: int) -> str:
    letter = NOTE_NAMES[midi % 12]
    octave = (midi // 12) - 1
    return f"{letter}{octave}"


def make_midi(notes: list[int], sustain_sec: float, gap_sec: float, bpm: float,
              velocity: int, name_hint: str, out_path: Path) -> None:
    """Write a single MIDI file playing the given list of notes in sequence."""
    mid = mido.MidiFile()
    track = mido.MidiTrack()
    mid.tracks.append(track)

    # ticks per quarter: 480 (standard high-res)
    mid.ticks_per_beat = 480
    sec_per_beat = 60.0 / bpm
    ticks_per_sec = mid.ticks_per_beat / sec_per_beat

    sustain_ticks = int(sustain_sec * ticks_per_sec)
    gap_ticks = int(gap_sec * ticks_per_sec)

    # NO tempo, NO time signature, NO track name — pure notes only.
    # Ableton uses session tempo. The note timing is in MIDI ticks; at the default
    # 120 BPM that MIDI players assume, our ticks_per_beat=480 gives the intended
    # 4-sec-sustain / 1-sec-gap pattern regardless of host tempo (since Ableton's
    # MIDI import preserves absolute durations).

    delta = 0  # ticks since last event
    for n in notes:
        track.append(mido.Message('note_on', note=n, velocity=velocity, time=delta))
        delta = sustain_ticks
        track.append(mido.Message('note_off', note=n, velocity=0, time=delta))
        delta = gap_ticks

    out_path.parent.mkdir(parents=True, exist_ok=True)
    mid.save(str(out_path))
    total_sec = len(notes) * (sustain_sec + gap_sec)
    print(f"  ✓ {out_path.name}  ·  {len(notes)} notes  ·  "
          f"{total_sec:.1f}s total ({sustain_sec}s sustain + {gap_sec}s gap)")
    print(f"     notes: {' '.join(note_to_name(n) for n in notes)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="all",
                    choices=["all", "minimal", "chromatic", "fifths", "octaves"],
                    help="which preset to generate (default: all)")
    ap.add_argument("--range", default="c2-c5",
                    help="note range. Default c2-c5 because Ableton Sampler's default root "
                         "is C3 — C-rooted multisamples drop in clean without remapping. "
                         "Override if you need other ranges (e.g. 'a1-a4' for deep bass).")
    ap.add_argument("--sustain", type=float, default=4.0, help="seconds per note")
    ap.add_argument("--gap", type=float, default=1.0, help="seconds of silence between notes")
    ap.add_argument("--bpm", type=float, default=120.0, help="MIDI file tempo")
    ap.add_argument("--velocity", type=int, default=100, help="MIDI velocity (0-127)")
    ap.add_argument("--out-dir", default=None,
                    help="output directory (default: tools/capture/midi/)")
    args = ap.parse_args()

    # parse range
    low_str, high_str = args.range.split("-")
    low, high = parse_note(low_str), parse_note(high_str)

    # default out dir
    if args.out_dir:
        out_dir = Path(args.out_dir)
    else:
        out_dir = Path(__file__).resolve().parent / "midi"

    print(f"Test MIDI generator")
    print(f"  range:    {args.range}  (MIDI {low}-{high}, {high - low + 1} semitones)")
    print(f"  sustain:  {args.sustain}s per note")
    print(f"  gap:      {args.gap}s between notes")
    print(f"  bpm:      {args.bpm}")
    print(f"  velocity: {args.velocity}")
    print(f"  out dir:  {out_dir}")
    print()

    # Build note lists for each mode
    modes_to_run = ["minimal", "octaves", "fifths", "chromatic"] if args.mode == "all" else [args.mode]

    for mode in modes_to_run:
        if mode == "minimal":
            # One note per octave starting at the low end
            notes = []
            n = low
            while n <= high:
                notes.append(n)
                n += 12
            # ensure highest octave is included
            if notes[-1] != high and (high - notes[-1]) >= 6:
                notes.append(high)
            name = "test-minimal-octaves"
            hint = "minimal: 1 root per octave — for Ableton 'Distribute Ranges Around Root Key'"
        elif mode == "octaves":
            # Same as minimal but explicit naming
            notes = list(range(low, high + 1, 12))
            if notes[-1] != high:
                notes.append(high)
            name = "test-octaves"
            hint = "octaves only — Ableton Sampler will pitch-shift in between"
        elif mode == "fifths":
            # Every 7 semitones — good middle ground (5 samples in 3 octaves)
            notes = list(range(low, high + 1, 7))
            if notes[-1] != high:
                notes.append(high)
            name = "test-fifths"
            hint = "every 5th — tighter mapping with less pitch-stretch artifact"
        elif mode == "chromatic":
            notes = list(range(low, high + 1))
            name = "test-chromatic-full"
            hint = "every semitone — exhaustive capture, no pitch-stretching needed"
        else:
            continue

        out_file = out_dir / f"{name}_{args.range}.mid"
        make_midi(notes, args.sustain, args.gap, args.bpm, args.velocity, hint, out_file)
        print()

    print("→ drop the .mid file onto a MIDI track in Ableton, route it to the synth,")
    print("  and record the audio on a separate track. Then drag the resulting WAV")
    print("  into Sampler and set roots + key zones.")


if __name__ == "__main__":
    main()
