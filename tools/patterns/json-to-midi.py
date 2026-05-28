#!/usr/bin/env python3
"""Convert JSON pattern files into Ableton-droppable .mid files.

Reads patterns/<category>/<name>.json and writes patterns/midi/<category>/<name>.mid
with the rules we've locked in for Ableton imports:
  - No set_tempo metadata (Ableton uses session tempo)
  - No track_name (avoids warp interpretation)
  - No time_signature (clean)
  - Just note_on / note_off events
  - 480 ticks_per_beat (high resolution)

Supports two pattern shapes:
  1. DRUM/MELODIC track style:
     tracks: { kick: [0, 36, 72], snare: [24, 72] }
     — each entry is a list of tick positions; note pitch comes from GM_DRUM_MAP
       or an explicit pitch override per track.

  2. EVENT list style (richer — for bass, melody, arp):
     events: [{"tick": 0, "note": "C2", "vel": 100, "len": 22}, ...]
     — explicit note + velocity + duration per event.

Usage:
    python json-to-midi.py                 # convert ALL JSON in patterns/
    python json-to-midi.py --category bass # only bass/
    python json-to-midi.py --file patterns/bass/house-pump.json  # one file
"""
import argparse
import json
import re
import sys
from pathlib import Path

import mido


# General MIDI drum map (standard channel 10 keys)
GM_DRUM_MAP = {
    "kick": 36, "bd": 36, "kick2": 35,
    "snare": 38, "sd": 38, "snare-rim": 37, "rim": 37, "snare-clap": 39, "clap": 39,
    "hh-closed": 42, "hat": 42, "ch": 42, "hh": 42,
    "hh-pedal": 44, "ph": 44,
    "hh-open": 46, "oh": 46,
    "crash": 49, "crash2": 57,
    "ride": 51, "ride-bell": 53,
    "tom-low": 41, "tom-mid": 47, "tom-hi": 50,
    "perc": 54, "tambourine": 54, "cowbell": 56,
    "shaker": 70, "maracas": 70,
    "conga-low": 64, "conga-hi": 63, "bongo-low": 61, "bongo-hi": 60,
}

NOTE_NAMES = ["c", "c#", "d", "d#", "e", "f", "f#", "g", "g#", "a", "a#", "b"]
SHARP_ALIASES = {"cs": "c#", "ds": "d#", "fs": "f#", "gs": "g#", "as": "a#"}


CHORD_INTERVALS = {
    "":      [0, 4, 7],        # major triad
    "maj":   [0, 4, 7],
    "M":     [0, 4, 7],
    "maj7":  [0, 4, 7, 11],
    "M7":    [0, 4, 7, 11],
    "maj9":  [0, 4, 7, 11, 14],
    "m":     [0, 3, 7],        # minor triad
    "min":   [0, 3, 7],
    "m7":    [0, 3, 7, 10],
    "min7":  [0, 3, 7, 10],
    "m9":    [0, 3, 7, 10, 14],
    "m7b5":  [0, 3, 6, 10],
    "7":     [0, 4, 7, 10],    # dom7
    "dim":   [0, 3, 6],
    "dim7":  [0, 3, 6, 9],
    "aug":   [0, 4, 8],
    "sus2":  [0, 2, 7],
    "sus4":  [0, 5, 7],
}


def chord_to_midi_notes(name: str, octave: int = 3) -> list:
    """Convert 'Cm' / 'Eb7' / 'Gmaj7' to a list of MIDI notes. Root in given octave."""
    m = re.match(r"^([A-Ga-g])([#b]?)\s*(.*)$", name.strip())
    if not m:
        return []
    letter, acc, suffix = m.groups()
    base = NOTE_NAMES.index(letter.lower())
    if acc == "#":
        base += 1
    elif acc == "b":
        base -= 1
    base = base % 12
    intervals = CHORD_INTERVALS.get(suffix.strip(), [0, 4, 7])  # fallback major triad
    root_midi = base + (octave + 1) * 12
    return [root_midi + i for i in intervals]


def note_to_midi(name: str) -> int:
    """Convert 'a3' or 'c#4' to MIDI number. Returns int."""
    if isinstance(name, int):
        return name
    name = name.strip().lower()
    for k, v in SHARP_ALIASES.items():
        if name.startswith(k):
            name = v + name[len(k):]
            break
    if "#" in name:
        letter, oct_str = name[:2], name[2:]
    else:
        letter, oct_str = name[:1], name[1:]
    if not oct_str.lstrip("-").isdigit():
        raise ValueError(f"bad note name: {name}")
    return (int(oct_str) + 1) * 12 + NOTE_NAMES.index(letter)


def convert_pattern(pattern: dict, src_ppqn: int = 24, out_ppqn: int = 480) -> mido.MidiFile:
    """Convert a pattern dict to a MidiFile. Scales ticks from src_ppqn to out_ppqn."""
    mid = mido.MidiFile()
    mid.ticks_per_beat = out_ppqn
    track = mido.MidiTrack()
    mid.tracks.append(track)

    scale = out_ppqn / src_ppqn

    events = []  # list of (abs_tick, type, note, velocity)
    if "events" in pattern:
        # Explicit event list
        for ev in pattern["events"]:
            tick = int(ev.get("tick", 0) * scale)
            note = note_to_midi(ev["note"])
            vel = ev.get("vel", ev.get("velocity", 100))
            length = int(ev.get("len", ev.get("length", src_ppqn // 2)) * scale)
            events.append((tick, "on", note, vel))
            events.append((tick + length, "off", note, 0))
    elif "tracks" in pattern:
        # Drum-style tracks dict. Pitch comes from GM map or an explicit "pitch" field.
        # If track has key like "kick: [...]" → use GM_DRUM_MAP["kick"]
        # Override: tracks can also be {note_pitch_override: [ticks]} via "pitch_overrides"
        pitch_overrides = pattern.get("pitch_overrides", {})
        default_len = int(pattern.get("note_length", src_ppqn // 4) * scale)
        for track_name, ticks in pattern["tracks"].items():
            if track_name in pitch_overrides:
                pitch = pitch_overrides[track_name]
                if isinstance(pitch, str):
                    pitch = note_to_midi(pitch)
            elif track_name in GM_DRUM_MAP:
                pitch = GM_DRUM_MAP[track_name]
            else:
                # try to parse track_name as a note name
                try:
                    pitch = note_to_midi(track_name)
                except Exception:
                    print(f"  ⚠ unknown track '{track_name}' — defaulting to MIDI 60")
                    pitch = 60
            for t in ticks:
                tick = int(t * scale)
                vel = 100
                events.append((tick, "on", pitch, vel))
                events.append((tick + default_len, "off", pitch, 0))
    elif "chords" in pattern or "progression" in pattern:
        # Chord-progression pattern — spell each chord as a block, hold for bars_per_chord
        chords = pattern.get("chords") or pattern.get("progression")
        if isinstance(chords, str):
            chords = chords.split()
        bars_per_chord = pattern.get("bars_per_chord", 2)
        # Each bar = 4 beats = 4 * src_ppqn ticks. Output scale already applied.
        ticks_per_bar = 4 * src_ppqn
        chord_dur_ticks = int(ticks_per_bar * bars_per_chord * scale)
        # Parse each chord into MIDI notes (root + 3 + 5 + optional 7)
        for i, chord_name in enumerate(chords):
            notes = chord_to_midi_notes(chord_name, octave=3)
            tick_start = i * chord_dur_ticks
            for n in notes:
                events.append((tick_start, "on", n, 90))
                events.append((tick_start + chord_dur_ticks - 1, "off", n, 0))
    else:
        raise ValueError(f"pattern {pattern.get('name','?')} has neither 'events' nor 'tracks' nor 'chords'")

    # Sort by tick, "off" before "on" at same tick to avoid stuck notes on repeats
    events.sort(key=lambda e: (e[0], 0 if e[1] == "off" else 1))

    delta = 0
    last_tick = 0
    for tick, kind, note, vel in events:
        msg_time = tick - last_tick
        last_tick = tick
        if kind == "on":
            track.append(mido.Message("note_on", note=note, velocity=vel, time=msg_time))
        else:
            track.append(mido.Message("note_off", note=note, velocity=0, time=msg_time))

    return mid


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", default=None,
                    help="only process this category (drums, bass, arps, melodies, etc.)")
    ap.add_argument("--file", default=None, help="convert just one JSON file")
    ap.add_argument("--patterns-dir", default=None,
                    help="path to patterns/ (default: auto-detect from script location)")
    ap.add_argument("--out-dir", default=None,
                    help="output dir for .mid files (default: patterns/midi/<category>/)")
    args = ap.parse_args()

    if args.patterns_dir:
        patterns_dir = Path(args.patterns_dir).resolve()
    else:
        # tools/patterns/X.py → repo/patterns/
        patterns_dir = Path(__file__).resolve().parent.parent.parent / "patterns"

    if not patterns_dir.exists():
        print(f"patterns dir missing: {patterns_dir}", file=sys.stderr); sys.exit(1)

    # Build the file list
    files = []
    if args.file:
        files = [Path(args.file)]
    else:
        # Walk categories
        categories_to_scan = [args.category] if args.category else None
        for cat_dir in patterns_dir.iterdir():
            if not cat_dir.is_dir():
                continue
            if cat_dir.name in ("midi", "tributes"):
                continue  # don't recurse into output; tributes are patch refs, not notes
            if categories_to_scan and cat_dir.name not in categories_to_scan:
                continue
            files.extend(sorted(cat_dir.glob("*.json")))

    if not files:
        print("no JSON pattern files found"); sys.exit(0)

    print(f"converting {len(files)} pattern(s)…\n")
    n_ok, n_fail = 0, 0
    for f in files:
        try:
            pattern = json.loads(f.read_text())
            category = f.parent.name
            out_dir = Path(args.out_dir) if args.out_dir else patterns_dir / "midi" / category
            out_dir.mkdir(parents=True, exist_ok=True)
            ppqn = pattern.get("ppqn", 24)
            mid = convert_pattern(pattern, src_ppqn=ppqn)
            out_path = out_dir / f"{f.stem}.mid"
            mid.save(str(out_path))
            n_ok += 1
            note_count = sum(1 for tr in mid.tracks[0] if tr.type == "note_on" and tr.velocity > 0)
            duration_beats = pattern.get("bars", 1) * 4
            print(f"  ✓ {category}/{f.stem}.mid  ·  {note_count} notes · {duration_beats} beats")
        except Exception as e:
            n_fail += 1
            print(f"  ✗ {f.name}: {e}")

    print(f"\n{n_ok} written, {n_fail} failed")
    if n_ok:
        print(f"→ files in {patterns_dir}/midi/<category>/")


if __name__ == "__main__":
    main()
