#!/usr/bin/env python3
"""
thespacepit master sample bank — drum-machine capture tool

Walks a per-tier voice matrix (kick × N, snare × N, hh × N, …), captures
variations per voice, names + saves WAVs into
  instruments/<slug>/kits/<kit>/raw/

Two trigger modes:
  manual (default) — you trigger the pad/voice, tool records a fixed window
  midi             — tool sends MIDI note (GM drum map or per-instrument voice_map)

Usage examples:
  uv run capture-drum.py --instrument sp-1200 --kit factory --tier standard
  uv run capture-drum.py --instrument sp-1200 --kit ny-grit --tier gold
  uv run capture-drum.py --instrument tr-808 --kit factory --tier quick --mode midi
  uv run capture-drum.py --instrument sp-1200 --kit factory --voice-list "kick:5,snare:5,hh-closed:3"

See README.md in this folder for the full guide.
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
    print(f"missing dep: {e}\n  run:  uv sync   (in tools/capture/)", file=sys.stderr)
    sys.exit(1)


# ---------- tier voice matrices ----------

TIER_MATRICES = {
    "quick": [
        ("kick", 3),
        ("snare", 3),
        ("hh-closed", 1),
        ("hh-open", 1),
        ("clap", 1),
        ("ride", 1),
        ("crash", 1),
        ("tom-lo", 1),
        ("tom-mid", 1),
        ("tom-hi", 1),
    ],
    "standard": [
        ("kick", 5),
        ("snare", 5),
        ("hh-closed", 3),
        ("hh-open", 3),
        ("pedal-hh", 1),
        ("clap", 3),
        ("ride", 2),
        ("crash", 2),
        ("tom-lo", 3),
        ("tom-mid", 3),
        ("tom-hi", 3),
        ("rim", 2),
        ("cowbell", 2),
        ("clave", 1),
    ],
    "gold": [
        ("kick", 8),
        ("snare", 8),
        ("hh-closed", 6),
        ("hh-open", 4),
        ("pedal-hh", 2),
        ("clap", 5),
        ("ride", 3),
        ("crash", 3),
        ("tom-lo", 5),
        ("tom-mid", 5),
        ("tom-hi", 5),
        ("rim", 3),
        ("cowbell", 3),
        ("clave", 2),
        ("conga-lo", 2),
        ("conga-mid", 2),
        ("conga-hi", 2),
    ],
}

# General MIDI drum note defaults — override per-instrument via manifest's voice_map.
GM_DRUM_MAP = {
    "kick": 36,
    "kick-alt": 35,
    "snare": 38,
    "snare-alt": 40,
    "rim": 37,
    "clap": 39,
    "tom-lo": 41,
    "hh-closed": 42,
    "tom-mid": 45,
    "hh-open": 46,
    "pedal-hh": 44,
    "crash": 49,
    "ride": 51,
    "tom-hi": 48,
    "crash-2": 57,
    "ride-2": 59,
    "cowbell": 56,
    "clave": 75,
    "conga-hi": 62,
    "conga-mid": 63,
    "conga-lo": 64,
}

# Default record windows per voice (sec). Crashes ring longer; kicks are short.
DEFAULT_RECORD_SEC = 4.0
VOICE_RECORD_OVERRIDE = {
    "crash": 10.0,
    "crash-2": 10.0,
    "ride": 8.0,
    "ride-2": 8.0,
    "hh-open": 6.0,
    "kick": 2.5,
    "rim": 2.0,
    "clap": 2.0,
    "clave": 2.0,
}


# ---------- helpers ----------

NOTE_NAMES = ["c", "cs", "d", "ds", "e", "f", "fs", "g", "gs", "a", "as", "b"]


def variant_letter(n: int) -> str:
    """1 -> 'a', 2 -> 'b', ..., 26 -> 'z'."""
    if n < 1 or n > 26:
        raise ValueError(f"variant {n} out of range a-z (got {n})")
    return chr(ord("a") + n - 1)


def parse_voice_list(s: str) -> list[tuple[str, int]]:
    """'kick:5,snare:5,hh-closed:3' -> [('kick', 5), ('snare', 5), ('hh-closed', 3)]"""
    out = []
    for pair in s.split(","):
        if ":" not in pair:
            raise ValueError(f"bad voice-list entry: {pair} (format voice:count)")
        v, n = pair.split(":")
        out.append((v.strip(), int(n.strip())))
    return out


def find_bank_root(start: Path | None = None) -> Path | None:
    p = (start or Path.cwd()).resolve()
    for candidate in [p, *p.parents]:
        if (candidate / "instruments").is_dir() and (candidate / "manifest.template.json").exists():
            return candidate
    fallback = Path.home() / "projects" / "spacepit-sample-bank"
    if (fallback / "instruments").is_dir():
        return fallback
    return None


def pick_from_list(prompt: str, items: list[str]) -> int:
    print(f"\n{prompt}")
    for i, item in enumerate(items):
        print(f"  [{i}] {item}")
    while True:
        raw = input("→ pick number: ").strip()
        try:
            idx = int(raw)
            if 0 <= idx < len(items):
                return idx
        except ValueError:
            pass
        print("nope, try again (or ctrl-c to abort)")


def resolve_audio_device(arg: str | None) -> int:
    devs = sd.query_devices()
    in_devs = [(i, d) for i, d in enumerate(devs) if d["max_input_channels"] > 0]
    if not in_devs:
        print("error: no audio input devices found.", file=sys.stderr)
        sys.exit(1)
    if arg:
        matches = [(i, d) for (i, d) in in_devs if arg.lower() in d["name"].lower()]
        if len(matches) == 1:
            return matches[0][0]
        if len(matches) > 1:
            print(f"ambiguous --audio-device '{arg}' matches: {[d['name'] for _, d in matches]}", file=sys.stderr)
            sys.exit(1)
        print(f"no audio device matches '{arg}'. available:", file=sys.stderr)
        for i, d in in_devs:
            print(f"  - {d['name']}  ({d['max_input_channels']} in)", file=sys.stderr)
        sys.exit(1)
    items = [f"{d['name']}  ({d['max_input_channels']} in)" for _, d in in_devs]
    idx = pick_from_list("select audio input device:", items)
    return in_devs[idx][0]


def resolve_midi_port(arg: str | None) -> str:
    ports = mido.get_output_names()
    if not ports:
        print("error: no MIDI output ports found.", file=sys.stderr)
        sys.exit(1)
    if arg:
        if arg in ports:
            return arg
        matches = [p for p in ports if arg.lower() in p.lower()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            print(f"ambiguous --midi-port '{arg}' matches: {matches}", file=sys.stderr)
            sys.exit(1)
        print(f"no MIDI port matches '{arg}'. available:", file=sys.stderr)
        for p in ports:
            print(f"  - {p}", file=sys.stderr)
        sys.exit(1)
    idx = pick_from_list("select MIDI output port:", ports)
    return ports[idx]


SUBTYPE = {16: "PCM_16", 24: "PCM_24", 32: "FLOAT"}


def record_window(audio_device: int, channels: list[int], sample_rate: int, dur_sec: float):
    n = int(dur_sec * sample_rate)
    return sd.rec(
        n, samplerate=sample_rate, channels=len(channels), device=audio_device,
        mapping=channels, dtype="float32", blocking=True,
    )


def record_with_midi_trigger(
    audio_device: int, channels: list[int], sample_rate: int,
    midi_port, midi_channel: int, note: int, velocity: int,
    record_sec: float, pre_roll: float = 0.05,
):
    total = pre_roll + record_sec
    n = int(total * sample_rate)
    rec = sd.rec(
        n, samplerate=sample_rate, channels=len(channels), device=audio_device,
        mapping=channels, dtype="float32", blocking=False,
    )
    time.sleep(pre_roll)
    midi_port.send(mido.Message("note_on", note=note, velocity=velocity, channel=midi_channel - 1))
    # Most drum machines retrigger on note-on; short note-off after 50ms is plenty
    time.sleep(0.05)
    midi_port.send(mido.Message("note_off", note=note, velocity=0, channel=midi_channel - 1))
    sd.wait()
    return rec


def all_notes_off(midi_port, midi_channel: int):
    midi_port.send(mido.Message("control_change", control=123, value=0, channel=midi_channel - 1))


def velocity_for_variant(variant_n: int, total_variants: int, vel_range=(40, 127)) -> int:
    if total_variants == 1:
        return vel_range[1]
    lo, hi = vel_range
    return int(lo + (hi - lo) * (variant_n - 1) / (total_variants - 1))


def record_sec_for_voice(voice: str, default: float) -> float:
    return VOICE_RECORD_OVERRIDE.get(voice, default)


# ---------- main ----------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="thespacepit drum-machine capture tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # core
    ap.add_argument("--instrument", required=True, help="instrument slug (e.g. sp-1200, tr-808)")
    ap.add_argument("--kit", required=True, help="kit name (e.g. factory, ny-grit). Saves to instruments/<slug>/kits/<kit>/raw/")
    ap.add_argument("--bank-root", default=None, help="path to spacepit-sample-bank root (auto-detected)")
    # matrix
    ap.add_argument("--tier", choices=list(TIER_MATRICES.keys()), default=None, help="override manifest tier")
    ap.add_argument("--voice-list", default=None, help="override matrix, format 'kick:5,snare:5,hh-closed:3'")
    # mode
    ap.add_argument("--mode", choices=["manual", "midi"], default="manual", help="manual = you trigger, midi = tool sends notes")
    ap.add_argument("--record-sec", type=float, default=DEFAULT_RECORD_SEC, help="default record window per hit (sec)")
    ap.add_argument("--velocity-range", default="40,127", help="MIDI mode: velocity range to sweep variants across, e.g. '40,127'")
    ap.add_argument("--velocity", type=int, default=None, help="MIDI mode: fixed velocity instead of sweep")
    # ports / audio
    ap.add_argument("--midi-port", default=None, help="MIDI output port (midi mode only)")
    ap.add_argument("--midi-channel", type=int, default=10, help="MIDI channel (drum machines usually ch 10)")
    ap.add_argument("--audio-device", default=None, help="audio input device name (substring)")
    ap.add_argument("--input-channels", default="1,2")
    ap.add_argument("--sample-rate", type=int, default=48000)
    ap.add_argument("--bit-depth", type=int, choices=[16, 24, 32], default=24)
    # modes
    ap.add_argument("--dry-run", action="store_true", help="print plan, don't capture")
    ap.add_argument("--resume", action="store_true", help="skip files that already exist")
    ap.add_argument("-y", "--yes", action="store_true", help="skip the press-enter confirmation")
    args = ap.parse_args()

    # input channels
    try:
        input_channels = [int(c) for c in args.input_channels.split(",")]
    except ValueError:
        print(f"bad --input-channels: {args.input_channels}", file=sys.stderr)
        sys.exit(1)

    # velocity range
    try:
        vlo, vhi = (int(x) for x in args.velocity_range.split(","))
        velocity_range = (vlo, vhi)
    except ValueError:
        print(f"bad --velocity-range: {args.velocity_range}", file=sys.stderr)
        sys.exit(1)

    # find bank + instrument
    bank_root = Path(args.bank_root).resolve() if args.bank_root else find_bank_root()
    if not bank_root:
        print("error: couldn't find spacepit-sample-bank root.", file=sys.stderr)
        sys.exit(1)
    instr_dir = bank_root / "instruments" / args.instrument
    if not instr_dir.exists():
        print(f"instrument folder doesn't exist: {instr_dir}", file=sys.stderr)
        sys.exit(1)
    manifest = json.loads((instr_dir / "manifest.json").read_text())

    # tier + matrix resolution
    tier = args.tier or manifest.get("tier") or "standard"
    if tier not in TIER_MATRICES:
        print(f"unknown tier '{tier}'. falling back to 'standard'", file=sys.stderr)
        tier = "standard"
    matrix = TIER_MATRICES[tier]
    if args.voice_list:
        matrix = parse_voice_list(args.voice_list)

    # voice map (for midi mode)
    voice_map = dict(GM_DRUM_MAP)
    if isinstance(manifest.get("voice_map"), dict):
        voice_map.update({k: int(v) for k, v in manifest["voice_map"].items()})

    # expanded plan
    expanded = []
    for voice, count in matrix:
        for n in range(1, count + 1):
            expanded.append((voice, n, count))
    total = len(expanded)

    # output dir
    kit_dir = instr_dir / "kits" / args.kit / "raw"
    kit_dir.mkdir(parents=True, exist_ok=True)

    slug_flat = args.instrument.replace("-", "")

    # plan summary
    est_per = args.record_sec + 1.5  # rough avg incl. user input + saving
    print("\n=== drum-machine capture ===")
    print(f"  instrument:  {manifest.get('name', args.instrument)}  ({args.instrument})")
    print(f"  kit:         {args.kit}")
    print(f"  tier:        {tier}")
    print(f"  mode:        {args.mode}")
    print(f"  matrix:")
    for voice, count in matrix:
        rs = record_sec_for_voice(voice, args.record_sec)
        midi_note = voice_map.get(voice)
        midi_hint = f" → MIDI {midi_note}" if args.mode == "midi" and midi_note is not None else ""
        miss = " (NOT IN VOICE MAP, will skip in midi mode)" if args.mode == "midi" and midi_note is None else ""
        print(f"    {voice:>12s}  × {count:<2d}  ({rs:.1f}s/hit){midi_hint}{miss}")
    print(f"  total hits:  {total}")
    print(f"  est. time:   ~{(total * est_per)/60:.1f} min")
    print(f"  output:      {kit_dir}")

    if args.dry_run:
        print("\n[dry-run] no audio captured.")
        return

    # resolve ports
    audio_dev = resolve_audio_device(args.audio_device)
    dev_info = sd.query_devices(audio_dev)
    print(f"\n  audio:       {dev_info['name']}, channels {input_channels} @ {args.sample_rate}Hz / {args.bit_depth}-bit")
    midi_port = None
    midi_port_name = None
    if args.mode == "midi":
        midi_port_name = resolve_midi_port(args.midi_port)
        print(f"  MIDI port:   {midi_port_name}  (channel {args.midi_channel})")

    if int(dev_info["default_samplerate"]) != args.sample_rate:
        print(f"  ⚠ device default rate is {int(dev_info['default_samplerate'])} — if capture fails, retry with --sample-rate {int(dev_info['default_samplerate'])}")

    # confirm
    if not args.yes:
        print("\n→ press enter to begin, ctrl-c to abort")
        try:
            input()
        except KeyboardInterrupt:
            print("\naborted.")
            return

    # open MIDI if needed
    if args.mode == "midi":
        midi_port = mido.open_output(midi_port_name)
        all_notes_off(midi_port, args.midi_channel)
        time.sleep(0.1)

    subtype = SUBTYPE[args.bit_depth]
    done = skipped = failed = 0
    t_start = time.time()

    try:
        i = 0
        while i < len(expanded):
            voice, variant_n, voice_total = expanded[i]
            letter = variant_letter(variant_n)
            fname = f"{slug_flat}_{voice}_{letter}.wav"
            fpath = kit_dir / fname
            progress = f"[{i+1:>3}/{total}]"

            if args.resume and fpath.exists():
                print(f"  {progress} skip   {fname}")
                skipped += 1
                i += 1
                continue

            rec_sec = record_sec_for_voice(voice, args.record_sec)

            # ---- MANUAL MODE ----
            if args.mode == "manual":
                print(f"\n  {progress} {voice} {variant_n}/{voice_total} → {fname}  ({rec_sec:.1f}s window)")
                action = input("    enter=record · s=skip · r=redo last · b=back · q=quit: ").strip().lower()
                if action == "q":
                    break
                if action == "s":
                    skipped += 1
                    i += 1
                    continue
                if action == "b":
                    i = max(0, i - 1)
                    continue
                if action == "r":
                    i = max(0, i - 1)
                    continue
                print(f"    recording for {rec_sec:.1f}s — hit the pad ...")
                rec = record_window(audio_dev, input_channels, args.sample_rate, rec_sec)

            # ---- MIDI MODE ----
            else:
                if voice not in voice_map:
                    print(f"  {progress} {voice} → no voice_map entry, skip")
                    skipped += 1
                    i += 1
                    continue
                note = voice_map[voice]
                if args.velocity is not None:
                    vel = args.velocity
                else:
                    vel = velocity_for_variant(variant_n, voice_total, velocity_range)
                print(f"  {progress} {voice} {variant_n}/{voice_total} → {fname}  (MIDI {note} vel {vel}, {rec_sec:.1f}s)")
                rec = record_with_midi_trigger(
                    audio_dev, input_channels, args.sample_rate,
                    midi_port, args.midi_channel, note, vel, rec_sec,
                )

            # save + report
            try:
                peak = float(np.max(np.abs(rec)))
                peak_db = 20 * np.log10(max(1e-10, peak))
                sf.write(str(fpath), rec, args.sample_rate, subtype=subtype)
                tag = " ⚠clip" if peak >= 0.99 else f" {peak_db:+.1f}dBFS"
                if peak < 0.001:
                    tag = " ⚠silent (did you hit it?)"
                print(f"    ✓ saved{tag}")
                done += 1
            except Exception as e:
                print(f"    ✗ save failed: {e}")
                failed += 1

            i += 1

    except KeyboardInterrupt:
        print("\n\ninterrupted.")
    finally:
        if midi_port is not None:
            all_notes_off(midi_port, args.midi_channel)
            midi_port.close()

    elapsed = time.time() - t_start
    print(f"\n✓ {done} captured · {skipped} skipped · {failed} failed · {elapsed/60:.1f} min")
    print(f"  → {kit_dir}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nbye.")
