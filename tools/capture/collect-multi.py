#!/usr/bin/env python3
"""Multi-part collector — "chord hop, for collecting."

Fire ONE progression across multiple synths on different MIDI ports/channels, capture EVERY synth's
audio at the same time (one input range per synth on a multi-in interface like the TX-6), and split
the recording into a named, foldered SET of per-synth stems — chords.wav, bass.wav, … — all the same
idea, knowing they belong together.

The OUT half (multichannel MIDI) is proven; this adds the COLLECT half: simultaneous multi-input
capture + per-synth split + naming/foldering.

  collect-multi.py --audio-device TX-6 --progression "Cm Ab Eb Bb" --bpm 120 --bars-per-chord 2 \
      --part "chords:mio:1:11,12" --part "bass:Moog Grandmother:all:3,4" --out ~/Desktop/collected

  --part  = name:midi_port:midi_channel:input_channels
            • midi_channel is 1-16, or "all" (sends on every channel — handy when a synth's receive
              channel is unknown, like the Grandmother on its own port).
            • input_channels are the interface's 1-based ins that synth lands on (a stereo pair or mono).
            • role is inferred from the name: contains "bass"/"sub" → root notes; else → full triads.

NOTE: the capture device (TX-6) must be FREE — if Ableton or another app is holding it, the inputs
record SILENCE. Monitor through the interface's hardware out. The per-stem peak check below warns you
if a stem came back silent (the classic device-contention tell).
"""
import argparse
import importlib.util
import re
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf
import mido

# reuse the proven chord-name parser from record-progression.py
_spec = importlib.util.spec_from_file_location("rp", str(Path(__file__).parent / "record-progression.py"))
rp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rp)


def resolve_device(name):
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and name.lower() in d["name"].lower():
            return i, d
    raise SystemExit(f"✗ no input device matches {name!r}")


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def parse_part(spec):
    name, port, ch, ins = spec.split(":", 3)
    channels = list(range(16)) if ch.strip().lower() == "all" else [int(ch) - 1]
    return {
        "name": name,
        "port": port,
        "channels": channels,
        "ins": [int(x) for x in ins.split(",")],
        "role": "bass" if re.search(r"bass|sub", name, re.I) else "chords",
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--audio-device", required=True)
    ap.add_argument("--progression", default="Cm Ab Eb Bb")
    ap.add_argument("--bpm", type=float, default=120)
    ap.add_argument("--bars-per-chord", type=int, default=2)
    ap.add_argument("--key", default="")
    ap.add_argument("--part", action="append", required=True, help="name:midi_port:midi_channel:input_channels")
    ap.add_argument("--out", default=str(Path.home() / "Desktop" / "collected"))
    ap.add_argument("--sample-rate", type=int, default=48000)
    ap.add_argument("--tail-sec", type=float, default=1.0)
    a = ap.parse_args()

    parts = [parse_part(s) for s in a.part]
    prog = rp.parse_progression(a.progression, root_octave=3)
    chord_sec = a.bars_per_chord * 4 * (60.0 / a.bpm)   # 4 beats per bar
    total_sec = len(prog) * chord_sec + a.tail_sec
    sr = a.sample_rate
    n = int(total_sec * sr)

    dev_idx, dev = resolve_device(a.audio_device)
    mapping = sorted({c for p in parts for c in p["ins"]})   # union of inputs, recorded together
    ports = {name: mido.open_output(name) for name in {p["port"] for p in parts}}

    def all_off():
        for op in ports.values():
            for ch in range(16):
                try:
                    op.send(mido.Message("control_change", control=123, value=0, channel=ch))
                except Exception:
                    pass

    print(f"● recording {total_sec:.1f}s from {dev['name']} ch {mapping}  ·  firing '{a.progression}' @ {a.bpm} BPM")
    rec = sd.rec(n, samplerate=sr, channels=len(mapping), device=dev_idx, mapping=mapping,
                 dtype="float32", blocking=False)
    try:
        all_off()
        for name, notes in prog:
            triad, root = notes, notes[0] - 12
            for p in parts:
                send = [root] if p["role"] == "bass" else triad
                for ch in p["channels"]:
                    for nn in send:
                        ports[p["port"]].send(mido.Message("note_on", note=nn, velocity=100, channel=ch))
            time.sleep(chord_sec)
            for p in parts:
                send = [root] if p["role"] == "bass" else triad
                for ch in p["channels"]:
                    for nn in send:
                        ports[p["port"]].send(mido.Message("note_off", note=nn, channel=ch))
    finally:
        all_off()
    sd.wait()
    for op in ports.values():
        op.close()

    setname = f"hook_{(slugify(a.key) + '_') if a.key else ''}{int(a.bpm)}bpm_{slugify(a.progression)}"
    outdir = Path(a.out).expanduser() / setname
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"✓ {len(parts)} stems → {outdir}")
    for p in parts:
        cols = [mapping.index(c) for c in p["ins"]]
        stem = rec[:, cols]
        sf.write(str(outdir / f"{p['name']}.wav"), stem, sr, subtype="PCM_24")
        pk = float(np.max(np.abs(stem))) if stem.size else 0.0
        db = 20 * np.log10(pk) if pk > 0 else -99
        warn = "   ⚠ SILENT — is the TX-6 held by Ableton/another app? check routing + levels" if db < -45 else ""
        print(f"   {p['name']:8} ({p['role']}, ins {p['ins']}): peak {db:5.1f} dBFS{warn}")


if __name__ == "__main__":
    main()
