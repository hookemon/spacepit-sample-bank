#!/usr/bin/env python3
"""Generate a chord progression from a genre + key.

Uses roman-numeral templates per genre, picks one (with optional seed), converts to
actual chord names in the chosen key. Outputs JSON.

Usage:
  generate-progression.py --genre house --key Am
  generate-progression.py --genre dilla --key Cm --bars 8
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys


# Roman numeral degree → semitone offset from tonic, with chord quality
# Capital = major triad, lowercase = minor, ° = diminished, ø = half-diminished
# Suffix: 7 = dominant7, m7 = minor7, maj7 = major7
# Format: (semitones_from_tonic, default_quality)

# For minor key (natural minor) — degrees by Roman numeral
# Also includes commonly-borrowed major-mode degrees (IV uppercase = borrowed major, etc.)
MINOR_DEGREES = {
    "i":     (0, "m"),
    "i7":    (0, "m7"),
    "I":     (0, ""),       # picardy / borrowed major tonic
    "Imaj7": (0, "maj7"),
    "bII":   (1, ""),
    "ii":    (2, "dim"),
    "ii7":   (2, "m7b5"),
    "II":    (2, ""),       # borrowed major II
    "bIII":  (3, ""),
    "III":   (3, ""),       # major
    "III7":  (3, "maj7"),
    "IIImaj7":(3, "maj7"),
    "iv":    (5, "m"),
    "iv7":   (5, "m7"),
    "IV":    (5, ""),       # borrowed major IV
    "IVmaj7":(5, "maj7"),
    "IV7":   (5, "7"),
    "v":     (7, "m"),
    "v7":    (7, "m7"),
    "V":     (7, ""),       # harmonic minor V — major
    "V7":    (7, "7"),
    "VI":    (8, ""),       # natural-minor 6 = major
    "VI7":   (8, "maj7"),
    "VImaj7":(8, "maj7"),
    "bVI":   (8, ""),
    "vi":    (9, "m"),      # borrowed minor vi (from parallel major)
    "vi7":   (9, "m7"),
    "VII":   (10, ""),
    "VII7":  (10, "7"),
    "bVII":  (10, ""),
    "bVII7": (10, "7"),
    "vii":   (11, "dim"),
}

# Major key degrees
MAJOR_DEGREES = {
    "I":      (0, ""),
    "Imaj7":  (0, "maj7"),
    "ii":     (2, "m"),
    "ii7":    (2, "m7"),
    "iii":    (4, "m"),
    "iii7":   (4, "m7"),
    "IV":     (5, ""),
    "IVmaj7": (5, "maj7"),
    "V":      (7, ""),
    "V7":     (7, "7"),
    "vi":     (9, "m"),
    "vi7":    (9, "m7"),
    "vii":    (11, "dim"),
    "vii7":   (11, "m7b5"),
}

# Genre templates (Roman numeral progressions). For each genre, a list of templates.
# Templates use space-separated Roman numerals. The mode is either "minor" or "major"
# depending on what the genre typically lives in.
#
# RULE: every template must be 4 chords (or 2 chords that double to 4 bars at bpc=2,
# or 8 chords at bpc=1). Loops only feel right on power-of-2 bar counts to the click.
# No 3-chord, 5-chord, 6-chord, or 7-chord templates. Ever.
GENRE_TEMPLATES = {
    "house": {
        "mode": "minor",
        "templates": [
            "VI IV i V",         # vi-IV-I-V (in relative major numbers; here i is minor home)
            "i VII VI V",
            "i iv VII III",
            "i VI III VII",
            "i v VI III",
        ],
    },
    "house-major": {
        "mode": "major",
        "templates": [
            "I V vi IV",
            "vi IV I V",
            "I vi IV V",
            "Imaj7 vi7 IV V",
        ],
    },
    "hip-hop": {
        "mode": "minor",
        "templates": [
            "i iv i V",
            "i VI iv V",
            "i VI III VII",
            "i7 iv7 i7 V7",
            "i VII VI V",
            "i bVI bVII V",
        ],
    },
    "neo-soul": {
        "mode": "minor",
        "templates": [
            "i7 iv7 bVII7 III",
            "i7 v7 iv7 V7",
            "i7 iv7 i7 V7",
            "i7 VII7 VI7 V7",
        ],
    },
    "dub": {
        "mode": "minor",
        "templates": [
            "i v",
            "i iv",
            "i VII i VII",
            "i VI iv VII",
        ],
    },
    "soul": {
        "mode": "major",
        "templates": [
            "I V vi IV",
            "I vi IV V",
            "vi IV I V",
            "Imaj7 vi7 ii7 V7",
        ],
    },
    "jazz": {
        "mode": "minor",
        "templates": [
            "ii7 V7 i7 vi7",          # ii-V-i-vi in minor (canonical turnaround)
            "ii7 V7 III7 VI7",        # circle-of-fifths walk (was the bad 3-chord "ii7 V7 III7")
            "i7 vi7 ii7 V7",          # extended minor turnaround
            "iv7 V7 i7 vi7",          # subdominant entry
        ],
    },
    "trap": {
        "mode": "minor",
        "templates": [
            "i V VI III",
            "i VI III VII",
            "i bVI bVII V",
            "i v iv III",
            "i bIII bVI V",
        ],
    },
    "lofi": {
        "mode": "major",
        "templates": [
            "Imaj7 iii7 vi7 IVmaj7",
            "Imaj7 vi7 IVmaj7 V7",
            "ii7 V7 Imaj7 vi7",
            "iii7 vi7 ii7 V7",
        ],
    },
    "rnb": {
        "mode": "minor",
        "templates": [
            "i7 III bVII bVI",
            "i7 v7 iv7 V7",
            "i VII VI V",
            "i7 IV iv7 V7",
        ],
    },
    "disco": {
        "mode": "minor",
        "templates": [
            "i7 IV i7 V",
            "i7 v7 iv7 V7",
            "ii7 V7 i7 vi7",
        ],
    },
    "dnb": {
        "mode": "minor",
        "templates": [
            "i VI III VII",
            "i V VI iv",
            "i iv V iv",
        ],
    },
}


# Note name → semitone offset
NOTE_SEMITONES = {
    "c": 0, "c#": 1, "db": 1, "d": 2, "d#": 3, "eb": 3,
    "e": 4, "f": 5, "f#": 6, "gb": 6, "g": 7, "g#": 8, "ab": 8,
    "a": 9, "a#": 10, "bb": 10, "b": 11,
}


def parse_key(key: str) -> tuple[str, str]:
    """Returns (note_letter_form, mode) — e.g. 'Cm' → ('C', 'minor'), 'Cmaj' → ('C', 'major')."""
    m = re.match(r"^([A-Ga-g])([#b]?)\s*(.*)$", key.strip())
    if not m:
        raise ValueError(f"bad key: {key}")
    letter, acc, suffix = m.groups()
    note = letter.upper() + acc.lower()
    suffix = suffix.lower().strip()
    mode = "minor" if (suffix in ("m", "min", "minor", "")) else "major"
    if suffix in ("maj", "major"): mode = "major"
    return note, mode


def degree_to_chord(degree: str, key_root: str, mode: str) -> str:
    """Convert a roman numeral degree (e.g. 'iv7') to a chord name (e.g. 'Fm7')."""
    degrees = MINOR_DEGREES if mode == "minor" else MAJOR_DEGREES
    if degree not in degrees:
        return degree  # passthrough if we don't recognize it
    semitones, quality = degrees[degree]
    root_st = NOTE_SEMITONES.get(key_root.lower(), 0)
    target_st = (root_st + semitones) % 12
    # find a note name for target_st — prefer flat or sharp based on key
    SHARP_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    FLAT_NAMES  = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
    use_flats = key_root.endswith("b") or key_root.lower() in ("f", "bb", "eb", "ab", "db", "gb")
    names = FLAT_NAMES if use_flats else SHARP_NAMES
    return names[target_st] + quality


def generate(genre: str, key: str, seed: int | None = None) -> dict:
    if seed is not None:
        random.seed(seed)
    if genre not in GENRE_TEMPLATES:
        raise ValueError(f"unknown genre '{genre}'. known: {sorted(GENRE_TEMPLATES.keys())}")
    g = GENRE_TEMPLATES[genre]
    template = random.choice(g["templates"])
    mode_override = g.get("mode")
    key_root, _ = parse_key(key)
    mode = mode_override if mode_override else parse_key(key)[1]
    degrees = template.split()
    chords = [degree_to_chord(d, key_root, mode) for d in degrees]
    return {
        "genre": genre,
        "key": key,
        "mode": mode,
        "template": template,
        "chords": chords,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--genre", required=True, help="genre name (e.g. house, hip-hop, dub, soul, jazz, trap, lofi, rnb, disco, dnb, neo-soul)")
    ap.add_argument("--key", required=True, help="key (e.g. Cm, Am, Em, Cmaj)")
    ap.add_argument("--seed", type=int, default=None, help="random seed (reproducible)")
    ap.add_argument("--count", type=int, default=1, help="generate N variations")
    args = ap.parse_args()

    out = []
    for i in range(args.count):
        seed_i = args.seed + i if args.seed is not None else None
        out.append(generate(args.genre, args.key, seed_i))

    if args.count == 1:
        print(json.dumps(out[0], indent=2))
    else:
        print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
