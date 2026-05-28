#!/usr/bin/env python3
"""Record a complete hi-hat subdivision suite in one performance.

Holds the synth's arp clock division at 1/16 (your hardware setting). This script
then sends MIDI clock at VARYING speeds to simulate different effective subdivisions
at a target tempo — without you touching the div knob:

  section          synth tick rate              clock BPM sent
  ─────────────────────────────────────────────────────────────
  quarter (1/4)    35 / target_bpm × 4          target_bpm / 4
  eighth (1/8)     70 / target_bpm × 4          target_bpm / 2
  sixteenth (1/16) 140 / target_bpm × 4         target_bpm
  thirtysecond     280 / target_bpm × 4         target_bpm × 2
  fill (random)    varying                       random per beat

You hold a key + LATCH on the synth, set arp div to 1/16, hit go on the script.
Tool records the whole performance to ONE WAV plus 5 chopped section WAVs.

Usage:
  record-hihat-suite.py --instrument grandmother --name trap-140 \\
    --target-bpm 140 --bars-per-section 4 --midi-port "Moog Grandmother"
"""
from __future__ import annotations

import argparse
import random
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

# Sections to record. (label, clock_bpm_multiplier, kind)
# Synth arp div is fixed at 1/16 — we vary the MIDI clock BPM to hit different effective subdivisions.
SECTIONS = [
    ("quarter",      0.25, "steady"),     # tick every quarter note at target BPM
    ("eighth",       0.5,  "steady"),     # tick every eighth note
    ("sixteenth",    1.0,  "steady"),     # tick every sixteenth (target BPM as-sent)
    ("thirtysecond", 2.0,  "steady"),     # tick every 32nd
    ("fill",         1.0,  "random"),     # random subdivision per beat — stuttering fill
]


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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instrument", required=True)
    ap.add_argument("--name", required=True, help="name prefix for output files")
    ap.add_argument("--target-bpm", type=float, default=140.0, help="the effective tempo for the loop tags")
    ap.add_argument("--bars-per-section", type=int, default=4)
    ap.add_argument("--meter", type=int, default=4)
    ap.add_argument("--fill-bars", type=int, default=2, help="how many bars for the random fill section")
    ap.add_argument("--midi-port", default="Moog Grandmother")
    ap.add_argument("--audio-device", default="TX-6")
    ap.add_argument("--input-channels", default="11,12")
    ap.add_argument("--sample-rate", type=int, default=48000)
    ap.add_argument("--bit-depth", type=int, choices=[16, 24, 32], default=24)
    ap.add_argument("--bank-root", default=None)
    ap.add_argument("--lead-in-sec", type=float, default=0.3, help="silent lead-in before first section")
    ap.add_argument("--tail-sec", type=float, default=0.5, help="extra recording after last section")
    ap.add_argument("--save-full", action="store_true", help="also save the full unchopped recording")
    ap.add_argument("-y", "--yes", action="store_true")
    args = ap.parse_args()

    try:
        channels = [int(c) for c in args.input_channels.split(",")]
    except ValueError:
        print("bad --input-channels", file=sys.stderr); sys.exit(1)

    bank_root = Path(args.bank_root).resolve() if args.bank_root else find_bank_root()
    if not bank_root:
        print("can't find bank root", file=sys.stderr); sys.exit(1)

    out_dir = bank_root / "instruments" / args.instrument / "loops" / "raw"
    out_dir.mkdir(parents=True, exist_ok=True)

    # compute section durations in seconds (at target BPM)
    sec_per_bar_target = (60.0 / args.target_bpm) * args.meter
    section_plan = []  # list of (label, duration_sec, clock_bpm_sequence)
    total_body = 0.0

    for label, mult, kind in SECTIONS:
        bars = args.fill_bars if label == "fill" else args.bars_per_section
        duration = bars * sec_per_bar_target
        if kind == "steady":
            section_plan.append((label, duration, mult * args.target_bpm, "steady"))
        else:
            # random — clock BPM will be picked randomly per beat in send_clock_section
            section_plan.append((label, duration, None, "random"))
        total_body += duration

    total_duration = args.lead_in_sec + total_body + args.tail_sec
    n_samples = int(total_duration * args.sample_rate)

    print("=== hi-hat suite record ===")
    print(f"  instrument:    {args.instrument}")
    print(f"  name prefix:   {args.name}")
    print(f"  target BPM:    {args.target_bpm}")
    print(f"  bars/section:  {args.bars_per_section}  (fill: {args.fill_bars} bars)")
    print(f"  sections:")
    for label, dur, bpm, kind in section_plan:
        if kind == "steady":
            print(f"    {label:<13} {dur:>5.2f}s  ·  clock @ {bpm:>5.1f} BPM (steady)")
        else:
            print(f"    {label:<13} {dur:>5.2f}s  ·  clock @ varying BPM (random fill)")
    print(f"  total body:    {total_body:.1f}s")
    print(f"  total record:  {total_duration:.1f}s (lead-in {args.lead_in_sec}s + body + tail {args.tail_sec}s)")
    print(f"  output:        {out_dir}/")
    print()
    print("BEFORE FIRING:")
    print("  1. Grandmother arp ON, div set to 1/16, clock source EXT")
    print("  2. Hold a note + engage LATCH so arp keeps playing")
    print("  3. Your patch should produce a percussive hit per arp tick (noise + fast env + S=0)")
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
    rec = sd.rec(n_samples, samplerate=args.sample_rate, channels=len(channels),
                 device=audio_dev, mapping=channels, dtype="float32", blocking=False)

    # open midi
    port = mido.open_output(midi_port_name)
    section_boundaries_sec = []  # for chopping later
    t0_overall = time.time()
    try:
        if args.lead_in_sec > 0:
            time.sleep(args.lead_in_sec)

        port.send(mido.Message('start'))
        body_start = time.time()

        for idx, (label, duration, bpm, kind) in enumerate(section_plan):
            section_start = time.time()
            section_boundaries_sec.append(section_start - t0_overall)
            print(f"  ▶ section {idx+1}/{len(section_plan)}: {label}", flush=True)

            if kind == "steady":
                interval = 60.0 / bpm / 24.0
                next_tick = time.time()
                section_end = section_start + duration
                while time.time() < section_end:
                    now = time.time()
                    if now >= next_tick:
                        port.send(mido.Message('clock'))
                        next_tick = next_tick + interval
                    else:
                        time.sleep(min(0.0005, next_tick - now))
            else:
                # random fill — pick a clock multiplier per beat, hold for one beat
                section_end = section_start + duration
                beat_dur = sec_per_bar_target / args.meter
                while time.time() < section_end:
                    # pick a random subdivision multiplier from the steady set
                    mult = random.choice([0.5, 1.0, 1.0, 2.0, 2.0, 4.0])  # weighted toward 1/16, 1/32, 1/64
                    bpm_this = mult * args.target_bpm
                    interval = 60.0 / bpm_this / 24.0
                    beat_end = min(time.time() + beat_dur, section_end)
                    next_tick = time.time()
                    while time.time() < beat_end:
                        now = time.time()
                        if now >= next_tick:
                            port.send(mido.Message('clock'))
                            next_tick = next_tick + interval
                        else:
                            time.sleep(min(0.0005, next_tick - now))

        port.send(mido.Message('stop'))
        # NOTE: intentionally NOT sending all-notes-off here — preserves the
        # user's latched note for the next capture. Use gmpanic if you need
        # to clear stuck notes between sessions.

        # tail
        if args.tail_sec > 0:
            time.sleep(args.tail_sec)
        sd.wait()

    finally:
        port.close()

    # save the full recording optionally
    subtype = SUBTYPE[args.bit_depth]
    if args.save_full:
        full_path = out_dir / f"{args.name}_FULL_{int(args.target_bpm)}bpm.wav"
        sf.write(str(full_path), rec, args.sample_rate, subtype=subtype)
        print(f"\n✓ saved full: {full_path.name}")

    # chop into sections based on the recorded boundaries
    print("\n--- chopping sections ---")
    section_boundaries_sec.append(time.time() - t0_overall - args.tail_sec)  # end of last section
    for i, (label, duration, bpm, kind) in enumerate(section_plan):
        start_sample = int(section_boundaries_sec[i] * args.sample_rate)
        end_sample = int(section_boundaries_sec[i+1] * args.sample_rate)
        section_audio = rec[start_sample:end_sample]
        bars = args.fill_bars if label == "fill" else args.bars_per_section
        fname = f"{args.name}_{label}_{int(args.target_bpm)}bpm_{bars}bars.wav"
        fpath = out_dir / fname
        sf.write(str(fpath), section_audio, args.sample_rate, subtype=subtype)
        peak = float(np.max(np.abs(section_audio))) if len(section_audio) > 0 else 0
        peak_db = 20 * np.log10(max(1e-10, peak))
        flag = " ⚠ silent" if peak_db < -50 else (" ⚠ clip" if peak >= 0.99 else "")
        print(f"  {fname:<50}  ({len(section_audio)/args.sample_rate:.1f}s, peak {peak_db:+.1f} dBFS{flag})")

    print(f"\n→ {out_dir}/")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\naborted.")
