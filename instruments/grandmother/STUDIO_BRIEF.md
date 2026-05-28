# Moog Grandmother First Pack — Studio Brief

the dedicated mental space for the multisampling work. open this on your phone or second monitor while you're recording. don't scroll chat history mid-session.

---

## the mission

capture one Grandmother patch as a multisample. learn the craft. validate the full pipeline from synth → repo → web sampler. this is the FIRST sample pack in the spacepit gumroad store.

**first session goal:** one patch, 13 notes, raw only (+ spring reverb pass = free bonus variant). ~20 min recording + ~30 min editing. drop the WAVs in this folder. that's it.

if that works end-to-end, scale to 5 patches across 2-3 more sessions.

---

## why Grandmother first (instead of MS-20)

- you're a master at this synth — zero new-gear learning
- it's MIDI'd — you can SEQUENCE the 13 notes from Ableton (huge time saver)
- built-in spring reverb = free second variant in one session
- the patch design is in your hands already

---

## recording spec

- Grandmother 1/4" line out → TX-6 line in → USB → Ableton
- 24-bit / 48 kHz
- peak around -6 to -10 dBFS
- NO external FX, NO compressor, NO preamp coloration
- spring reverb OFF for raw pass (we'll do a second pass with it ON)

---

## first patch: CLASSIC MOOG BASS

THE iconic Moog ladder-filter sound. clean, fat, consistent across the keyboard.

- VCO 1: SAW, 16' (or 32' for sub territory — your call)
- VCO 2: OFF (or saw at -7 semis for very mild detune — optional)
- mixer: VCO1 full
- filter cutoff: ~25% (low — that's where the warmth lives)
- filter resonance: ~20% (subtle peak, not screaming)
- envelope amount on filter: 0 (we want the filter STATIC across the multisample — env on filter would vary across notes)
- VCA envelope: short attack, full sustain, medium release
- LFO: OFF
- spring reverb: **OFF**

verify before recording: play A2. should be a thick clean Moog bass — fat low end, slight filter character. no envelope movement, no LFO. STATIC patch.

---

## the 13 notes

A1, C2, Eb2, F#2, A2, C3, Eb3, F#3, A3, C4, Eb4, F#4, A4

(every minor third across 4 octaves)

---

## automated capture (the real time saver)

skip the Ableton MIDI clip + slice-by-hand workflow. the project has a python tool that does the whole job in one command:

- sends each MIDI note one at a time
- records each note as its own WAV
- names files with the locked convention automatically
- drops them into the right folder
- reports peak dBFS per note (warns if anything clipped)

**the workflow (3 commands):**

```bash
gmcheck-level    # 1. play a loud note for 5 sec — confirms TX-6 input is hot (-6 to -3 dBFS target)
gmcapture-dry    # 2. dry-run prints the plan, no recording. sanity check.
gmcapture-raw    # 3. real run. press enter, walk away ~85 sec, come back to 13 named WAVs.
```

behind `gmcapture-raw` is:

```bash
.venv/bin/python capture-synth.py \
  --instrument grandmother --patch moogbass --chain raw \
  --midi-port "Moog Grandmother" --midi-channel 1 \
  --audio-device "TX-6" --input-channels 1,2 \
  --note-range a1-a4 --step 3 \
  --velocities 100 --round-robins 1 \
  --sustain-sec 4 --tail-sec 2.5 \
  --sample-rate 48000 --bit-depth 24
```

**filename convention** is locked by the project: `<slug>_<patch>_<note>_v<velocity>_rr<roundrobin>.wav`. the tool generates `grandmother_moogbass_a1_v100_rr1.wav` … `grandmother_moogbass_a4_v100_rr1.wav` — 13 files. you don't have to think about names.

---

## where the WAVs go

`instruments/grandmother/patches/moogbass/raw/` (dry pass)
`instruments/grandmother/patches/moogbass/spring/` (spring pass)

(folders pre-created. the tool writes to them automatically based on `--chain` flag.)

---

## BONUS PASS: spring reverb variant (free)

after raw pass is done, leave the patch alone, flip the built-in spring reverb to ~30-40% wet, and run:

```bash
gmcapture-spring
```

same patch, same MIDI sequence, recorded through the Grandmother's onboard spring reverb. lands in `patches/moogbass/spring/`. two products from one session. **this is the move.**

same filename convention — `grandmother_moogbass_a1_v04_rr1.wav` etc., just inside the `spring/` folder. now you have raw + spring-wet of the same patch. two products from one session. **this is the move.**

---

## photo checklist (do this in the same session)

while the gear is set up, get the photos. the pack on gumroad sells with VISUALS — sample packs without good photography look like SoundCloud throwaways. yours should look like an album cover.

### setup
- Grandmother on the workbench, in its natural environment (not isolated/cleaned-up)
- TX-6 + cables visible — the actual signal chain doing the work
- other gear in the periphery is GOOD (sets context, makes it feel like a studio not a stock photo)
- natural light if possible (window) — otherwise ONE warm key light, not overhead fluorescent
- don't mix daylight + tungsten (looks weird — pick one color temp)
- camera on a stand or tripod (sharper than handheld)

### lighting

lighting is the single biggest factor between "stock photo of a synth" and "album cover." get this right and everything else falls into place.

**the recipe (one key + one fill + practical + the gear's own glow):**

1. **key light** — single warm source, off-axis (front-left or front-right), about 45° down. defines the form, makes the knobs cast soft shadows. ~3200K (warm tungsten).
2. **fill** — white foamcore ($5 from a craft store) leaned against something on the opposite side. catches spill from the key, lifts the shadow side so it's not black.
3. **practical in frame** — a warm Edison-bulb lamp visible somewhere in the shot. ambient warmth + sets time of day (evening session vibe). matches the spacepit lamp-amber `#F2B705` brand range.
4. **the gear's own LEDs** — turn the room lights DOWN. Grandmother panel indicators + TX-6 screen + MS-20 lights become characters in the shot. dim ambient = these pop.
5. *(optional)* **cool background wash** — small RGB light hidden BEHIND the gear pointing at the wall, set to deep blue/purple. wall washes cool, gear stays warm. cinematic spacepit-cosmos contrast.

**use what you have (tonight, $0):**
- any warm desk lamp = your key light. position it off-axis (front-left or front-right), 45° down.
- white foamcore or even a white pizza box = your fill (lean opposite the key).
- existing studio lamp = your practical in frame.
- room lights OFF. gear LEDs ON. shoot.

this gets you 70% of the way. ship the v1 pack tonight if speed > polish.

**if you want to buy (~$200, arrives in 2 days):**
- **Aputure Amaran 60d** (~$150) — bicolor LED panel, tungsten↔daylight, battery + AC. industry standard. does 80% of the work.
- **softbox modifier** ($30-50) — Aputure Light Dome mini for the 60d, or a generic 24" softbox. diffuses so the light is flattering not harsh.
- **white foamcore** ($5) — bounce card for fill.
- **the warm Edison-bulb lamp you already own** ($0) — sits in frame as the practical.

**optional cinematic add (+$90):**
- **Aputure MC mini RGB** — tiny pocket light, magnetic, internal battery. set to deep purple/blue, hide behind the gear pointing at the wall. teenage engineering shoots gear EXACTLY this way.

**what NOT to buy:**
- ring lights (face-lighting tool, terrible for gear)
- camera-mounted flash (kills mood)
- "creator" multi-light kits at $400 with cheap parts (one good light > three bad ones)
- color gels until the basics with white light are dialed

### shots to get (50-100 photos in a session, 5-10 keepers)

**hero shots (must-haves):**
- [ ] full Grandmother shot, centered, slightly elevated angle ("looking down on it")
- [ ] 3/4 angle showing depth — front-left or front-right
- [ ] straight-on front shot, level with the panel
- [ ] vertical orientation of the same — for IG stories / mobile

**detail shots:**
- [ ] filter section close-up (cutoff + res knobs)
- [ ] VCO section
- [ ] the patch cables if any are connected
- [ ] the keyboard
- [ ] brand badge / "Moog Grandmother" logo
- [ ] the back panel / output jacks
- [ ] spring reverb mention area if visible

**in-context shots:**
- [ ] full workbench wide — Grandmother + TX-6 + MS-20 + monitor
- [ ] your hand on a knob (in-use feel, no face needed)
- [ ] the Ableton screen mid-recording (shows the multisample workflow)
- [ ] cables running into/out of the TX-6

**atmosphere:**
- [ ] periphery gear out of focus
- [ ] room context — studio vibe, lamp light, the spacepit feel

### specs
- shoot RAW if possible (HEIC at highest quality on iPhone is fine)
- both horizontal AND vertical orientations
- avoid super wide-angle distortion (use 1x lens on phone, not 0.5x)
- don't over-stylize in post — clean color correction is enough

### where the photos go
`/Users/nickhook/projects/spacepit-sample-bank/instruments/moog-grandmother/photos/`

raw files. i'll select + crop + treat them for the spacepit /store page and the gumroad pack landing. you can also use them for IG / discord drops.

---

## what happens after you push

1. you commit + push raw/, spring/, and photos/ folders
2. my multisample-manifest code on spacepit-web sees the new audio files
3. patterns on /lessons/sampler that could use Moog auto-upgrade from synth → real Moog
4. you'll hear your actual Grandmother playing back in your browser within 24 hours
5. i pull 3-5 hero photos, treat them in spacepit's visual language, draft the /store/moog-grandmother landing page
6. we review together, ship the pack on gumroad

---

## the full pack (after session 1 succeeds)

5 patches in priority order:

1. **classic Moog bass** — saw + filter, the staple (first session)
2. **filter sweep lead** — saw + envelope on filter, the classic Moog wail
3. **sub sine** — triangle through fully closed filter for clean sub-bass duty
4. **acid squelch** — high resonance + envelope, the Moog answer to a 303
5. **patch-modulated weirdo** — using the semi-modular CV inputs, the "experimental Moog" sound

5 patches × 13 notes × 1 vel × 1 RR = 65 WAVs raw + 65 spring + 65 chained later = 195 WAVs in the full pack.

---

## craft reference (read once before first session, don't sweat in studio)

### why minor third spacing
3 semitones between anchors = max 1.5 semitones any zone has to interpolate. that's the natural-sounding pitch-shift range before formants munchkinize. minor third = optimal density vs. naturalism tradeoff.

### why no envelope on the filter for the first patch
envelope on filter means the filter cutoff moves over time. across 13 notes captured at different pitches with the same envelope, the FREQUENCY of the filter sweep stays the same but the perceived character changes per note (a low note has more filter content to sweep through than a high note). result: inconsistent character across the multisample. fix: keep filter STATIC for v1. envelope-on-filter patches come later as their own dedicated multisample.

### why MIDI-sequenced > hand-played
machine consistency. every key-hit has the same velocity, same timing. when you hand-play 13 notes you accidentally hit some harder than others. fine for a performance, bad for multisample consistency.

### why FULL release tail
amateur samples cut the release. listener can hear it — notes feel gated, artificial. the Moog VCA envelope releases with character. capturing the full release means producers can sustain notes naturally.

### why no round robins for melodic synths
RRs solve "machine gun" repetition for percussive samples retriggered at the same pitch. melodic synth playing different notes = each note already unique. RRs add nothing for melodic content.

### why raw + spring (two variants from one session)
raw = customer applies their own reverb. spring = the Grandmother's built-in character pre-applied. perceived premium product. teaches you to think about your gear's natural FX AS part of the sound, not optional.

### the deeper move
multisampling is freezing time. this exact Grandmother at this exact moment, captured forever. caps drift. the room will change. today's capture IS today's Grandmother. you're a museum curator, not a sample seller.

---

## post-session writeup (optional but valuable)

after the first session, text me a 2-3 paragraph dump of "what surprised me, what felt natural, what was harder than expected, what i'd do different next time." that becomes an anchor piece on the spacepit titled something like "i'd never multisampled before. starting with my Moog Grandmother." every chopper-deep producer who reads it self-identifies. pulls real audience.

---

**now go cook.** capture the dry pass, flip the spring, capture again, shoot the photos. drop everything in the folders. push. we listen together. craft is learned by doing → breaking → fixing → iterating.
