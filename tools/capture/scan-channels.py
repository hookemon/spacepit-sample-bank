#!/usr/bin/env python3
"""Find which audio channel a MIDI-triggered synth is on.

Sends a single MIDI note, records ALL channels of the audio device for 5 sec,
reports per-channel peak so you know which --input-channels to pass to capture-synth.py.

Usage:
  scan-channels.py --device "TX-6" --midi-port "Moog Grandmother" --note a2
"""
import argparse
import sys
import time

import mido
import numpy as np
import sounddevice as sd


def note_name_to_midi(name: str) -> int:
    import re
    m = re.match(r"^([a-g])(s|#|b)?(-?\d+)$", name.lower())
    if not m:
        raise ValueError(f"bad note name: {name}")
    letter, accidental, octave = m.groups()
    base = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}[letter]
    if accidental in ("s", "#"):
        base += 1
    elif accidental == "b":
        base -= 1
    return base + (int(octave) + 1) * 12


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", required=True, help="audio input device name substring")
    ap.add_argument("--midi-port", required=True, help="MIDI output port name substring")
    ap.add_argument("--midi-channel", type=int, default=1)
    ap.add_argument("--note", default="a2")
    ap.add_argument("--velocity", type=int, default=100)
    ap.add_argument("--sample-rate", type=int, default=48000)
    ap.add_argument("--duration", type=float, default=5.0, help="recording window in seconds")
    ap.add_argument("--hold", type=float, default=3.0, help="note hold time")
    args = ap.parse_args()

    # find audio device
    devs = sd.query_devices()
    matches = [(i, d) for i, d in enumerate(devs) if args.device.lower() in d["name"].lower() and d["max_input_channels"] > 0]
    if not matches:
        print(f"no audio device matches '{args.device}'", file=sys.stderr)
        sys.exit(1)
    dev_idx, dev = matches[0]
    n_channels = dev["max_input_channels"]
    print(f"device:  {dev['name']}  ({n_channels} input channels)")

    # find midi port
    midi_ports = mido.get_output_names()
    midi_matches = [p for p in midi_ports if args.midi_port.lower() in p.lower()]
    if not midi_matches:
        print(f"no MIDI port matches '{args.midi_port}'", file=sys.stderr)
        sys.exit(1)
    midi_port_name = midi_matches[0]
    print(f"midi:    {midi_port_name}  (channel {args.midi_channel})")

    # note
    note = note_name_to_midi(args.note)
    print(f"note:    {args.note} (MIDI {note}) vel {args.velocity}")
    print(f"window:  {args.duration}s record, {args.hold}s note hold\n")

    # open midi
    midi = mido.open_output(midi_port_name)
    try:
        # arm recording — all channels, sequential 1..n
        mapping = list(range(1, n_channels + 1))
        n_samples = int(args.duration * args.sample_rate)
        print("recording all channels — sending note in 0.1s")
        rec = sd.rec(
            n_samples, samplerate=args.sample_rate, channels=n_channels,
            device=dev_idx, mapping=mapping, dtype="float32", blocking=False,
        )
        time.sleep(0.1)
        midi.send(mido.Message("note_on", note=note, velocity=args.velocity, channel=args.midi_channel - 1))
        time.sleep(args.hold)
        midi.send(mido.Message("note_off", note=note, velocity=0, channel=args.midi_channel - 1))
        sd.wait()
    finally:
        midi.send(mido.Message("control_change", control=123, value=0, channel=args.midi_channel - 1))
        midi.close()

    # per-channel peaks
    print("\nper-channel peak (loudest 3 marked):")
    peaks = []
    for i in range(n_channels):
        peak = float(np.max(np.abs(rec[:, i])))
        if peak == 0:
            db = float("-inf")
        else:
            db = 20 * np.log10(peak)
        peaks.append((i + 1, db))

    # sort by db desc to find loudest
    sorted_peaks = sorted(peaks, key=lambda x: x[1], reverse=True)
    top3 = {ch for ch, _ in sorted_peaks[:3]}

    for ch, db in peaks:
        marker = ""
        if db > -40:
            marker = "  ← HOT" if ch in top3 else "  ← signal"
        elif ch in top3:
            marker = "  (loudest of the silent set)"
        db_str = "-inf  " if db == float("-inf") else f"{db:+6.1f}"
        print(f"  ch {ch:2d}: {db_str} dBFS{marker}")

    # recommend
    hot = [ch for ch, db in peaks if db > -40]
    print()
    if hot:
        print(f"→ active channel(s): {hot}")
        cmd = ",".join(str(c) for c in hot)
        print(f"  re-run capture with:  --input-channels {cmd}")
    else:
        print("→ no channels hot. troubleshoot:")
        print("  - is the synth's audio cable in the TX-6?")
        print("  - is the TX-6 channel's volume up + unmuted?")
        print("  - did you hear the note in your headphones during this scan?")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\naborted.")
