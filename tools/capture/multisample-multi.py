#!/usr/bin/env python3
"""Multi-synth multisampler — "multisample your whole rig in one pass."

Walk a chromatic note range across MULTIPLE synths at once (each on its own MIDI port/channel), record
EVERY synth's interface inputs together on a multi-in interface (TX-6), then split + slice into a
per-synth folder of note-named WAVs. Drag each folder into Ableton Sampler/Simpler and you've got a
playable instrument per synth — all from one pass.

Same multi-synth idea as collect-multi.py (the progression collector), but it walks the notes one at a
time (each with a release tail) instead of firing a loop, so each synth comes out as a multisample set.

  multisample-multi.py --audio-device TX-6 --note-range C2-C5 --step 4 \
      --part "jp:mio:1:1,2" --part "moog:Moog Grandmother:all:3,4" --out ~/Desktop/multisampled

  --part = name:midi_port:midi_channel:input_channels   (identical to collect-multi)
           • midi_channel is 1-16, or "all" (sends on every channel — for synths whose receive
             channel is unknown, like the Grandmother on its own port).
           • input_channels are the interface's 1-based ins this synth lands on (a stereo pair or mono).

NOTE: the capture device (TX-6) must be FREE — if Ableton or another app holds it, the inputs record
SILENCE. The per-synth peak check below warns you if a synth came back silent (device contention).
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

# Reuse the trim-dead-air + clip-safe make-up gain pass so multisamples open hot in Ableton,
# exactly like every other bench capture (clean-wavs.clean_one).
_cspec = importlib.util.spec_from_file_location(
    "clean_wavs", str(Path(__file__).resolve().parent.parent / "pack" / "clean-wavs.py"))
clean = importlib.util.module_from_spec(_cspec)
_cspec.loader.exec_module(clean)

NOTE_NAMES = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]


def note_to_midi(name: str) -> int:
    """Scientific pitch — C4 = 60 (middle C). Accepts C2, F#3, Eb4, Bb2 …"""
    m = re.match(r"^([A-Ga-g])([#sb]?)(-?\d+)$", name.strip())
    if not m:
        raise SystemExit(f"✗ bad note {name!r} — expected like C2, F#3, Eb4")
    letter, acc, octv = m.groups()
    base = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}[letter.lower()]
    if acc in ("#", "s"):
        base += 1
    elif acc == "b":
        base -= 1
    return base + (int(octv) + 1) * 12


def midi_to_note(m: int) -> str:
    return f"{NOTE_NAMES[m % 12]}{m // 12 - 1}"


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def resolve_device(name):
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and name.lower() in d["name"].lower():
            return i, d
    raise SystemExit(f"✗ no input device matches {name!r}")


def parse_part(spec):
    name, port, ch, ins = spec.split(":", 3)
    channels = list(range(16)) if ch.strip().lower() == "all" else [int(ch) - 1]
    return {"name": name, "port": port, "channels": channels,
            "ins": [int(x) for x in ins.split(",")]}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--audio-device", required=True)
    ap.add_argument("--note-range", default="C2-C5", help="e.g. C2-C5 (scientific pitch, C4 = middle C)")
    ap.add_argument("--step", type=int, default=4, help="semitone step between sampled notes (3 dense … 12 octaves)")
    ap.add_argument("--sustain", type=float, default=2.0, help="hold each note this long (s)")
    ap.add_argument("--tail", type=float, default=1.5, help="record this long after note-off for the release (s)")
    ap.add_argument("--velocity", type=int, default=100)
    ap.add_argument("--part", action="append", required=True, help="name:midi_port:midi_channel:input_channels")
    ap.add_argument("--out", default=str(Path.home() / "Desktop" / "multisampled"))
    ap.add_argument("--sample-rate", type=int, default=48000)
    a = ap.parse_args()

    parts = [parse_part(s) for s in a.part]
    lo, hi = a.note_range.split("-")
    lo_m, hi_m = note_to_midi(lo), note_to_midi(hi)
    notes = list(range(lo_m, hi_m + 1, max(1, a.step)))
    if not notes:
        raise SystemExit(f"✗ no notes in range {a.note_range} step {a.step}")
    sr = a.sample_rate
    lead = 0.5
    per_note = a.sustain + a.tail
    total_sec = lead + len(notes) * per_note + 0.3
    n = int(total_sec * sr)

    dev_idx, dev = resolve_device(a.audio_device)
    mapping = sorted({c for p in parts for c in p["ins"]})            # union of inputs, recorded together
    ports = {name: mido.open_output(name) for name in {p["port"] for p in parts}}

    def all_off():
        for op in ports.values():
            for ch in range(16):
                try:
                    op.send(mido.Message("control_change", control=123, value=0, channel=ch))
                except Exception:
                    pass

    print(f"● multisampling {len(parts)} synth(s) · {len(notes)} notes ({lo}->{hi} step {a.step}) "
          f"· ~{total_sec/60:.1f} min · {dev['name']} ch {mapping}")
    rec = sd.rec(n, samplerate=sr, channels=len(mapping), device=dev_idx, mapping=mapping,
                 dtype="float32", blocking=False)
    t_rec0 = time.time()
    note_times = []   # (midi, actual_offset_into_recording) — real times so slicing is drift-proof
    try:
        all_off()
        time.sleep(lead)
        for i, nn in enumerate(notes):
            t_on = time.time() - t_rec0
            note_times.append((nn, t_on))
            for p in parts:
                for ch in p["channels"]:
                    ports[p["port"]].send(mido.Message("note_on", note=nn, velocity=a.velocity, channel=ch))
            print(f"  [{i+1:>2}/{len(notes)}] {midi_to_note(nn)}")
            time.sleep(a.sustain)
            for p in parts:
                for ch in p["channels"]:
                    ports[p["port"]].send(mido.Message("note_off", note=nn, channel=ch))
            time.sleep(a.tail)
    finally:
        all_off()
    sd.wait()
    for op in ports.values():
        op.close()

    outroot = Path(a.out).expanduser() / f"multisample_{slugify(a.note_range)}"
    print(f"slicing per synth into {outroot} ...")
    for p in parts:
        cols = [mapping.index(c) for c in p["ins"]]
        pdir = outroot / slugify(p["name"])
        pdir.mkdir(parents=True, exist_ok=True)
        saved = 0
        silent = 0
        peaks = []
        for idx, (nn, t_on) in enumerate(note_times):
            s0 = int(max(0.0, t_on - 0.02) * sr)
            next_on = note_times[idx + 1][1] if idx + 1 < len(note_times) else total_sec
            s1 = int(min(next_on - 0.03, t_on + a.sustain + a.tail) * sr)
            seg = rec[s0:s1][:, cols]
            pk = float(np.max(np.abs(seg))) if seg.size else 0.0
            if pk < 0.005:                                   # ~-46 dBFS — silent note, skip it
                silent += 1
                continue
            peaks.append(pk)
            fpath = pdir / f"{slugify(p['name'])}_{midi_to_note(nn)}.wav"
            sf.write(str(fpath), seg, sr, subtype="PCM_24")
            try:
                clean.clean_one(fpath, target_peak_db=None, gain_db=3.0)   # trim dead air + open hot
            except Exception:
                pass
            saved += 1
        db = 20 * np.log10(max(peaks)) if peaks else -99.0
        warn = "  SILENT - is the TX-6 held by another app? check routing + levels" if saved == 0 else ""
        print(f"   {slugify(p['name'])} ({saved} notes, ins {p['ins']}): peak {db:5.1f} dBFS{warn}")
    print(f"→ {outroot}")


if __name__ == "__main__":
    main()
