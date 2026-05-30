#!/usr/bin/env python3
"""Record an arpeggiated chord progression.

Engage the arp on the synth, then this tool sends the chord notes at the right
times — note_on at the start of each chord, note_off when the next chord starts.
The arp arpeggiates whatever's currently held, so you get a chord progression
arp loop.

Assumes MIDI clock is being streamed externally (e.g., via stream-clock.py).

Usage:
  record-progression.py --instrument grandmother --name acid-cmin \\
    --progression "Cm Ab Eb Bb" --bars-per-chord 2 --bpm 120 \\
    --midi-port "Moog Grandmother"
"""
from __future__ import annotations

import argparse
import re
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


# ---------- chord parsing ----------

ROOTS = {
    'c': 0, 'c#': 1, 'cs': 1, 'db': 1, 'd': 2, 'd#': 3, 'ds': 3, 'eb': 3,
    'e': 4, 'f': 5, 'f#': 6, 'fs': 6, 'gb': 6, 'g': 7, 'g#': 8, 'gs': 8,
    'ab': 8, 'a': 9, 'a#': 10, 'as': 10, 'bb': 10, 'b': 11,
}

CHORD_TYPES = {
    '':       [0, 4, 7],          # major (default)
    'maj':    [0, 4, 7],
    'major':  [0, 4, 7],
    'M':      [0, 4, 7],
    'maj7':   [0, 4, 7, 11],
    'M7':     [0, 4, 7, 11],
    'maj9':   [0, 4, 7, 11, 14],
    'm':      [0, 3, 7],          # minor
    'min':    [0, 3, 7],
    'minor':  [0, 3, 7],
    'm7':     [0, 3, 7, 10],
    'min7':   [0, 3, 7, 10],
    'm9':     [0, 3, 7, 10, 14],
    '7':      [0, 4, 7, 10],      # dominant 7
    'dim':    [0, 3, 6],
    'dim7':   [0, 3, 6, 9],
    'aug':    [0, 4, 8],
    'sus2':   [0, 2, 7],
    'sus4':   [0, 5, 7],
    '5':      [0, 7],             # power chord (root + fifth)
}


def parse_chord(name: str, root_octave: int = 3) -> list[int]:
    """Parse a chord name like 'Cm', 'Ab7', 'Eb maj9' into a list of MIDI note numbers.
    root_octave: scientific pitch, so C3 = MIDI 48."""
    name = name.strip()
    m = re.match(r'^([A-Ga-g])([#sb]?)\s*(.*)$', name)
    if not m:
        raise ValueError(f"can't parse chord '{name}'")
    letter, acc, suffix = m.groups()
    letter_lc = letter.lower()
    root_pc = ROOTS[letter_lc]
    if acc == 'b':
        root_pc -= 1
    elif acc in ('s', '#'):
        root_pc += 1
    root_pc = root_pc % 12

    suffix_clean = suffix.strip()
    intervals = CHORD_TYPES.get(suffix_clean)
    if intervals is None:
        raise ValueError(f"unknown chord type '{suffix_clean}' in '{name}'. known: {sorted(CHORD_TYPES.keys())}")

    root_midi = root_pc + (root_octave + 1) * 12
    return [root_midi + i for i in intervals]


def parse_progression(prog_str: str, root_octave: int = 3) -> list[tuple[str, list[int]]]:
    """Parse 'Cm Ab Eb Bb' into [('Cm', [48,51,55]), ('Ab', [56,60,63]), ...]"""
    chord_names = prog_str.split()
    return [(name, parse_chord(name, root_octave)) for name in chord_names]


# ---------- audio device helper ----------

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


def find_bank_root() -> Path | None:
    p = Path.cwd().resolve()
    for c in [p, *p.parents]:
        if (c / "instruments").is_dir() and (c / "manifest.template.json").exists():
            return c
    fallback = Path.home() / "projects" / "spacepit-sample-bank"
    if (fallback / "instruments").is_dir():
        return fallback
    return None


def all_notes_off(port, channel: int) -> None:
    port.send(mido.Message('control_change', control=123, value=0, channel=channel))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instrument", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--progression", required=True, help="space-separated chord names, e.g. 'Cm Ab Eb Bb'")
    ap.add_argument("--bpm", type=float, default=120.0)
    ap.add_argument("--bars-per-chord", type=float, default=2)
    ap.add_argument("--meter", type=int, default=4)
    ap.add_argument("--root-octave", type=int, default=3, help="scientific octave for chord root (C3=middle C is 3)")
    ap.add_argument("--velocity", type=int, default=80)
    ap.add_argument("--midi-port", default="Moog Grandmother")
    ap.add_argument("--midi-channel", type=int, default=1)
    ap.add_argument("--audio-device", default="TX-6")
    ap.add_argument("--input-channels", default="11,12")
    ap.add_argument("--sample-rate", type=int, default=48000)
    ap.add_argument("--bit-depth", type=int, choices=[16, 24, 32], default=24)
    ap.add_argument("--tail-sec", type=float, default=1.0, help="extra recording after last chord ends")
    ap.add_argument("--bank-root", default=None)
    ap.add_argument("--send-clock", action="store_true", help="send MIDI clock during recording (in case clock isn't streaming externally)")
    ap.add_argument("--mode", default="chord", choices=["chord", "root", "arp"],
        help="chord = send all chord notes simultaneously (poly synth OR mono with arp engaged); "
             "root = send only the root note (mono synth bass mode, no arp); "
             "arp = script-side arpeggio at --arp-rate (mono synth, no arp engaged)")
    ap.add_argument("--arp-rate", type=float, default=16, help="for --mode arp: note value denominator. 8=1/8 notes, 16=1/16 notes (default), 32=1/32 notes.")
    ap.add_argument("-y", "--yes", action="store_true")
    args = ap.parse_args()

    try:
        channels = [int(c) for c in args.input_channels.split(",")]
    except ValueError:
        print("bad --input-channels", file=sys.stderr); sys.exit(1)

    bank_root = Path(args.bank_root).resolve() if args.bank_root else find_bank_root()
    if not bank_root:
        print("can't find bank root", file=sys.stderr); sys.exit(1)

    # parse progression
    try:
        prog = parse_progression(args.progression, args.root_octave)
    except ValueError as e:
        print(f"chord parse error: {e}", file=sys.stderr); sys.exit(1)

    # timing
    sec_per_bar = (60.0 / args.bpm) * args.meter
    sec_per_chord = args.bars_per_chord * sec_per_bar
    total_bars = len(prog) * args.bars_per_chord
    body_duration = total_bars * sec_per_bar
    total_duration = body_duration + args.tail_sec

    # output path
    out_dir = bank_root / "instruments" / args.instrument / "loops" / "raw"
    out_dir.mkdir(parents=True, exist_ok=True)
    key_tag = chord_to_key_tag(prog[0][0]) if prog else "Cmaj"
    import re as _re
    _nm = _re.sub(r'[^A-Za-z0-9]+', '-', args.name).strip('-') or 'loop'      # clean Splice name (no spaces)
    fname = f"hook_{args.instrument}_{int(args.bpm)}_{_nm}_{key_tag}.wav"      # Splice: brand_instrument_bpm_sound_key
    out_path = out_dir / fname

    # plan
    print("=== progression record ===")
    print(f"  instrument:  {args.instrument}")
    print(f"  name:        {args.name}")
    print(f"  bpm:         {args.bpm}  ({args.meter}/{args.meter} time)")
    print(f"  progression: {args.progression}")
    print(f"  bars/chord:  {args.bars_per_chord}  ·  total {int(total_bars)} bars")
    print(f"  duration:    {body_duration:.2f}s body + {args.tail_sec}s tail = {total_duration:.2f}s")
    print(f"  root octave: {args.root_octave} (scientific — C3 = middle C is 3)")
    print(f"  chord notes:")
    for name, notes in prog:
        note_names = [_midi_to_name(n) for n in notes]
        print(f"    {name:<8} = {notes}  ({', '.join(note_names)})")
    print(f"  output:      {out_path}")
    print()
    print("NOTE: this tool assumes MIDI clock is streaming externally (gmclock).")
    print("      arp on Grandmother should be ENGAGED and synced to external clock.")
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
    n_samples = int(total_duration * args.sample_rate)
    rec = sd.rec(n_samples, samplerate=args.sample_rate, channels=len(channels),
                 device=audio_dev, mapping=channels, dtype="float32", blocking=False)

    # open midi
    port = mido.open_output(midi_port_name)
    ch = args.midi_channel - 1
    try:
        # if sending clock embedded, start now
        if args.send_clock:
            port.send(mido.Message('start'))
            clock_interval = 60.0 / args.bpm / 24.0
            next_clock = time.time()
        # Mode-specific note selection
        def notes_for_mode(chord_notes):
            if args.mode == "root":
                return [chord_notes[0]]  # just the root
            return chord_notes  # 'chord' or 'arp' both start with all chord notes

        # send first chord at t=0
        t0 = time.time()
        prev_notes = []
        for i, (chord_name, notes) in enumerate(prog):
            target_t = t0 + i * sec_per_chord
            # interleave clock ticks while we wait for next chord
            while time.time() < target_t:
                if args.send_clock and time.time() >= next_clock:
                    port.send(mido.Message('clock'))
                    next_clock += clock_interval
                else:
                    time.sleep(0.0005)

            if args.mode == "arp":
                # Script-side arpeggio — release prev, then step through this chord's notes
                for n in prev_notes:
                    port.send(mido.Message('note_off', note=n, velocity=0, channel=ch))
                prev_notes = []
                # arp_rate is the note-value denominator (8 = 1/8 notes, 16 = 1/16 notes).
                # 1 whole bar in 4/4 = 4 beats; a 1/N note occupies sec_per_bar / N seconds.
                arp_interval = sec_per_bar / args.arp_rate  # sec per arp note
                # how many arp notes fit in this chord's duration
                arp_notes = []
                t_arp = target_t
                idx = 0
                chord_end = target_t + sec_per_chord
                while t_arp < chord_end:
                    n = notes[idx % len(notes)]
                    # Wait until t_arp
                    while time.time() < t_arp:
                        if args.send_clock and time.time() >= next_clock:
                            port.send(mido.Message('clock'))
                            next_clock += clock_interval
                        else:
                            time.sleep(0.0005)
                    # fire note (with short note_off scheduled inline by next iteration's release)
                    port.send(mido.Message('note_on', note=n, velocity=args.velocity, channel=ch))
                    # short note duration — fire note_off after ~80% of interval
                    note_off_time = t_arp + arp_interval * 0.8
                    while time.time() < note_off_time:
                        if args.send_clock and time.time() >= next_clock:
                            port.send(mido.Message('clock'))
                            next_clock += clock_interval
                        else:
                            time.sleep(0.0005)
                    port.send(mido.Message('note_off', note=n, velocity=0, channel=ch))
                    t_arp += arp_interval
                    idx += 1
                print(f"  bar {int(i * args.bars_per_chord) + 1:>2}: → {chord_name}  ({idx} arp notes)")
            else:
                # chord or root mode — send notes simultaneously
                this_notes = notes_for_mode(notes)
                for n in prev_notes:
                    port.send(mido.Message('note_off', note=n, velocity=0, channel=ch))
                for n in this_notes:
                    port.send(mido.Message('note_on', note=n, velocity=args.velocity, channel=ch))
                prev_notes = this_notes
                print(f"  bar {int(i * args.bars_per_chord) + 1:>2}: → {chord_name}  ({args.mode} mode)")

        # let last chord play out (with clock ticks if enabled)
        target_end = t0 + body_duration
        while time.time() < target_end:
            if args.send_clock and time.time() >= next_clock:
                port.send(mido.Message('clock'))
                next_clock += clock_interval
            else:
                time.sleep(0.0005)
        # release last chord
        for n in prev_notes:
            port.send(mido.Message('note_off', note=n, velocity=0, channel=ch))

        if args.send_clock:
            port.send(mido.Message('stop'))

        # tail (no more clock, just record)
        sd.wait()
    finally:
        all_notes_off(port, ch)
        port.close()

    # save — process the raw take into a SAMPLE-EXACT, seam-wrapped PERFECT LOOP.
    loop, onset, loop_len = make_perfect_loop(rec, args.sample_rate, body_duration)
    sf.write(str(out_path), loop, args.sample_rate, subtype=SUBTYPE[args.bit_depth])
    # keep the raw take next to it (front latency + full tail) in case we want to re-trim by hand
    sf.write(str(out_path.with_name(out_path.stem + "_raw.wav")), rec, args.sample_rate, subtype=SUBTYPE[args.bit_depth])
    peak = float(np.max(np.abs(loop)))
    peak_db = 20 * np.log10(max(1e-10, peak))
    loop_sec = loop_len / args.sample_rate
    print(f"\n✓ saved {fname}  (PERFECT LOOP — {loop_sec:.3f}s = exactly {int(total_bars)} bars @ {args.bpm}bpm)")
    print(f"  downbeat trimmed at {onset/args.sample_rate*1000:.0f}ms · tail folded back · peak {peak_db:+.1f} dBFS")
    if peak >= 0.99:
        print("  ⚠ clipping detected")


def _midi_to_name(midi: int) -> str:
    names = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
    return f"{names[midi % 12]}{midi // 12 - 1}"


def make_perfect_loop(rec, sr, body_duration, onset_db=-38.0, wrap_sec=0.6):
    """Turn the raw take (leading MIDI->audio latency + trailing ring-out) into a file that ACTUALLY
    loops: (1) trim to the downbeat so the loop starts ON the first chord, (2) cut to EXACTLY
    body_duration = N bars, sample-accurate to the BPM, so it sits dead-on the DAW grid, and (3) fold
    the ring-out tail back onto the top so the last chord's release wraps seamlessly into the first
    chord — no click at the seam, no chopped-off tail."""
    import numpy as np
    rec = np.asarray(rec)
    mono = rec.mean(axis=1) if rec.ndim > 1 else rec
    a = np.abs(mono)
    pk = float(a.max()) or 1.0
    above = np.where(a > pk * (10.0 ** (onset_db / 20.0)))[0]
    onset = int(above[0]) if len(above) else 0           # downbeat = first sound
    loop_len = int(round(body_duration * sr))            # exact N-bar length in samples
    end = onset + loop_len
    if end > len(rec):                                   # take ran short — pad with silence
        pad = np.zeros((end - len(rec),) + rec.shape[1:], dtype=rec.dtype)
        rec = np.concatenate([rec, pad])
    body = rec[onset:end].astype(np.float64).copy()
    W = int(min(len(rec) - end, loop_len // 2, round(wrap_sec * sr)))   # tail-wrap window
    if W > 0:
        tail = rec[end:end + W].astype(np.float64)       # ring-out that continues past the loop
        win = np.linspace(1.0, 0.0, W)                   # decay it to 0 as it folds in -> no seam click
        if rec.ndim > 1:
            win = win[:, None]
        body[:W] += tail * win
    pk2 = float(np.max(np.abs(body)))
    if pk2 > 0.999:                                      # keep the fold from clipping
        body *= 0.999 / pk2
    return body.astype(np.float32), onset, loop_len


def chord_to_key_tag(chord_name):
    """Readable key tag for the filename from the first chord — root + maj/min (e.g. 'Cmin',
    'Dbmaj', 'F#min'). Not full key detection, just a clean Splice-style label."""
    import re
    m = re.match(r'^\s*([A-Ga-g][#b]?)', chord_name or "")
    if not m:
        return "Cmaj"
    root = m.group(1)[0].upper() + m.group(1)[1:]                 # keep accidental as written
    rest = (chord_name[m.end():] or "").lower()
    minor = (rest.startswith("m") and not rest.startswith("maj")) or rest.startswith("min") or rest.startswith("-")
    return f"{root}{'min' if minor else 'maj'}"


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\naborted.")
