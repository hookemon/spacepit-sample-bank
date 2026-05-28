# capture tools

Two tools, one job each:

- **`capture-synth.py`** — chromatic multi-sampling for hardware synths. Sends MIDI notes, records each one across the chosen velocity layers + round-robins, writes WAVs into `instruments/<slug>/patches/<patch>/raw/`.
- **`capture-drum.py`** — drum-machine sampling. Walks a per-tier voice matrix (kick × N, snare × N, hh × N, …) and records each variation. Manual mode (you hit the pad) or MIDI mode (tool sends notes). Writes into `instruments/<slug>/kits/<kit>/raw/`.

Plus `list-devices.py` — lists MIDI ports + audio inputs so you know what to pass.

---

## install (one time)

```bash
# install uv if you don't have it
curl -LsSf https://astral.sh/uv/install.sh | sh

# from this folder
cd projects/spacepit-sample-bank/tools/capture
uv sync
```

Or with pip:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

(All examples assume `uv run`. With pip, drop the `uv run` and activate `.venv` first.)

---

## hardware setup (same for both tools)

1. **MIDI** (only needed for MIDI mode): drum machine / synth's MIDI IN → Mac (USB MIDI directly, or via MIDI interface — Kenton Pro Solo, MPU-101, Oplab on hand)
2. **Audio**: instrument out → audio interface input. Stereo for synths, mono usually fine for drum machines.
3. **Monitoring**: route the interface so you can hear the source during capture (direct monitoring on the interface, or Loopback). The capture tools record only — they don't monitor.
4. **Source prep**: load the patch / kit. Mute LFOs / arpeggiator / external mod sources you don't want printed. Set output volume so loud notes peak around −6 dBFS at the interface.

---

## step 1: confirm device names

```bash
uv run list-devices.py
```

Copy the exact spelling of your MIDI port and audio device. Both tools accept substring matches too.

---

## step 2: check the input level

```bash
uv run capture-synth.py --meter --audio-device "Apogee" --input-channels 1,2 --meter-duration 5
```

Play loud during the 5s window. Aim for `−6 to −3 dBFS`.

(Available on the synth tool but applies to either workflow — same audio path.)

---

# capture-synth.py — chromatic multi-sampling

For: MS-20, ARP 2600, Mono/Poly, Poly-800, MicroFreak, OP-1/Z/XY, Triton, MiniNova, Peak, Circuit Tracks, Mono Station, TT-303, Wurlitzer 200, etc.

## dry-run the plan

```bash
uv run capture-synth.py --instrument ms-20 --patch lead01 --dry-run
```

Reads `instruments/ms-20/manifest.json` for the tier. Gold tier ≈ 730 files, ~75 min unattended.

## real capture

```bash
uv run capture-synth.py \
  --instrument ms-20 --patch lead01 \
  --midi-port "Korg MS-20" --midi-channel 1 \
  --audio-device "Apogee" --input-channels 1,2
```

Press enter when prompted. Output: `instruments/ms-20/patches/lead01/raw/ms20_lead01_c4_v100_rr1.wav` and friends.

## key flags

- `--tier {quick|standard|gold}` override manifest
- `--note-range C2-C6`, `--step 2`, `--velocities 50,90,127`, `--round-robins 1`
- `--sustain-sec 6 --tail-sec 8` for long-release pads
- `--resume` skips files that already exist
- `--octave-offset -1` for Ableton-style display (middle C = C3)

See full naming + tier specs in the bank's main README.

---

# capture-drum.py — drum-machine sampling

For: TR-808/909/606/707/727, LinnDrum, SP-1200, MPC60II, MPC 2500, Digitakt, EP-133, K.O. II, SP-404MKII, Maschine+, DDD-1, DR-550, Drum Station, plus anything else with pads / drum voices.

## the matrix

Each tier defines a list of `(voice, variant-count)` pairs:

| tier | example matrix | total hits | est. time |
|---|---|---|---|
| quick | kick × 3, snare × 3, hh-closed, hh-open, clap, ride, crash, toms (lo/mid/hi) | 14 | ~5 min |
| standard | each voice × 3-5 + rim, cowbell, clave | ~45 | ~30 min |
| gold | each voice × 6-10 + congas + variations + ringouts | ~100+ | half day |

Filenames come out as `<slug-flat>_<voice>_<letter>.wav` — `sp1200_kick_a.wav`, `sp1200_kick_b.wav`, etc. Variants use letters a-z.

## manual mode (default — works for any drum machine)

```bash
uv run capture-drum.py --instrument sp-1200 --kit factory --tier standard
```

Tool walks the matrix, prompts you before each capture:

```
[ 1/ 45] kick 1/5 → sp1200_kick_a.wav  (2.5s window)
   enter=record · s=skip · r=redo last · b=back · q=quit:
```

Press enter → recording starts → hit the pad during the window → tool saves → moves to next.

Per-voice record windows are tuned by default: kick = 2.5s, crash = 10s, hh-open = 6s. Override globally with `--record-sec 4`.

## midi mode (for drum machines that respond to MIDI)

```bash
uv run capture-drum.py \
  --instrument tr-808 --kit factory --tier standard \
  --mode midi \
  --midi-port "TR-808" --midi-channel 10 \
  --audio-device "Apogee"
```

Tool sends MIDI notes per the General MIDI drum map. Velocity is swept across variants by default (variant 1 = vel 40, variant N = vel 127). Override with `--velocity 100` for fixed velocity.

Channel 10 is the standard drum channel. Override with `--midi-channel`.

## per-instrument voice map

For samplers like SP-1200 where pad-to-MIDI mapping is whatever you set up, override the GM map by adding `voice_map` to the instrument's `manifest.json`:

```json
{
  ...
  "voice_map": {
    "kick": 60,
    "snare": 62,
    "hh-closed": 64,
    "hh-open": 65,
    "tom-lo": 67
  }
}
```

The tool merges this on top of the GM defaults — only override the voices that differ.

## key flags

- `--tier {quick|standard|gold}` override manifest
- `--voice-list "kick:5,snare:5,hh-closed:3"` custom matrix
- `--mode {manual|midi}` default manual
- `--record-sec 4` default record window; per-voice overrides built in
- `--velocity 100` fixed velocity for MIDI mode (default sweeps across variants)
- `--resume` skip existing
- `--dry-run` preview the plan

---

## resume + redo

Both tools support `--resume` — skip any WAV that already exists. Means:

- If a run gets stopped, re-run with `--resume` to pick up where you left off
- If you want to redo specific captures, delete those WAVs and run with `--resume`
- The drum tool also has live `r` (redo last) and `b` (back) controls during manual mode

---

## naming reference

**Synth multisample**: `<slug-flat>_<patch>_<note>_v<vel>_rr<rr>.wav`
- `ms20_lead01_c4_v100_rr1.wav`
- `ms20_lead01_cs4_v100_rr1.wav` (sharps as `s`)

**Drum one-shot**: `<slug-flat>_<voice>_<letter>.wav`
- `sp1200_kick_a.wav`, `sp1200_snare_b.wav`, `tr808_hh-closed_c.wav`

`slug-flat` = instrument slug with hyphens removed (`ms-20` → `ms20`, `sp-1200` → `sp1200`).

---

## what these tools DON'T do (yet)

- **Re-amp through chains** (raw → processed). That's a separate pass — re-route raw files through the chain in Ableton, save to `processed/`.
- **Build Move kits / MPC pgms / SFZ exports**. That's the next tool — the Move kit exporter — when needed.
- **Loops**. Loops are performed live and saved manually for now (different workflow per gear).
- **FX pedals / outboard**. FX capture is its own thing — different signal flow, send-side gain, etc.
