# thespacepit pattern library

The MIDI pattern library that every spacepit sample pack uses. **Decoupled from any specific synth** — these patterns get sent to whichever instrument is currently set up. The result: every pack ships with the same progressions / drum patterns / arps applied to its gear's character.

**This is the foundation of the synthesis machine.** When we capture the MS-20 next month, we run these same patterns through it. Producers who buy multiple packs can layer them in the same key, same BPM, same progression.

## Structure

```
patterns/
├── progressions/   chord progressions — JSON files
│   └── minor-classic.json    ("Cm Ab Eb Bb")
├── drums/          drum patterns — kick/snare/hh sequences as MIDI ticks
│   └── four-on-floor.json
├── arps/           single-chord arp patterns (sequencer steps, not chord progressions)
├── bass/           bass lines as MIDI note sequences
├── melodies/       lead melodies as MIDI note sequences
└── tributes/       tribute patch metadata — recreate iconic sounds
    └── daftpunk-house.json
```

## Pattern file format (JSON)

### Chord progression
```json
{
  "name": "minor-classic",
  "type": "progression",
  "key": "Cm",
  "bpm": 120,
  "vibe": "classic minor i-VI-III-VII, hip-hop / R&B friendly",
  "chords": ["Cm", "Ab", "Eb", "Bb"],
  "bars_per_chord": 2,
  "tags": ["minor", "hip-hop", "rnb", "soul"]
}
```

### Drum pattern (24 PPQN, ticks within a bar)
```json
{
  "name": "four-on-floor",
  "type": "drums",
  "bpm": 124,
  "ppqn": 24,
  "bars": 1,
  "vibe": "house / techno foundation",
  "tracks": {
    "kick":      [0, 24, 48, 72],
    "snare":     [24, 72],
    "hh-closed": [12, 36, 60, 84]
  },
  "tags": ["house", "techno", "4x4"]
}
```

### Tribute patch metadata
```json
{
  "name": "daftpunk-house",
  "type": "tribute",
  "artist": "Daft Punk",
  "era": "1997-2001",
  "reference_tracks": ["Around the World", "One More Time"],
  "synth_class": "analog mono with square + saw oscillators",
  "patch_notes": "VCO 1 square 8', filter 50% cutoff + 30% res, env amount 20%, fast attack on both envelopes, sustain mid",
  "best_on": ["Moog Grandmother", "Mother-32", "MS-20", "Pro-One"],
  "tags": ["house", "french-house", "tribute"]
}
```

## How patterns get used

- **Capture:** `gmprog-saved minor-classic` loads the progression and fires it on the Grandmother (or whichever synth is connected)
- **Cross-pack consistency:** Every pack uses the same set of patterns → producers can layer loops from different gear in the same key/BPM
- **Pack docs:** Each pack's documentation lists which patterns were used → producers know what they're getting

## MIDI export — patterns/midi/

Every JSON pattern (except tributes — those are patch references, not music) can be auto-converted to a real `.mid` file at `patterns/midi/<category>/<name>.mid`. Run:

```bash
python tools/patterns/json-to-midi.py            # convert ALL JSON patterns
python tools/patterns/json-to-midi.py --category bass
python tools/patterns/json-to-midi.py --file patterns/bass/house-pump.json
```

Output rules locked in (matches the Ableton drop-in convention):
- **No tempo metadata** — Ableton uses session tempo
- **No track_name, no time_signature** — clean note events only
- **480 ticks_per_beat** — high resolution
- **note_on / note_off only** — nothing else

Drop any of these `.mid` files onto a MIDI track in Ableton and they play immediately.

### Event-list schema (new — for bass / arp / melody)

The richer schema for melodic patterns:

```json
{
  "name": "house-pump",
  "type": "bass",
  "genre": "house",
  "bpm": 124,
  "ppqn": 24,
  "bars": 1,
  "key_root": "C",
  "key_mode": "minor",
  "vibe": "Classic 4/4 house bass — root on every quarter, soft accent on 1 and 3.",
  "events": [
    {"tick": 0,  "note": "C2", "vel": 110, "len": 22},
    {"tick": 24, "note": "C2", "vel": 88,  "len": 22},
    {"tick": 48, "note": "C2", "vel": 100, "len": 22},
    {"tick": 72, "note": "C2", "vel": 88,  "len": 22}
  ],
  "tags": ["house", "4/4", "classic", "pump"]
}
```

Each event has `tick` (position from start at the ppqn), `note` (name or MIDI number), `vel` (velocity 1-127), `len` (duration in ticks).

## Adding new patterns

Drop a JSON file in the appropriate subfolder. The capture tools auto-discover them by filename. The `gmprog-saved`, `gmdrums-saved`, etc. functions reference the pattern by its `name` field.
