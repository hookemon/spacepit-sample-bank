#!/usr/bin/env python3
"""Send a saved drum pattern to a drum machine + record the audio output.

Pattern format: patterns/drums/<name>.json with tracks (voice → tick positions in 24 PPQN).

Drum machine voice→MIDI-note mapping comes from one of:
  - The instrument's manifest.json `voice_map` field (custom drum machines)
  - The default General MIDI drum map (fallback for GM-compatible gear)

Usage:
  record-drums.py --instrument tr-808 --pattern four-on-floor --bars 4 \\
    --midi-port "TR-808" --midi-channel 10 --audio-device "TX-6"
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

try:
    import mido
    import numpy as np
    import sounddevice as sd
    import soundfile as sf
except ImportError as e:
    print(f"missing dep: {e}", file=sys.stderr)
    sys.exit(1)


SUBTYPE = {16: "PCM_16", 24: "PCM_24", 32: "FLOAT"}

# General MIDI drum map (channel 10, 0-based = channel 9)
GM_DRUM_MAP = {
    "kick":       36,
    "snare":      38,
    "rim":        37,
    "clap":       39,
    "tom-lo":     41,
    "hh-closed":  42,
    "tom-mid":    45,
    "hh-open":    46,
    "tom-hi":     48,
    "crash":      49,
    "ride":       51,
    "cowbell":    56,
    "clave":      75,
    "conga":      62,
    "shaker":     70,
}


def find_bank_root() -> Path | None:
    p = Path.cwd().resolve()
    for c in [p, *p.parents]:
        if (c / "instruments").is_dir() and (c / "manifest.template.json").exists():
            return c
    fallback = Path.home() / "projects" / "spacepit-sample-bank"
    if (fallback / "instruments").is_dir():
        return fallback
    return None


def resolve_audio_device(arg: str) -> int:
    devs = sd.query_devices()
    matches = [(i, d) for i, d in enumerate(devs) if arg.lower() in d["name"].lower() and d["max_input_channels"] > 0]
    if not matches:
        print(f"no audio device matches '{arg}'", file=sys.stderr); sys.exit(1)
    return matches[0][0]


def resolve_midi_port(arg: str) -> str:
    ports = mido.get_output_names()
    matches = [p for p in ports if arg.lower() in p.lower()]
    if not matches:
        print(f"no MIDI port matches '{arg}'", file=sys.stderr); sys.exit(1)
    return matches[0]


def load_pattern(bank_root: Path, name: str) -> dict:
    path = bank_root / "patterns" / "drums" / f"{name}.json"
    if not path.exists():
        print(f"drum pattern not found: {path}", file=sys.stderr); sys.exit(1)
    return json.loads(path.read_text())


def build_event_list(pattern: dict, voice_map: dict, bars_to_play: int) -> list[tuple[float, int, int, int]]:
    """Build a list of (tick_offset, midi_note, velocity, channel_offset) events for the full duration."""
    ppqn = pattern.get("ppqn", 24)
    pattern_bars = pattern.get("bars", 1)
    meter = 4  # 4/4 default
    ticks_per_bar = ppqn * meter
    events = []
    for loop_i in range(bars_to_play // pattern_bars + 1):
        loop_offset = loop_i * pattern_bars * ticks_per_bar
        for voice, positions in pattern.get("tracks", {}).items():
            note = voice_map.get(voice, GM_DRUM_MAP.get(voice))
            if note is None:
                continue
            for pos in positions:
                if loop_offset + pos < bars_to_play * ticks_per_bar:
                    events.append((loop_offset + pos, note, 100, 0))
    events.sort()
    return events


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instrument", required=True, help="drum machine slug (e.g. tr-808)")
    ap.add_argument("--pattern", required=True, help="drum pattern name from patterns/drums/")
    ap.add_argument("--bars", type=int, default=4, help="how many bars to play (the pattern loops)")
    ap.add_argument("--bpm", type=float, default=None, help="override the pattern's tempo")
    ap.add_argument("--midi-port", required=True)
    ap.add_argument("--midi-channel", type=int, default=10, help="drum standard = channel 10")
    ap.add_argument("--audio-device", required=True)
    ap.add_argument("--input-channels", default="1,2")
    ap.add_argument("--sample-rate", type=int, default=48000)
    ap.add_argument("--bit-depth", type=int, choices=[16, 24, 32], default=24)
    ap.add_argument("--tail-sec", type=float, default=1.5)
    ap.add_argument("--note-duration-ms", type=float, default=50.0, help="how long each MIDI note is held")
    ap.add_argument("--bank-root", default=None)
    ap.add_argument("-y", "--yes", action="store_true")
    args = ap.parse_args()

    try:
        channels = [int(c) for c in args.input_channels.split(",")]
    except ValueError:
        print("bad --input-channels", file=sys.stderr); sys.exit(1)

    bank_root = Path(args.bank_root).resolve() if args.bank_root else find_bank_root()
    if not bank_root:
        print("can't find bank root", file=sys.stderr); sys.exit(1)

    pattern = load_pattern(bank_root, args.pattern)
    bpm = args.bpm if args.bpm else pattern.get("bpm", 120)

    # Get the drum machine's voice_map (or use GM)
    voice_map = {}
    instr_manifest_path = bank_root / "instruments" / args.instrument / "manifest.json"
    if instr_manifest_path.exists():
        try:
            voice_map = json.loads(instr_manifest_path.read_text()).get("voice_map") or {}
        except Exception:
            pass

    # build event list
    events = build_event_list(pattern, voice_map, args.bars)
    if not events:
        print(f"no events to play. check voice_map for {args.instrument} or the pattern's voices", file=sys.stderr)
        sys.exit(1)

    # timing
    ppqn = pattern.get("ppqn", 24)
    sec_per_tick = 60.0 / bpm / ppqn
    meter = 4
    duration_sec = args.bars * meter * (60.0 / bpm) + args.tail_sec

    # output path
    out_dir = bank_root / "instruments" / args.instrument / "loops" / "raw"
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{args.pattern}_{int(bpm)}bpm_{args.bars}bars.wav"
    out_path = out_dir / fname

    print("=== drum pattern record ===")
    print(f"  instrument:  {args.instrument}")
    print(f"  pattern:     {args.pattern}  ({pattern.get('vibe', '')})")
    print(f"  bpm:         {bpm}")
    print(f"  bars:        {args.bars}")
    print(f"  events:      {len(events)} MIDI note-ons")
    print(f"  voices:      {list(pattern.get('tracks', {}).keys())}")
    print(f"  duration:    {duration_sec:.1f}s")
    print(f"  output:      {out_path}")
    print()

    if not args.yes:
        print("→ press enter to start, ctrl-c to abort")
        try:
            input()
        except KeyboardInterrupt:
            return

    audio_dev = resolve_audio_device(args.audio_device)
    midi_port_name = resolve_midi_port(args.midi_port)

    # arm audio
    n_samples = int(duration_sec * args.sample_rate)
    rec = sd.rec(n_samples, samplerate=args.sample_rate, channels=len(channels),
                 device=audio_dev, mapping=channels, dtype="float32", blocking=False)

    # open midi
    port = mido.open_output(midi_port_name)
    ch = args.midi_channel - 1
    note_dur = args.note_duration_ms / 1000.0
    try:
        t0 = time.time()
        active_notes = []  # (off_time, note)
        for tick_offset, note, velocity, _ in events:
            target_t = t0 + tick_offset * sec_per_tick
            # send note-offs for any past notes
            now = time.time()
            still_active = []
            for off_t, n in active_notes:
                if off_t <= now:
                    port.send(mido.Message('note_off', note=n, velocity=0, channel=ch))
                else:
                    still_active.append((off_t, n))
            active_notes = still_active
            # wait for target
            wait = target_t - time.time()
            if wait > 0:
                time.sleep(wait)
            # fire
            port.send(mido.Message('note_on', note=note, velocity=velocity, channel=ch))
            active_notes.append((time.time() + note_dur, note))

        # release lingering notes
        time.sleep(note_dur)
        for off_t, n in active_notes:
            port.send(mido.Message('note_off', note=n, velocity=0, channel=ch))

        sd.wait()
    finally:
        port.send(mido.Message('control_change', control=123, value=0, channel=ch))
        port.close()

    sf.write(str(out_path), rec, args.sample_rate, subtype=SUBTYPE[args.bit_depth])
    peak = float(np.max(np.abs(rec)))
    peak_db = 20 * np.log10(max(1e-10, peak))
    print(f"\n✓ saved {fname}  ({duration_sec:.1f}s, peak {peak_db:+.1f} dBFS)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\naborted.")
