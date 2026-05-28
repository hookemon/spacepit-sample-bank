# MS-20 First Pack — Studio Brief

the dedicated mental space for the multisampling work. open this when you sit down at the MS-20. don't scroll chat history mid-session.

---

## the mission

capture one MS-20 patch as a multisample. learn the craft. validate the full pipeline from synth → repo → web sampler. this is the FIRST sample pack in the spacepit gumroad store.

**first session goal:** one patch, 13 notes, raw only. ~45 min recording + ~30 min editing. drop the WAVs in this folder. that's it.

if that works end-to-end on the web side, scale to 5 patches across 2-3 more sessions.

---

## recording spec

- MS-20 line out → audio interface line in (DI, no mic, no FX)
- 24-bit / 48 kHz
- peak around -6 to -10 dBFS (headroom for editing later)
- one note at a time, 8-10 seconds hold + FULL release tail

---

## first patch: SELF-RESONANT BASS

the iconic MS-20 sound. filter screaming, saw underneath for body.

- VCO 1: SAW, sub octave on
- VCO 2: off (or detuned saw for thickness)
- mix: VCO 1 only (or VCO 1 dominant)
- LPF: cutoff LOW (~9 o'clock), peak HIGH (right at self-oscillation point)
- HPF: off (full open)
- ENV1 → VCA: short attack, full sustain, medium release
- LFO: off
- patch the keyboard CV → VCO frequency normally
- sub octave switch ON for that extra low end

verify before recording: play A2. should be a thick bass with a screaming filter overtone. that's the sound.

---

## the 13 notes (every minor third, A1 to A4)

A1, C2, Eb2, F#2, A2, C3, Eb3, F#3, A3, C4, Eb4, F#4, A4

---

## per-note procedure

1. play the note, hold key for 8-10 sec
2. release the key
3. let the natural decay ring all the way out (~2 sec)
4. record one clip per note
5. label the clip with the note name as you go (saves you 30 min in editing)

between notes: don't touch the knobs. ANY drift breaks the multisample consistency.

---

## editing in ableton

(you know this part — listed for completeness)

- trim attack: ~5ms pre-roll before the transient
- fade last 50ms of tail to absolute silence (prevents click on release)
- normalize each WAV to -3 dBFS
- remove DC offset (right-click → process → DC offset removal)
- consolidate as 24-bit / 48 kHz WAV
- name them with this exact convention:
  - `ms20-resobass-A1.wav`
  - `ms20-resobass-C2.wav`
  - `ms20-resobass-Eb2.wav`
  - etc.

filename convention matters. the manifest in code parses `[instrument]-[patchname]-[note].wav` automatically. typo = note doesn't map.

---

## where the WAVs go

`/Users/nickhook/projects/spacepit-sample-bank/instruments/ms-20/raw/resobass/`

if `raw/resobass/` doesn't exist, just make it. drop all 13 WAVs in there. commit + push to the repo.

---

## what happens after you push

1. my multisample-manifest code on spacepit-web sees the new path
2. relevant patterns on /lessons/sampler auto-upgrade from MS-20 synth → MS-20 real
3. you'll hear the actual MS-20 playing back in your browser within 24 hours of pushing
4. we listen together, decide what's next

---

## the full pack (after session 1 succeeds)

5 patches in priority order:

1. **self-resonant bass** — THE sound (first session)
2. **classic saw bass through LPF** — clean working bass
3. **white noise → filter** — wind, texture, snare tops
4. **HP-filtered acid lead** — the squelch
5. **noise + sample-and-hold** — the experimental one

5 patches × 13 notes × 1 vel × 1 RR = 65 WAVs raw. plus chain pass = 130 WAVs in the full pack. one solid session captures one patch. ship raw first, chain pass second.

---

## chain pass (later, second-pass enhancement)

after the raw pack is locked, run the same 65 WAVs through the spacepit chain (tape + drive + plate, mild) and save to:

`/Users/nickhook/projects/spacepit-sample-bank/instruments/ms-20/chained/resobass/`

doubles pack value. teaches you to author the spacepit signature AS a product.

---

## craft reference (read once before first session, don't sweat in studio)

### why minor third spacing
3 semitones between anchors = max 1.5 semitones any zone has to interpolate. that's the natural-sounding pitch-shift range before formants munchkinize. every minor third = optimal density vs. naturalism tradeoff. tighter is wasted effort for synths, wider sounds artificial.

### why one velocity for MS-20
monosynth has no velocity sensitivity. one capture covers full velocity range, scaled digitally on playback. 67% time savings vs velocity-sensitive instruments. capture velocity layers ONLY when the source has actual velocity response.

### why no round robins for melodic synths
RRs solve "machine gun" repetition for percussive samples retriggered at the same pitch. melodic synth playing different notes = each note is already unique. RRs add nothing for melodic content. they DO matter for drum machines.

### why 8-10 second captures
- 0-50ms: attack character (where the patch lives)
- 50ms-3s: sustain body
- 3-8s: release tail (the MS-20's filter decay IS part of the sound)
- buffer at end: safety
- shorter = you'll wish you had more tail
- longer = wasted editing time

### why FULL release tail matters
amateur samples cut the release short. you can hear it in cheap packs — notes feel gated, artificial. the MS-20's filter releases with character. capturing the full release means when a producer plays your sample, the natural decay plays out — sounds like the real synth.

### why both raw + chained variants
raw = customer applies their own character. chained = your spacepit signature pre-applied. shipping both = perceived premium product + you start authoring the "nick hook sound" as a buyable thing. that's the spacepit brand crystallized into something a producer can own.

### the deeper move
multisampling is freezing time. this exact MS-20 at this exact moment, captured forever. caps drift. the room will change. your hands will play differently in 2030. today's capture IS today's MS-20. you're a museum curator, not a sample seller. every editing decision starts mattering more when you hold it that way.

---

## post-session writeup (optional but valuable)

after the first session, if you want — text a 2-3 paragraph dump of "what surprised me, what felt natural, what was harder than expected, what i'd do different next time." that becomes an anchor piece on the spacepit titled something like "i'd never multisampled before. here's what i learned." every chopper-deep producer who reads it self-identifies. pulls real audience.

---

**now go cook.** don't try to nail it the first session. capture, edit what you can, drop the folder, push. we listen together. craft is learned by doing → breaking → fixing → iterating, not from any document.
