# thespacepit master sample bank

A studio archive — every piece of gear in the spacepit, sampled and processed through your signature chains. So when gear leaves, the sound stays. Doubles as content (process videos), site fuel (Sounds widget on /gear pages), and product (downloadable kits, eventually).

Source spreadsheet: `~/Library/CloudStorage/Dropbox/My Mac (Mac-mini)/Downloads/thespacepit gear list (1).xlsx` — ~95 sampleable pieces.

---

## Folder shape

```
instruments/<slug>/
├── manifest.json
├── kits/                 (drum machines, samplers — one subdir per kit)
│   └── <kit-name>/
│       ├── raw/
│       ├── processed/
│       └── kit-notes.md
├── patches/              (synths — one subdir per programmed patch)
│   └── <patch-name>/
│       ├── raw/
│       ├── processed/
│       └── patch-notes.md
├── loops/
│   ├── raw/
│   └── processed/
└── exports/              (Move kit, MPC, OP-Z, EP-133, SFZ, etc.)
```

Drum-machine instruments use `kits/`. Synth instruments use `patches/`. Both can have `loops/` and `exports/`.

For built-in drum machines (808, 909, LinnDrum, DR-550 — single sound set), use `kits/factory/`. For samplers (SP-1200, MPC, Digitakt, EP-133 — user-loaded kits), name each kit (`kits/ny-grit/`, `kits/dancehall/`, etc.).

---

## Capture tiers

Decide per instrument. Record the choice in the manifest.

- **Quick** (~30 min) — character one-shots only. For cheap/oddball gear.
- **Standard** (~2 hrs) — chromatic every 3 semitones × 3 velocities + loops. Default.
- **Gold** (~half day) — every note × 5 velocities × round-robins + sustained holds + many loops. Hero pieces only.

Hero candidates: SP-1200, MS-20, TR-808, Wurlitzer 200, ARP 2600, MPC60II, Mono/Poly, Linn LM-2.

---

## Capture spec — drum machines

Per voice, capture variations sweeping the box's parameter range:

- **Quick**: kick × 3, snare × 3, hh-closed, hh-open, ride, crash, toms (lo/mid/hi), each percussion voice × 1. 4-8 loops.
- **Standard**: each voice × 3-5 variations. 8-16 loops.
- **Gold**: each voice × 6-10 variations + velocity layers if the box supports it. Sustained holds (HH ringouts, gated kicks). 16+ loops.

Voice abbreviations: `kick`, `snare`, `hh-closed`, `hh-open`, `tom-lo`, `tom-mid`, `tom-hi`, `clap`, `ride`, `crash`, `cowbell`, `clave`, `conga`, `rim`.

---

## Capture spec — synths

Per patch:

- **Quick**: 5-8 character notes + 2-3 sustained tones. 2 loops. 1 patch.
- **Standard**: every 3 semitones × 3 velocities × 1 round-robin. 2-4 sustained tones. 4-6 loops. 2-3 patches per instrument.
- **Gold**: every note × 5 velocities × 2-3 round-robins. 4-6 sustained tones (8 sec). 12+ loops sweeping BPM/key. Plus filter sweeps, mod-wheel passes, pitch-bend slides as standalone WAVs. 3-5 patches.

Per patch, write `patch-notes.md` — photo of panel, knob positions, external CV, BPM/sync notes, one-line vibe descriptor. You'll thank yourself in 3 years when you recreate.

---

## Naming

All filenames lowercase, hyphens not spaces, underscores between sections.

**Drum one-shots**: `<slug>_<voice>_<variant>.wav`
- `sp1200_kick_a.wav`, `sp1200_kick_b.wav`, `sp1200_hh-closed_a.wav`

**Synth multisample**: `<slug>_<patch>_<note>_v<velocity>_rr<roundrobin>.wav`
- `ms20_lead01_c3_v04_rr1.wav`
- `ms20_lead01_cs3_v04_rr1.wav` (sharps as `s` for filesystem safety)
- Velocity 1-5, round-robin 1-3

**Loops**: `<slug>_<patch-or-empty>_loop<NN>_<BPM>bpm_<KEY>_<chain>.wav`
- `sp1200_loop01_85bpm_dry.wav`
- `ms20_bass01_loop01_85bpm_am_dry.wav`

**Processed files** append the chain tag with double underscore:
- `sp1200_kick_a__244.wav`
- `ms20_lead01_c3_v04_rr1__rat-plate.wav`

Raw lives in `raw/`, processed in `processed/`. Same basename, different chain tag.

---

## Signature chains (the TSP IP)

The processed versions are what makes this not-generic. Run raws through:

- `__244` — Tascam 244 (tape squish + head bump)
- `__moog-filter` — Moog Ladder 500-series
- `__sp1200` — sampled into SP-1200 (12-bit, 26.04kHz)
- `__rat-plate` — Rat (Keeley) → EMT plate
- `__fucifier` — Evol Audio Fucifier
- `__space-echo` — Boss RE-20
- `__bbd` — analog BBD delay (MF-104M)
- `__la610` — UA LA-610
- `__pultec` — Pultec MEQ-5
- `__plate` — EMT plate only
- `__spring` — spring reverb (unit TBD)

You don't need every chain on every instrument. Pick 1-2 that flatter the source. Note which were captured in the manifest.

Per chain, eventually write a recipe sheet (knobs, gain staging, photo) so you can recreate in a year.

---

## Session setup

- 24-bit / 48 kHz default (SP-1200 raw stays native if captured into the unit)
- DI / line out clean, peak −6 dBFS, no clip
- One Ableton session per instrument; tracks per chain pre-routed for re-amping the raws
- Photograph the panel / patch sheet before tearing down

---

## Workflow per instrument

1. Set up — mic / DI, ableton template, photograph patch
2. Capture raw (per tier)
3. Capture processed (re-amp raws through chosen chains)
4. Build loops (4-8 takes at common BPMs)
5. Edit + name (trim, label, fade)
6. Fill in `manifest.json`
7. Export — build Move kit (and other formats if time) into `exports/`
8. Load on Move, verify it works
9. Log process-video shot list while fresh
10. Eventually: sync to spacepit-web (Sounds widget on `/gear/<slug>`)

---

## Starter instruments (scaffolded)

- `instruments/sp-1200/` — drum machine, **Gold** tier
- `instruments/ms-20/` — synth, **Gold** tier

Build these first as the template references. Once both are done end-to-end, the next 90+ pieces become a repeatable assembly line.

---

## Tools

- `tools/capture/capture-synth.py` — chromatic synth capture. Sends MIDI notes, records each note across the chosen velocity layers + round-robins, names WAVs per spec into `patches/<patch>/raw/`. Unattended.
- `tools/capture/capture-drum.py` — drum-machine capture. Walks a per-tier voice matrix (kick × N, snare × N, hh × N, …), supports manual (you trigger the pad) or MIDI (tool sends notes) mode, names WAVs into `kits/<kit>/raw/`.
- `tools/capture/list-devices.py` — prints available MIDI ports + audio input devices.

See [`tools/capture/README.md`](tools/capture/README.md) for install + usage.

---

## Sync to spacepit-web (validated 2026-05-25)

When patch WAVs land in `instruments/<gear>/patches/<patch>/<variant>/`, run:

```bash
cd /Users/nickhook/projects/spacepit-web
npm run sync-samples
```

This:
1. Walks every `instruments/<gear>/patches/<patch>/<variant>/` and `kits/<kit>/<variant>/` dir
2. Parses filenames flexibly — handles `<slug>_<patch>_<note>_v<vel>_rr<rr>.wav`, simple `<slug>-<patch>-<note>.wav`, lowercase or upper notes, sharps as `#` or `s`
3. Variants recognized: `raw/`, `processed/`, `spring/`, `wet/`, `chained/`, `warm/`
4. Copies WAVs into `spacepit-web/public/samples/<gear>/patches/<patch>/<variant>/` (gitignored — too big for git, source of truth stays here)
5. Generates `spacepit-web/app/lessons/_lib/sample-bank-manifests.ts` — one `MultisampleManifest` export per patch+variant, with computed zone ranges
6. Sampler auto-picks up new manifests via `ALL_INSTRUMENTS` import

`npm run dev` after that — new patches appear in the /lessons/sampler instrument picker next to the synth engines.

End-to-end validated on 2026-05-25 with 16 Grandmother patches (10 raw + 6 spring variants).

---

## What's NOT sampled

Skip these — they're chain ingredients or infra, not sources: soundcards, mixers, speakers, casing/lunchboxes, power supplies, MIDI/CV converters, eurorack utility modules (Brains, MATHS, Optomix, etc.).
