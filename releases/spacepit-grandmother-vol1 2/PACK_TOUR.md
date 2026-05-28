# Pack Tour — Moog Grandmother

Creator-facing walkthrough. Open this when you want to remember what's in the pack and what's next.

## Currently in this folder

```
spacepit-grandmother-vol1/
├── README.md              ← producer-facing pack docs (= Gumroad listing copy)
├── PACK_TOUR.md           ← this file (creator-facing)
├── landing.html           ← interactive landing page (browse all loops + audio previews)
├── demo.wav               ← 46-sec pack tour audio (headline preview)
├── pack-inventory.json    ← machine-readable inventory of every file
├── audio/
│   ├── multisamples/      ← 195 WAVs across patches × chains
│   └── loops/             ← 34 tempo-tagged loops
├── instruments/
│   ├── sfz/               ← 15 SFZ presets (universal cross-DAW)
│   ├── decent-sampler/    ← 15 Decent Sampler .dspreset files
│   ├── ableton/           ← (empty — next session)
│   ├── logic-exs24/       ← (empty — next session)
│   ├── move-kit/          ← (empty — Tier 3)
│   └── ep-133/            ← (empty — Tier 3)
├── images/                ← 8 hero photos, color + rotation corrected
└── docs/PATCH_NOTES.md    ← combined patch documentation
```

## To view the landing page

```bash
gmlanding              # starts a local server + opens in browser
```

Or manually: `cd` into this folder, `python3 -m http.server 8765`, browse to http://localhost:8765/landing.html

## To rebuild after changes

```bash
gmpack                 # full rebuild from source bank state
gmpackopen             # opens this folder in Finder
gmpackzip              # zips for Gumroad upload
gmdemo                 # rebuilds just the demo audio
gminventory            # regenerates just the inventory JSON
```

## What's left to ship

1. **Ableton native presets** (Tier 2) — Sampler/Drum Rack .adv/.adg files
2. **Logic EXS24 presets** (Tier 2)
3. **TE Move kit** (Tier 3)
4. **EP-133 K.O. II project** (Tier 3)
5. **Demo BEAT** (vs the current demo audio which is a tour) — make a beat in Ableton using the pack samples
6. **Gumroad listing** — copy from README.md, hero photo from images/, audio from demo.wav
7. **Spacepit-web landing page** — port landing.html to the spacepit-web Sanity / Next.js stack
8. **IG drop** — tag @teenage.engineering, link to Gumroad
