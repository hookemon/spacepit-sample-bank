#!/usr/bin/env python3
"""Record a performance from a hardware synth.

For loops, sweeps, swells, one-shots — anything where you PERFORM live and we just
capture audio. No MIDI note sequencing (unlike capture-synth.py).

Optional: send MIDI clock to sync the synth's arpeggiator / LFO to a known tempo.

Usage:
  # 8-bar loop at 120 BPM, sync arpeggiator via MIDI clock
  record-performance.py --instrument grandmother --kind loop --name moogbass-arp \\
    --bpm 120 --bars 8 --send-clock --midi-port "Moog Grandmother"

  # 5-second filter sweep one-shot (no clock)
  record-performance.py --instrument grandmother --kind sweep --name filter-down \\
    --duration 5

Files land in:
  instruments/<slug>/loops/raw/<name>_<bpm>bpm_<bars>bars.wav    (loop kind)
  instruments/<slug>/sweeps/raw/<name>_<duration>s.wav            (sweep kind)
  instruments/<slug>/oneshots/raw/<name>.wav                      (oneshot kind)
"""
from __future__ import annotations

import argparse
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


def find_bank_root(start: Path | None = None) -> Path | None:
    p = (start or Path.cwd()).resolve()
    for candidate in [p, *p.parents]:
        if (candidate / "instruments").is_dir() and (candidate / "manifest.template.json").exists():
            return candidate
    fallback = Path.home() / "projects" / "spacepit-sample-bank"
    if (fallback / "instruments").is_dir():
        return fallback
    return None


def resolve_audio_device(arg: str | None) -> int:
    devs = sd.query_devices()
    matches = [(i, d) for i, d in enumerate(devs) if (arg or "").lower() in d["name"].lower() and d["max_input_channels"] > 0]
    if not matches:
        print(f"no audio device matches '{arg}'", file=sys.stderr)
        sys.exit(1)
    return matches[0][0]


def resolve_midi_port(arg: str | None) -> str | None:
    if not arg:
        return None
    ports = mido.get_output_names()
    matches = [p for p in ports if arg.lower() in p.lower()]
    if not matches:
        print(f"no MIDI port matches '{arg}'", file=sys.stderr)
        sys.exit(1)
    return matches[0]


def send_clock(midi_port_name: str, bpm: float, duration_sec: float) -> None:
    """Send MIDI clock to sync the synth's arp/LFO. 24 ticks per quarter note."""
    port = mido.open_output(midi_port_name)
    try:
        port.send(mido.Message('start'))
        clock_interval = 60.0 / bpm / 24.0
        t_start = time.time()
        i = 0
        while time.time() - t_start < duration_sec:
            target = t_start + i * clock_interval
            now = time.time()
            sleep_time = target - now
            if sleep_time > 0:
                time.sleep(sleep_time)
            port.send(mido.Message('clock'))
            i += 1
        port.send(mido.Message('stop'))
    finally:
        port.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instrument", required=True, help="instrument slug (e.g. grandmother)")
    ap.add_argument("--name", required=True, help="loop/sweep/oneshot name")
    ap.add_argument("--kind", choices=["loop", "sweep", "oneshot"], default="loop")

    # tempo / duration
    ap.add_argument("--bpm", type=float, default=120.0)
    ap.add_argument("--bars", type=float, default=8, help="bars (loop kind)")
    ap.add_argument("--meter", type=int, default=4, help="beats per bar")
    ap.add_argument("--duration", type=float, default=None, help="seconds (overrides bars for sweep/oneshot)")
    ap.add_argument("--tail-sec", type=float, default=0.5, help="extra time after the bars finish")
    ap.add_argument("--pre-roll", type=float, default=0.0, help="seconds of silence at start (for headroom before arp engages)")

    # audio
    ap.add_argument("--audio-device", default="TX-6")
    ap.add_argument("--input-channels", default="11,12")
    ap.add_argument("--sample-rate", type=int, default=48000)
    ap.add_argument("--bit-depth", type=int, choices=[16, 24, 32], default=24)

    # midi clock (optional)
    ap.add_argument("--send-clock", action="store_true", help="send MIDI clock to sync arpeggiator/LFO")
    ap.add_argument("--midi-port", default="Moog Grandmother")

    # bank
    ap.add_argument("--bank-root", default=None)
    ap.add_argument("-y", "--yes", action="store_true")

    args = ap.parse_args()

    # input channels
    try:
        channels = [int(c) for c in args.input_channels.split(",")]
    except ValueError:
        print(f"bad --input-channels: {args.input_channels}", file=sys.stderr)
        sys.exit(1)

    bank_root = Path(args.bank_root).resolve() if args.bank_root else find_bank_root()
    if not bank_root:
        print("can't find bank root", file=sys.stderr)
        sys.exit(1)

    # compute duration + output path
    if args.kind == "loop":
        sec_per_bar = (60.0 / args.bpm) * args.meter
        duration = args.bars * sec_per_bar
        out_dir = bank_root / "instruments" / args.instrument / "loops" / "raw"
        fname = f"{args.name}_{int(args.bpm)}bpm_{args.bars:g}bars.wav"
    elif args.kind == "sweep":
        duration = args.duration if args.duration else 5.0
        out_dir = bank_root / "instruments" / args.instrument / "sweeps" / "raw"
        fname = f"{args.name}_{duration:.1f}s.wav"
    else:  # oneshot
        duration = args.duration if args.duration else 3.0
        out_dir = bank_root / "instruments" / args.instrument / "oneshots" / "raw"
        fname = f"{args.name}.wav"

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / fname

    total_sec = args.pre_roll + duration + args.tail_sec
    audio_dev = resolve_audio_device(args.audio_device)
    midi_port_name = resolve_midi_port(args.midi_port) if args.send_clock else None

    print("=== performance record ===")
    print(f"  instrument:  {args.instrument}")
    print(f"  kind:        {args.kind}")
    print(f"  name:        {args.name}")
    if args.kind == "loop":
        print(f"  bpm:         {args.bpm}")
        print(f"  bars:        {args.bars} ({args.meter}/{args.meter} time)")
    print(f"  duration:    {duration:.2f}s (pre-roll {args.pre_roll}s + body + tail {args.tail_sec}s = {total_sec:.2f}s)")
    print(f"  audio:       {sd.query_devices(audio_dev)['name']}  ch {channels}  @ {args.sample_rate}Hz / {args.bit_depth}-bit")
    if midi_port_name:
        print(f"  midi clock:  {midi_port_name}  (BPM {args.bpm})")
    print(f"  output:      {out_path}")
    print()

    if not args.yes:
        print("→ press enter to start, ctrl-c to abort")
        try:
            input()
        except KeyboardInterrupt:
            return

    # arm audio
    n_samples = int(total_sec * args.sample_rate)
    rec = sd.rec(n_samples, samplerate=args.sample_rate, channels=len(channels),
                 device=audio_dev, mapping=channels, dtype="float32", blocking=False)

    # pre-roll wait
    if args.pre_roll > 0:
        time.sleep(args.pre_roll)

    # midi clock during the body (run in main thread, blocks until duration ends)
    if midi_port_name:
        send_clock(midi_port_name, args.bpm, duration)
    else:
        time.sleep(duration)

    # tail
    time.sleep(args.tail_sec)
    sd.wait()

    # save
    subtype = SUBTYPE[args.bit_depth]
    sf.write(str(out_path), rec, args.sample_rate, subtype=subtype)

    peak = float(np.max(np.abs(rec)))
    peak_db = 20 * np.log10(max(1e-10, peak))
    print(f"\n✓ saved {fname}  ({total_sec:.1f}s, peak {peak_db:+.1f} dBFS)")
    if peak >= 0.99:
        print("  ⚠ clipping detected")
    elif peak_db < -50:
        print("  ⚠ very quiet — was the synth playing?")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\naborted.")
