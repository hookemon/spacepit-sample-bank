#!/usr/bin/env python3
"""Session composer — match patterns across the library given a vibe.

Substrate for the synthesis machine. Given inputs (genre, BPM range, key),
returns a curated trio from the existing pattern library:
  - chord progression
  - bass line
  - drum pattern
Plus a suggested patch from the captured instruments (based on genre tags).

Foundation. The full synthesis machine layers feel/era/level on top of this.

Usage:
    python compose.py --genre house --bpm 124
    python compose.py --genre dub --key Am --bpm 75
    python compose.py --vibe "rainy late night neo-soul, slow"

Output: prints the curated session + optionally writes a combined .mid file
with 3 tracks (drums on ch10, bass on ch2, chords on ch1).
"""
import argparse
import json
import random
import re
import sys
from pathlib import Path
from typing import Optional

import mido


def find_repo_root() -> Path:
    """tools/synthesis/X.py → spacepit-sample-bank/"""
    return Path(__file__).resolve().parent.parent.parent


def load_patterns(repo: Path) -> dict:
    """Walk patterns/<category>/ and load every JSON pattern. Returns by category."""
    out = {"progressions": [], "bass": [], "drums": [], "melodies": [], "arps": []}
    for cat in out.keys():
        cat_dir = repo / "patterns" / cat
        if not cat_dir.exists():
            continue
        for f in sorted(cat_dir.glob("*.json")):
            try:
                p = json.loads(f.read_text())
                p["_file"] = str(f.relative_to(repo))
                p["_midi"] = str((repo / "patterns" / "midi" / cat / f.stem).with_suffix(".mid").relative_to(repo))
                out[cat].append(p)
            except Exception:
                pass
    return out


def load_instruments(repo: Path) -> list:
    """Walk instruments/<slug>/manifest.json and return all instrument manifests."""
    out = []
    instr_dir = repo / "instruments"
    if not instr_dir.exists():
        return out
    for slug_dir in sorted(instr_dir.iterdir()):
        mf = slug_dir / "manifest.json"
        if mf.exists():
            try:
                m = json.loads(mf.read_text())
                m["_slug"] = slug_dir.name
                out.append(m)
            except Exception:
                pass
    return out


def matches_genre(pattern: dict, genre: str) -> bool:
    """A pattern matches a genre if the genre appears in its tags or `genre` field."""
    g = genre.lower()
    tags = [t.lower() for t in pattern.get("tags", [])]
    pgen = (pattern.get("genre") or "").lower()
    return g in tags or g in pgen


def matches_bpm(pattern: dict, bpm_min: float, bpm_max: float) -> bool:
    """A pattern matches if its BPM falls inside the requested range (inclusive)."""
    pbpm = pattern.get("bpm")
    if pbpm is None:
        return True  # no BPM = drum/melody patterns where tempo is fluid
    return bpm_min <= float(pbpm) <= bpm_max


def matches_key(pattern: dict, key_root: Optional[str], mode: Optional[str]) -> bool:
    """A pattern matches the key if root matches AND mode matches (when specified).
    Patterns without key info match anything (so drum patterns always match)."""
    if not (key_root or mode):
        return True
    pkey = (pattern.get("key") or pattern.get("key_root") or "").lower()
    pmode = (pattern.get("key_mode") or "").lower()
    if not pkey:
        return True  # no key info — assume drum/melody, always match
    if key_root:
        kr = key_root.lower().strip()
        # rough match — pattern's key starts with the root letter (+ optional accidental)
        if not pkey.startswith(kr):
            return False
    if mode and pmode and mode.lower() != pmode:
        return False
    return True


def pick(category_patterns: list, genre: str, bpm_min: float, bpm_max: float,
         key_root: Optional[str], mode: Optional[str], rng: random.Random) -> Optional[dict]:
    """Pick a pattern from a list, filtered + ranked, with a small bit of randomness."""
    # Tier 1: exact genre + bpm + key match
    exact = [p for p in category_patterns
             if matches_genre(p, genre) and matches_bpm(p, bpm_min, bpm_max) and matches_key(p, key_root, mode)]
    if exact:
        return rng.choice(exact)
    # Tier 2: genre + bpm
    fuzzy = [p for p in category_patterns
             if matches_genre(p, genre) and matches_bpm(p, bpm_min, bpm_max)]
    if fuzzy:
        return rng.choice(fuzzy)
    # Tier 3: just bpm
    bpm_only = [p for p in category_patterns if matches_bpm(p, bpm_min, bpm_max)]
    if bpm_only:
        return rng.choice(bpm_only)
    # Tier 4: anything
    if category_patterns:
        return rng.choice(category_patterns)
    return None


def suggest_patch(instruments: list, genre: str, rng: random.Random) -> Optional[dict]:
    """Pick a captured patch that fits the genre. Looks at patch notes + tags."""
    g = genre.lower()
    candidates = []
    for inst in instruments:
        for patch in inst.get("patches", []):
            blob = (patch.get("notes", "") + " " + patch.get("name", "")).lower()
            score = 0
            # Soft heuristic scoring
            keywords = {
                "house":      ["square", "bass", "stab", "pluck", "hook", "saw"],
                "techno":     ["acid", "squelch", "303", "pluck", "saw"],
                "trap":       ["sub", "808", "bass"],
                "hip-hop":    ["bass", "moog", "lead", "stab", "warm"],
                "dub":        ["bass", "sub", "spring"],
                "jazz":       ["pad", "warm", "ep", "rhodes", "wurli"],
                "neo-soul":   ["pad", "wurli", "rhodes", "ep", "warm"],
                "soul":       ["wurli", "rhodes", "warm", "ep"],
                "lofi":       ["wurli", "warm", "ep", "pad", "muffled"],
                "rnb":        ["pad", "wurli", "rhodes", "warm"],
                "disco":      ["pluck", "square", "bass", "stab"],
                "dnb":        ["reese", "sub", "bass"],
                "trance":     ["lead", "saw", "pad"],
                "ambient":    ["pad", "wash", "atmospheric"],
                "minimal":    ["sub", "bass", "pluck"],
                "acid":       ["acid", "squelch", "303", "bass"],
                "afrobeat":   ["square", "lead", "stab"],
                "reggae":     ["bass", "sub", "spring", "skank"],
                "dancehall":  ["bass", "sub", "skank"],
            }
            for k, words in keywords.items():
                if k in g:
                    for w in words:
                        if w in blob:
                            score += 1
            if score > 0:
                candidates.append((score, inst, patch))
    if not candidates:
        # nothing matched — return any patch as fallback
        all_patches = [(inst, p) for inst in instruments for p in inst.get("patches", [])]
        if all_patches:
            inst, p = rng.choice(all_patches)
            return {"instrument": inst["_slug"], "patch": p["name"], "notes": p.get("notes", ""), "match_score": 0}
        return None
    candidates.sort(key=lambda x: -x[0])  # highest score first
    # Take top 3 and pick randomly to add variety
    top = candidates[:3]
    score, inst, patch = rng.choice(top)
    return {
        "instrument": inst["_slug"],
        "patch": patch["name"],
        "notes": patch.get("notes", ""),
        "match_score": score,
    }


def build_combined_midi(progression: dict, bass: dict, drums: dict, out_path: Path) -> bool:
    """Write a single .mid file with 3 tracks: chords, bass, drums. Keeps the rules
    we've locked in — no tempo metadata, just notes."""
    repo = find_repo_root()

    def load_mid(p):
        if not p:
            return None
        midi_path = repo / p["_midi"]
        if not midi_path.exists():
            return None
        return mido.MidiFile(str(midi_path))

    combined = mido.MidiFile()
    combined.ticks_per_beat = 480

    # Helper to add a track from another .mid file (skip meta messages)
    def add_track_from(src_mid, channel: int):
        if not src_mid:
            return
        scale = combined.ticks_per_beat / src_mid.ticks_per_beat
        tr = mido.MidiTrack()
        for msg in src_mid.tracks[0]:
            if msg.is_meta and msg.type != "end_of_track":
                continue
            if msg.type in ("note_on", "note_off"):
                new_time = int(msg.time * scale)
                tr.append(msg.copy(channel=channel, time=new_time))
        combined.tracks.append(tr)

    prog_mid = load_mid(progression)
    bass_mid = load_mid(bass)
    drum_mid = load_mid(drums)
    if not (prog_mid or bass_mid or drum_mid):
        return False

    add_track_from(prog_mid, 0)   # chords on MIDI channel 1
    add_track_from(bass_mid, 1)   # bass on channel 2
    add_track_from(drum_mid, 9)   # drums on channel 10 (GM convention)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    combined.save(str(out_path))
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--genre", default="house", help="genre to match (house, hip-hop, dub, etc.)")
    ap.add_argument("--bpm", type=float, default=None, help="target BPM (composer picks within ±10)")
    ap.add_argument("--bpm-min", type=float, default=None)
    ap.add_argument("--bpm-max", type=float, default=None)
    ap.add_argument("--key", default=None, help="key root (C, A, F#, etc.)")
    ap.add_argument("--mode", default=None, choices=["minor", "major"])
    ap.add_argument("--seed", type=int, default=None, help="reproducible — same seed = same composition")
    ap.add_argument("--out", default=None, help="write combined .mid to this path (3 tracks)")
    args = ap.parse_args()

    repo = find_repo_root()
    patterns = load_patterns(repo)
    instruments = load_instruments(repo)

    # Resolve BPM range
    if args.bpm_min is not None and args.bpm_max is not None:
        bpm_min, bpm_max = args.bpm_min, args.bpm_max
    elif args.bpm is not None:
        bpm_min, bpm_max = args.bpm - 10, args.bpm + 10
    else:
        bpm_min, bpm_max = 60, 180  # any tempo

    rng = random.Random(args.seed)

    progression = pick(patterns["progressions"], args.genre, bpm_min, bpm_max, args.key, args.mode, rng)
    bass = pick(patterns["bass"], args.genre, bpm_min, bpm_max, args.key, args.mode, rng)
    drums = pick(patterns["drums"], args.genre, bpm_min, bpm_max, None, None, rng)  # drums have no key
    patch = suggest_patch(instruments, args.genre, rng)

    # Output
    print(f"╭─ composed session ─────────────────────────────────────╮")
    print(f"│  genre:    {args.genre}")
    print(f"│  bpm:      {bpm_min:.0f}–{bpm_max:.0f}" + (f" (target {args.bpm:.0f})" if args.bpm else ""))
    if args.key or args.mode:
        print(f"│  key:      {args.key or '?'} {args.mode or ''}")
    if args.seed is not None:
        print(f"│  seed:     {args.seed}")
    print(f"╰────────────────────────────────────────────────────────╯")
    print()

    def show(label, p):
        if not p:
            print(f"  {label:<14} (no match)")
            return
        bpm = p.get("bpm", "—")
        key = p.get("key") or p.get("key_root") or "—"
        mode = p.get("key_mode") or ""
        tags = " · ".join(p.get("tags", [])[:4])
        print(f"  {label:<14} {p['name']:<24}  {bpm:>5} BPM  {str(key)+' '+mode:<8}  {tags}")
        vibe = p.get("vibe")
        if vibe:
            print(f"  {'':<14} \"{vibe[:90]}{'…' if len(vibe)>90 else ''}\"")

    show("PROGRESSION", progression)
    show("BASS LINE",   bass)
    show("DRUMS",       drums)
    print()
    if patch:
        print(f"  PATCH SUGGEST  {patch['instrument']}/{patch['patch']}  (match score: {patch['match_score']})")
        if patch.get("notes"):
            print(f"  {'':<14} \"{patch['notes'][:90]}{'…' if len(patch['notes'])>90 else ''}\"")

    # Optionally write combined .mid
    if args.out:
        out_path = Path(args.out)
        ok = build_combined_midi(progression, bass, drums, out_path)
        if ok:
            print(f"\n✓ combined .mid written to {out_path}")
            print(f"  3 tracks: chords (ch1) + bass (ch2) + drums (ch10)")
            print(f"  drag into Live, route channels to your synths + drum rack")
        else:
            print(f"\n✗ couldn't write combined .mid (no source MIDI files found)")


if __name__ == "__main__":
    main()
