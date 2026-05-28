#!/usr/bin/env python3
"""List MIDI output ports + audio input devices on this machine.

Use this to find the exact names to pass via --midi-port and --audio-device.
"""
import sys

try:
    import mido
    import sounddevice as sd
except ImportError as e:
    print(f"missing dep: {e}\n  run:  uv sync   (in tools/capture/)", file=sys.stderr)
    sys.exit(1)

print("=== MIDI output ports ===")
ports = mido.get_output_names()
if ports:
    for i, p in enumerate(ports):
        print(f"  [{i}] {p}")
else:
    print("  (none — plug in your synth or MIDI interface)")

print("\n=== Audio input devices ===")
devs = sd.query_devices()
in_devs = [(i, d) for i, d in enumerate(devs) if d["max_input_channels"] > 0]
if in_devs:
    for i, d in in_devs:
        print(f"  [{i}] {d['name']}  ({d['max_input_channels']} in @ {d['default_samplerate']:.0f}Hz)")
else:
    print("  (none — check audio interface connection)")

try:
    default_in = sd.query_devices(kind="input")
    print(f"\n  default input: {default_in['name']}")
except Exception:
    pass
