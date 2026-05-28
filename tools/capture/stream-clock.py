#!/usr/bin/env python3
"""Stream continuous MIDI clock to a synth's MIDI input.

Keeps the synth's arpeggiator / LFO / sequencer locked to a known tempo
across multiple capture sessions. Run in its own terminal tab; ctrl-C to stop.

Usage:
  stream-clock.py --midi-port "Moog Grandmother" --bpm 120
"""
from __future__ import annotations

import argparse
import sys
import time

try:
    import mido
except ImportError as e:
    print(f"missing dep: {e}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--midi-port", default="Moog Grandmother", help="MIDI port name substring")
    ap.add_argument("--bpm", type=float, default=120.0)
    ap.add_argument("--no-start", action="store_true", help="don't send START message — only clock ticks (use if synth is already running)")
    args = ap.parse_args()

    ports = mido.get_output_names()
    matches = [p for p in ports if args.midi_port.lower() in p.lower()]
    if not matches:
        print(f"no MIDI port matches '{args.midi_port}'. available: {ports}", file=sys.stderr)
        sys.exit(1)
    port_name = matches[0]

    interval = 60.0 / args.bpm / 24.0
    print(f"streaming clock to {port_name} @ {args.bpm} BPM (24 PPQN)")
    print(f"  tick interval: {interval*1000:.2f} ms")
    print(f"  ctrl-C to stop")
    print()

    port = mido.open_output(port_name)
    try:
        if not args.no_start:
            port.send(mido.Message('start'))
            print("→ sent START")

        clock_msg = mido.Message('clock')
        t_start = time.time()
        i = 0
        last_report = t_start

        while True:
            target = t_start + i * interval
            now = time.time()
            sleep_time = target - now
            if sleep_time > 0:
                time.sleep(sleep_time)
            port.send(clock_msg)
            i += 1
            # status print every 4 seconds
            if now - last_report >= 4.0:
                beats = i / 24
                print(f"  streaming…  {i} ticks  ·  {beats:.1f} beats  ·  {beats/4:.1f} bars")
                last_report = now

    except KeyboardInterrupt:
        print("\n→ sending STOP")
        port.send(mido.Message('stop'))
        print(f"stopped after {i} ticks  ·  {i/24/4:.1f} bars")
    finally:
        port.close()


if __name__ == "__main__":
    main()
