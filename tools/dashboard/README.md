# thespacepit capture dashboard

Web UI for capturing instruments into the sample bank. Runs locally at `http://localhost:8001`. Talks to your gear over MIDI, records WAVs from your audio interface, and writes manifests + presets into `instruments/<slug>/`.

## Quick start

From any Terminal tab:

```
bankserver
```

That starts the server in the current tab. The tab "owns" it — Ctrl+C in that tab kills it. Mic permission inherits from this tab, so audio capture works.

Then open Safari to **http://localhost:8001**.

To check status from any other tab:

```
bankstatus
```

To stop:

```
bankstop
```

## What the panels do

### Sidebar — Audio + MIDI setup

Per-instrument settings. Picks up the audio device, input channels (mono or stereo pair), MIDI port, MIDI channel, and polyphony from the instrument's `manifest.json`. Save here when you change something — it persists.

**Polyphony hint:** if the instrument is marked `monophonic`, the UI surfaces a warning about "chord" mode (mono synths can only play one note at a time). Pick "root only" for bass lines or "arp" for script-side arpeggios.

### Sidebar — MIDI Clock → synth

Always-on. Green LED = synth is locked to our clock. Auto-starts on page load, auto-restarts when you change BPM or pick a different MIDI port.

- **BPM input** — drag/type. Updates the clock + click in real time, no glitch.
- **⏮ 1 button** — Ableton-style transport reset. Sends MIDI Stop → Start so the synth's arp/sequencer snaps to the downbeat. Hit this whenever the arp drifts out of phase.
- **🔔 Click** — browser-side metronome. Goes to Mac speakers, NOT through the TX-6 (won't bleed into captures). Subdivision picker: 1/4, 1/8, 1/16, 1/32, 1/8T, 1/16T. Volume slider.

### Capture styles

Tabs across the top: **progression / multisample / hihat / sweep / drums**.

#### progression
Records a chord progression as a loop. Pick from the saved library or type your own chords.

- **Loop length** — 4 bars / 8 bars / 16 bars. The "bars per chord" gets computed automatically from your chord count.
- **Pattern picker** — saved progressions in `patterns/progressions/`. Picking one auto-sets BPM, chords, loop length.
- **Loop length readout** — shows total bars/beats + warns if the math doesn't divide cleanly.
- **🎹 Preview** — sends MIDI to the actual synth, no recording. Resets the click + the synth transport so chord 1 lands on the click's "1".
- **send_mode** — chord (all notes at once, needs poly synth OR synth's own arp engaged) / root (just the root note, mono bass mode) / arp (script-side arpeggio at 1/16 notes).

#### multisample
Chromatic capture across a note range. Records each note for `sustain` seconds with a `tail` for the release.

#### hihat (suite)
5-section pattern designed for hi-hat samples — steady at multiple subdivisions + a random fill. The synth stays latched between captures.

#### drums
Sends a drum pattern from `patterns/drums/` via MIDI. Useful for SP-808-style drum captures.

### Playback panel

Captures arrive here already trimmed + opening hot (auto-cleaned at capture time;
loudness is matched across the whole pack at build). From here you can:
- ✓ **Keep + Save Notes** — write the WAV path + notes to a sidecar `.json`
- 🔄 **Retake** — fire the same capture again
- ✗ **Discard** — delete the WAV + sidecar

### This session's captures

Running log of every WAV recorded this session. Three states:

- **◐ pending** (amber) — fresh, decide via Keep/Discard
- **✓ kept** (green) — locked in
- **✗ discarded** (faint) — gone

Click the ✗ button on any pending row to delete it inline — no need to scroll up to Playback.

**Session report** at the top shows: total captures, kept/pending/discarded breakdown, BPMs used, top 5 patches by capture count, session duration.

## Common gotchas

### Red LED on the clock
Server doesn't have the clock endpoint, OR the MIDI port isn't found.

- Run `bankstatus`. If it says "OLD code" → restart with `bankserver`
- Check the MIDI port dropdown actually has "Moog Grandmother" (or your synth) selected
- If the synth was unplugged after server start, the port is gone — replug + restart server

### Captures are silent ("peak: -inf dBFS")
macOS microphone permission. The server needs mic permission to record from the audio interface.

- Make sure the server is launched from a **Terminal tab that has mic permission**. Terminal usually does; Claude Code's subprocess doesn't unless you grant it explicitly.
- If you launched the server from Claude Code, kill it and re-launch with `bankserver` in Terminal.

### Audition keeps playing after closing the browser
Three safety nets are now in place:
1. Closing the tab fires a stop beacon (auto-cleanup)
2. If the beacon misses, server auto-stops after 60s of no client pings
3. Emergency: from any Terminal, run `bankstopaudio`

### Stuck note on the synth
```
gmpanic
```
Sends MIDI All Notes Off + All Sound Off on every channel.

### Capture loops drift against the click
Run a jitter test:
```
gmjitter            # 15 sec @ 120 BPM, doesn't send MIDI
gmjitter 88 30      # 30 sec @ 88 BPM
gmjitter 120 60 send  # 60 sec @ 120 BPM, actually sends MIDI to Grandmother
```
Verdict comes out as ROCK SOLID / GOOD / JITTERY with stats. On a Mac mini in normal load you should see "ROCK SOLID" — stdev under 1ms, drift under 0.01%.

## File layout this dashboard touches

```
spacepit-sample-bank/
├── instruments/<slug>/
│   ├── manifest.json              ← per-instrument settings, patches list, polyphony
│   ├── patches/<patch>/raw/       ← multisample WAVs
│   ├── patches/<patch>/spring/    ← processed chain captures
│   ├── loops/raw/                 ← progression + hi-hat suite WAVs
│   └── *.json                     ← keep-notes sidecars next to each WAV
├── patterns/
│   ├── progressions/*.json        ← saved chord progressions (35 in library)
│   └── drums/*.json               ← drum patterns
├── tools/
│   ├── capture/                   ← record scripts (called by the dashboard)
│   ├── dashboard/                 ← THIS — Flask server + web UI
│   ├── patterns/                  ← chord/progression generator
│   └── pack/                      ← build-pack pipeline, audit, demo audio
└── releases/                      ← built packs ready to ship
```

## Companion zsh shortcuts

All defined in `~/.zshrc`. Available from any Terminal tab.

| Command | Does |
|---|---|
| `bankserver` | Start dashboard server in current Terminal (Ctrl+C to stop) |
| `bankstatus` | Show whether server is running + which tab + endpoint version |
| `bankstop` | Kill any running server |
| `bankstopaudio` | Kill running audition + MIDI panic (use if browser is closed and synth still plays) |
| `gmpanic` | Send MIDI panic to clear stuck notes on the Grandmother |
| `gmjitter [bpm] [dur] [send]` | Measure MIDI clock jitter |
| `bank` | `cd` into the sample-bank repo |

## the studio dreamer  (`/dreamer`)

Generative band that plays your physical studio over MIDI. Assign players
(chords / bass / drums / lead) to synth ports + channels, pick a vibe, hit
**PLAY THE ROOM**. `localhost:8001/dreamer`.

**Shipped (v0):**
- 7 vibes — meditation, ambient, lofi, hiphop (5-chord boom-bap), house, party, trap
- free-text vibe box — type a feeling ("rainy study", "dark night 808") → closest mood
- per-player synth/channel assignment; generative 16th-step engine (swing + probabilities)
- time-of-day auto-schedule scaffold (6am meditation → 9am lofi → 1pm hiphop → 6pm house → 10pm party → ambient overnight). OFF by default.

**Next:**
- per-player density / energy knobs (busier or sparser, on the fly)
- pull chord voicings + patterns from the captured patch bank + pattern library
- richer drum routing per machine (KO/EP-133 pad maps, not just GM notes)
- editable schedule UI + sunrise/sunset triggers
- record the dreamer's output back to audio
- whole-studio: every synth firing at once

**API:** `POST /api/dreamer/start {vibe,key,tempo,roles}` · `POST /api/dreamer/stop` · `GET /api/dreamer/status` · `GET|POST /api/dreamer/schedule`
