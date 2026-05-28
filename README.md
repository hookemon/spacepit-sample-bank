# the spacepit sample bank

> An archive of Nick Hook's studio. Every iconic patch on every piece of gear, captured through the spacepit's signal chain, documented in the act of being made. So when the gear leaves, the sound stays.

[**thespacepit.com**](https://thespacepit.com) · built by [**@nickhook**](https://instagram.com/nickhook) at [**@thespacepit**](https://instagram.com/thespacepit)

---

## what this is

Most sample packs are "I plugged a synth into an interface and pressed record." Fine, useful, generic. This is not that.

The spacepit is a studio in NYC with a 20-year history — Moog 500-series delays, an EMT 140 plate, a Radial J+4, the board, the chains that defined records you've heard. The sample bank is what happens when one producer decides to **document that studio** instead of just record in it.

Each sample carries its provenance: which synth, which patch, what mode the synth was in, which chain it ran through, captured when. That metadata is the product as much as the audio. When you load a `spacepit-jp8000` sound in Ableton, you know it went *through* the spacepit, not just *near* it.

This repo contains:

- **the bench** — the capture pipeline + UI we use to run sessions (`tools/dashboard/`)
- **per-instrument manifests** — each piece of gear's iconic patches, MIDI quirks, signal chain, and audio fingerprint
- **the methodology** — auto-discard silence, live waveform pre-flight, chain-credit-in-WAV-metadata, the works
- **synthesis tools** — pattern library, composer, JP-8000 preset scraper (and counting)
- **eventually, the packs themselves** — shipped as [GitHub Releases](https://github.com/hookemon/spacepit-sample-bank/releases) once each instrument's bank is ready

The captures themselves (the WAVs) live on a separate drive — public repo stays light + clone-friendly.

---

## the bench

A local web tool that turns your hardware into multisample instruments. Plug in a synth, the bench finds it, learns its MIDI quirks once, fires captures on command. Built for the way producers actually work: visual signal verification, one-button capture queue, per-patch provenance documented automatically.

What it does:

- **Live waveform** — always-on input scope (Web Audio API), 3-band psychedelic palette, switches to spectrogram waterfall when zoomed out in time so you SEE the harmonic content of a 7-second sustain
- **Iconic patches sidebar** — your curated list per synth (the JP-8000's supersaw-1, mega-saw, tb-echo, etc.). Click one → MIDI Program Change fires → synth jumps to that preset
- **Bind from synth** — scroll your synth manually, bench listens for the PC + Bank Select it broadcasts, saves the binding to the manifest
- **Bang it out** — auto-capture queue. Cycles through every iconic patch, fires Program Change, runs a multisample (a2-a5 chromatic with configurable step), saves WAVs with peak verification
- **Silence guard** — captures with peak < -46 dBFS are NOT saved, with a loud warning. 3 consecutive silent = abort the run with a diagnostic. Impossible to ship empty WAVs.
- **Per-synth midi_quirks** — every instrument's manifest declares its weird shit. JP-8000 needs PATCH mode + Performance Control Channel OFF. MS-20 has no MIDI presets at all. Drum machines want channel 10. The bench respects each one.
- **Pack builder** — generates Ableton `.adv` + `.adg`, SFZ, Decent Sampler, drum racks. One command per instrument. Output zipped + ready to share.

Run it:

```bash
git clone https://github.com/hookemon/spacepit-sample-bank.git
cd spacepit-sample-bank
# the bench lives in tools/dashboard/ — see its README for setup
open tools/dashboard/README.md
```

The bench expects a Mac with a USB-MIDI interface (mio, MOTU, iConnectivity-anything), an audio interface, and at least one synth plugged in. Tested heavily on Mac mini M-series. Should work on Intel Macs + Linux with minor tweaks.

---

## current state

| instrument | iconic patches | midi_quirks documented | captured |
|---|---|---|---|
| Moog Grandmother | scaffolded | yes | 225 WAVs (raw + spring chain) |
| Roland JP-8000 | 12 patches hand-linked to factory PCs | yes (PATCH mode + Perform Ctrl OFF + MSB=81 nuance) | pending studio session |
| Sequential Prophet-6 | 12 patches scaffolded | yes | pending |
| Korg MS-20 | scaffolded | yes (no MIDI preset recall — manual only) | pending |
| Korg M1 | scaffolded | yes | pending |
| Korg Triton | scaffolded | yes | pending |
| Yamaha DX7 | scaffolded | yes | pending |
| E-mu SP-1200 | scaffolded | yes | pending |

JP-8000 captures ship first as `spacepit-jp8000-vol1` — a full multisample + Ableton instrument pack with chain credits baked in.

---

## philosophy

A few principles, locked in commits:

1. **Documentation IS the product.** Every WAV ships with the signal chain it traveled through. Sample-pack culture is starved for provenance; we provide it as default.
2. **Each synth gets its own personality.** The `midi_quirks` framework respects every instrument's quirks instead of flattening them. JP-8000 in PATCH mode + Performance Control off is a documented fact in the manifest, not a tribal-knowledge gotcha.
3. **The chain is the moat.** Anyone can sample a JP-8000. Only Nick can sample a JP-8000 through Nick's specific Moog delay + EMT plate + SSL chain. Every captured sample carries that credit.
4. **Verify before claim done.** The bench has a live waveform you check before captures. The capture pipeline refuses to save silent WAVs. The queue pre-flights a test note before firing all 12. "It worked" requires evidence.
5. **Build by a producer, for producers.** Built BY someone whose first credit was 20 years ago, whose ear knows what matters. Not BY a tech team for a market segment.

---

## credits

Built by **Nick Hook** at thespacepit, NYC. Co-built with **Claude (Anthropic)** — every commit message at the bottom credits AI co-authorship because pretending otherwise would violate principle #4 above.

Sounds + chains from the spacepit studio: Moog 500-series delays, EMT 140 plate, Radial J+4 stereo line driver, the SSL channels, the Apollo 8p.

The lineage: Brian Eno, Trent Reznor, Aphex Twin, Madlib, Flying Lotus — producers who treated their tools as part of the art and made the tools themselves.

---

## links

- 🎹 [thespacepit.com](https://thespacepit.com)
- 📷 [@nickhook on Instagram](https://instagram.com/nickhook)
- 🛠 [@thespacepit on Instagram](https://instagram.com/thespacepit)
- 🎧 [calm + collect (the label)](https://thespacepit.com/calm-collect)
- 📨 [contact](mailto:nick@thespacepit.com)

## license

The bench code (`tools/`) is MIT-licensed — fork it, run it, capture your own studio. The chain documentation, photos, and shipped sample banks are © Nick Hook / thespacepit, all rights reserved.

If you fork the bench and capture your own instruments, credit "captured with the bench by @thespacepit" if you ship samples publicly — appreciated but not required.

---

*"We're not selling 1000 saws. We're selling a Nick Hook take on the JP-8000."*
