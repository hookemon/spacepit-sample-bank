#!/usr/bin/env python3
"""
thespacepit master sample bank — chromatic capture tool

Automates multi-sampling of hardware synths. Sends MIDI notes, records audio,
names + saves WAVs per the bank spec. Saves a 4-hour click-fest.

Usage examples:
  uv run capture.py --instrument ms-20 --patch lead01
  uv run capture.py --instrument ms-20 --patch lead01 --tier quick --dry-run
  uv run capture.py --instrument ms-20 --patch lead01 --note-range C2-C6 --step 2
  uv run capture.py --instrument ms-20 --patch lead01 --resume
  uv run capture.py --meter --audio-device "Apogee" --meter-duration 5

See README.md in this folder for the full guide.
"""
from __future__ import annotations

import argparse
import json
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
    print(f"missing dep: {e}\n  run:  uv sync   (in tools/capture/)", file=sys.stderr)
    sys.exit(1)


# ---------- tier defaults ----------

TIER_DEFAULTS = {
    "quick": {
        "range": ("C2", "C5"),
        "step": 6,
        "velocities": [100],
        "rr": 1,
        "sustain": 2.0,
        "tail": 1.0,
    },
    "standard": {
        "range": ("C1", "C6"),
        "step": 3,
        "velocities": [40, 80, 120],
        "rr": 1,
        "sustain": 3.0,
        "tail": 1.5,
    },
    "gold": {
        "range": ("C1", "C7"),
        "step": 1,
        "velocities": [20, 50, 80, 100, 127],
        "rr": 2,
        "sustain": 4.0,
        "tail": 2.0,
    },
}

# ---------- note name helpers ----------

NOTE_NAMES = ["c", "cs", "d", "ds", "e", "f", "fs", "g", "gs", "a", "as", "b"]


def note_name_to_midi(name: str) -> int:
    """Scientific pitch — C-1 = MIDI 0, C4 = MIDI 60 (middle C), G9 = MIDI 127.
    Accepts sharps ('s' or '#') and flats ('b'). e.g. C4, Cs4, C#4, Eb3, Bb2."""
    m = re.match(r"^([a-g])(s|#|b)?(-?\d+)$", name.lower())
    if not m:
        raise ValueError(f"bad note name: {name}  (expected like C4, Cs4, Eb3, Bb2)")
    letter, accidental, octave = m.groups()
    base = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}[letter]
    if accidental in ("s", "#"):
        base += 1
    elif accidental == "b":
        base -= 1
    return base + (int(octave) + 1) * 12


def midi_to_note_name(midi: int, octave_offset: int = 0) -> str:
    """Reverse of above. Lowercase, sharps as 's'. e.g. 60 -> 'c4', 61 -> 'cs4'."""
    octave = (midi // 12) - 1 + octave_offset
    pc = midi % 12
    return f"{NOTE_NAMES[pc]}{octave}"


# ---------- bank discovery ----------

def find_bank_root(start: Path | None = None) -> Path | None:
    """Walk up from start (or cwd) looking for a dir with instruments/ + manifest.template.json."""
    p = (start or Path.cwd()).resolve()
    for candidate in [p, *p.parents]:
        if (candidate / "instruments").is_dir() and (candidate / "manifest.template.json").exists():
            return candidate
    # also try a known absolute fallback
    fallback = Path.home() / "projects" / "spacepit-sample-bank"
    if (fallback / "instruments").is_dir():
        return fallback
    return None


# ---------- interactive pickers ----------

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


def resolve_midi_port(arg: str | None) -> str:
    ports = mido.get_output_names()
    if not ports:
        print("error: no MIDI output ports found. plug in your synth / MIDI interface.", file=sys.stderr)
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


# ---------- bit-depth → soundfile subtype ----------

SUBTYPE = {16: "PCM_16", 24: "PCM_24", 32: "FLOAT"}


# ---------- meter mode (peak level check) ----------

def run_meter(audio_device: int, channels: list[int], sample_rate: int, duration: float) -> None:
    print(f"\nrecording {duration}s for peak check on device #{audio_device}, channels {channels}...")
    n = int(duration * sample_rate)
    rec = sd.rec(
        n, samplerate=sample_rate, channels=len(channels), device=audio_device,
        mapping=channels, dtype="float32", blocking=True,
        blocksize=4096, latency="high",
    )
    peak = float(np.max(np.abs(rec)))
    if peak == 0:
        print("  peak: -inf dBFS  (silence — is the synth on? cable in the right input?)")
        return
    peak_db = 20 * np.log10(peak)
    if peak_db > -3:
        note = "  ⚠ HOT — lower input gain"
    elif peak_db < -18:
        note = "  ↑ quiet — could raise gain"
    elif peak_db < -6:
        note = "  ✓ headroom looks good"
    else:
        note = "  ~ acceptable, watch transients"
    print(f"  peak: {peak_db:+.1f} dBFS{note}")


# ---------- the actual capture ----------

def capture_one(
    midi_port,
    midi_channel: int,
    audio_device: int,
    channels: list[int],
    sample_rate: int,
    note: int,
    velocity: int,
    sustain: float,
    tail: float,
    pre_roll: float = 0.05,
    blocksize: int = 4096,
):
    total_dur = pre_roll + sustain + tail
    n = int(total_dur * sample_rate)
    rec_kw = dict(
        samplerate=sample_rate, channels=len(channels), device=audio_device,
        mapping=channels, dtype="float32", blocking=False, latency="high",
        blocksize=max(512, int(blocksize)),     # floor 512 — Nick's call
    )
    rec = sd.rec(n, **rec_kw)
    time.sleep(pre_roll)
    midi_port.send(mido.Message("note_on", note=note, velocity=velocity, channel=midi_channel - 1))
    time.sleep(sustain)
    midi_port.send(mido.Message("note_off", note=note, velocity=0, channel=midi_channel - 1))
    time.sleep(tail)
    sd.wait()
    return rec


def all_notes_off(midi_port, midi_channel: int) -> None:
    """Safety — All Notes Off (CC 123) on the channel."""
    midi_port.send(mido.Message("control_change", control=123, value=0, channel=midi_channel - 1))


# ---------- main ----------

def _post_clean_dir(directory: Path, gain_db: float = 3.0) -> int:
    """Trim dead air + clip-safe make-up gain on every WAV under `directory`.

    Reuses clean_one() from tools/pack/clean-wavs.py so EVERY capture that comes
    out of the bench (app queue, app single-capture, or CLI) is already trimmed
    and hot — no separate manual clean step. Returns the count cleaned.
    """
    import importlib.util
    cw = Path(__file__).resolve().parent.parent / "pack" / "clean-wavs.py"
    spec = importlib.util.spec_from_file_location("_clean_wavs", cw)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    n = 0
    for wav in sorted(directory.rglob("*.wav")):
        # target_peak_db=None → no per-file normalize (keep natural dynamics);
        # gain_db → uniform clip-safe make-up gain so it opens hot.
        mod.clean_one(wav, target_peak_db=None, gain_db=gain_db)
        n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser(
        description="thespacepit chromatic capture tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # core
    ap.add_argument("--instrument", help="instrument slug (e.g. ms-20). reads instruments/<slug>/manifest.json")
    ap.add_argument("--patch", help="patch name (e.g. lead01). writes to instruments/<slug>/patches/<patch>/<chain>/")
    ap.add_argument("--chain", default="raw", help="output subfolder under patches/<patch>/ — e.g. 'raw', 'spring', 'processed'. default 'raw'")
    ap.add_argument("--bank-root", default=None, help="path to spacepit-sample-bank root (auto-detected)")
    # tier + overrides
    ap.add_argument("--tier", choices=list(TIER_DEFAULTS.keys()), default=None, help="override manifest tier")
    ap.add_argument("--note-range", default=None, help="e.g. C1-C7  (scientific pitch — C4 = middle C)")
    ap.add_argument("--step", type=int, default=None, help="semitone step (1, 3, 6)")
    ap.add_argument("--velocities", default=None, help="comma-separated MIDI vel values e.g. 40,80,120")
    ap.add_argument("--round-robins", type=int, default=None, help="takes per (note, velocity)")
    ap.add_argument("--sustain-sec", type=float, default=None, help="hold note for N seconds")
    ap.add_argument("--tail-sec", type=float, default=None, help="record N seconds after note-off for release tail")
    ap.add_argument("--octave-offset", type=int, default=0,
                    help="0 = scientific (C4 = middle C). use -1 for Ableton-style display (C3 = middle C)")
    # ports / audio
    ap.add_argument("--midi-port", default=None, help="MIDI output port name (exact or substring)")
    ap.add_argument("--midi-channel", type=int, default=1, help="MIDI channel 1-16")
    ap.add_argument("--audio-device", default=None, help="audio input device name (substring)")
    ap.add_argument("--input-channels", default="1,2", help="1-based input channels e.g. '1,2' or '5,6'")
    ap.add_argument("--sample-rate", type=int, default=48000)
    ap.add_argument("--bit-depth", type=int, choices=[16, 24, 32], default=24)
    ap.add_argument("--blocksize", type=int, default=512,
                    help="audio buffer in samples (min 512). Big enough to avoid dropout "
                         "clicks, small enough to keep note-on timing tight. Clamped to >=512.")
    ap.add_argument("--take", action="store_true",
                    help="CONTINUOUS take mode: record ONE unbroken WAV while firing all notes, "
                         "then slice it into per-note files (like recording in Ableton + slicing). "
                         "Avoids the per-note start/stop that can drop notes.")
    # modes
    ap.add_argument("--dry-run", action="store_true", help="print plan, don't capture")
    ap.add_argument("--resume", action="store_true", help="skip files that already exist")
    ap.add_argument("--meter", action="store_true", help="just measure peak level for N sec, no MIDI")
    ap.add_argument("--meter-duration", type=float, default=5.0)
    ap.add_argument("-y", "--yes", action="store_true", help="skip the press-enter confirmation")
    ap.add_argument("--gain-db", type=float, default=3.0,
                    help="make-up gain (dB) in the post-clean pass, clip-safe. default +3 so it opens hot")
    ap.add_argument("--no-post-clean", action="store_true",
                    help="skip the auto trim + gain pass (keep raw WAVs exactly as recorded)")
    # Program Change + CC sweep automation (the encyclopedia move)
    ap.add_argument("--program-change", type=int, default=None,
                    help="MIDI Program Change number (0-127) to send before capturing. Synth auto-loads the preset.")
    ap.add_argument("--bank-msb", type=int, default=None,
                    help="MIDI Bank Select MSB (CC 0) sent before Program Change. Required for some synths to address bank+preset.")
    ap.add_argument("--bank-lsb", type=int, default=None,
                    help="MIDI Bank Select LSB (CC 32) sent before Program Change.")
    ap.add_argument("--pc-settle-ms", type=float, default=300,
                    help="ms to wait after Program Change for the synth to load the preset (default 300)")
    ap.add_argument("--cc-sweep", default=None,
                    help='Optional CC sweep, format "CC:val1,val2,...". E.g. "74:30,60,90" captures the full multisample 3 times — once at filter cutoff 30, then 60, then 90. WAVs land in <chain>/cc<num>_<val>/.')
    args = ap.parse_args()

    # parse input channels
    try:
        input_channels = [int(c) for c in args.input_channels.split(",")]
    except ValueError:
        print(f"bad --input-channels: {args.input_channels}", file=sys.stderr)
        sys.exit(1)

    # meter mode — short-circuit
    if args.meter:
        dev = resolve_audio_device(args.audio_device)
        run_meter(dev, input_channels, args.sample_rate, args.meter_duration)
        return

    # require instrument + patch for capture
    if not args.instrument or not args.patch:
        print("error: --instrument and --patch required (use --meter to just check input level)", file=sys.stderr)
        sys.exit(1)

    # find bank root
    bank_root = Path(args.bank_root).resolve() if args.bank_root else find_bank_root()
    if not bank_root:
        print("error: couldn't find spacepit-sample-bank root. pass --bank-root or run from inside the bank dir.", file=sys.stderr)
        sys.exit(1)

    # instrument + manifest
    instr_dir = bank_root / "instruments" / args.instrument
    if not instr_dir.exists():
        print(f"instrument folder doesn't exist: {instr_dir}", file=sys.stderr)
        print(f"  if this is new gear, create the folder + manifest first (see {bank_root}/README.md)", file=sys.stderr)
        sys.exit(1)
    manifest_path = instr_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"missing manifest: {manifest_path}", file=sys.stderr)
        sys.exit(1)
    manifest = json.loads(manifest_path.read_text())

    # resolve tier + overrides
    tier = args.tier or manifest.get("tier") or "standard"
    if tier not in TIER_DEFAULTS:
        print(f"unknown tier '{tier}' in manifest. falling back to 'standard'", file=sys.stderr)
        tier = "standard"
    cfg = {**TIER_DEFAULTS[tier]}
    if args.note_range:
        try:
            lo, hi = args.note_range.split("-")
            cfg["range"] = (lo, hi)
        except ValueError:
            print(f"bad --note-range: {args.note_range}. format C1-C7", file=sys.stderr)
            sys.exit(1)
    if args.step is not None:
        cfg["step"] = args.step
    if args.velocities:
        cfg["velocities"] = [int(v) for v in args.velocities.split(",")]
    if args.round_robins is not None:
        cfg["rr"] = args.round_robins
    if args.sustain_sec is not None:
        cfg["sustain"] = args.sustain_sec
    if args.tail_sec is not None:
        cfg["tail"] = args.tail_sec

    # compute note list
    lo_midi = note_name_to_midi(cfg["range"][0])
    hi_midi = note_name_to_midi(cfg["range"][1])
    notes = list(range(lo_midi, hi_midi + 1, cfg["step"]))

    # Parse CC sweep early so the plan output reflects the real file count
    cc_sweep_values = []
    if args.cc_sweep:
        try:
            _cc_num, _vals = args.cc_sweep.split(":", 1)
            cc_sweep_values = [int(v) for v in _vals.split(",")]
        except Exception:
            pass
    cc_multiplier = max(1, len(cc_sweep_values))

    per_state_count = len(notes) * len(cfg["velocities"]) * cfg["rr"]
    total = per_state_count * cc_multiplier
    sec_per = 0.05 + cfg["sustain"] + cfg["tail"] + 0.15  # pre-roll + sustain + tail + gap
    total_sec = total * sec_per

    # output dir
    patch_dir = instr_dir / "patches" / args.patch / args.chain
    patch_dir.mkdir(parents=True, exist_ok=True)

    # plan summary
    instr_slug_flat = args.instrument.replace("-", "")
    print("\n=== chromatic capture ===")
    print(f"  instrument:  {manifest.get('name', args.instrument)}  ({args.instrument})")
    print(f"  patch:       {args.patch}")
    print(f"  chain:       {args.chain}")
    print(f"  tier:        {tier}")
    print(f"  notes:       {len(notes)}  ({cfg['range'][0]} → {cfg['range'][1]}, step {cfg['step']})")
    print(f"  velocities:  {cfg['velocities']}")
    print(f"  round-robin: {cfg['rr']}")
    print(f"  sustain:     {cfg['sustain']}s")
    print(f"  tail:        {cfg['tail']}s")
    print(f"  total files: {total}" + (f"  ({per_state_count} per CC state × {cc_multiplier} states)" if cc_multiplier > 1 else ""))
    print(f"  est. time:   ~{total_sec/60:.1f} min")
    # Surface PC + CC plan so the user sees what'll fire BEFORE capture starts
    if args.program_change is not None:
        bank_str = ""
        if args.bank_msb is not None:
            bank_str = f" (bank MSB {args.bank_msb}" + (f" LSB {args.bank_lsb})" if args.bank_lsb is not None else ")")
        print(f"  prog change: PC {args.program_change}{bank_str}")
    if cc_sweep_values:
        _cc_num = args.cc_sweep.split(":")[0]
        print(f"  cc sweep:    CC{_cc_num} → {cc_sweep_values}  (multisample captured at each)")
    print(f"  output:      {patch_dir}")

    if args.dry_run:
        print("\n[dry-run] no audio captured.")
        return

    # resolve ports
    midi_port_name = resolve_midi_port(args.midi_port)
    audio_dev = resolve_audio_device(args.audio_device)
    dev_info = sd.query_devices(audio_dev)
    print(f"\n  MIDI port:   {midi_port_name}  (channel {args.midi_channel})")
    print(f"  audio:       {dev_info['name']}, channels {input_channels} @ {args.sample_rate}Hz / {args.bit_depth}-bit")
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

    # open MIDI
    midi_port = mido.open_output(midi_port_name)
    try:
        all_notes_off(midi_port, args.midi_channel)
        time.sleep(0.1)

        # Bank Select + Program Change — auto-load the synth's preset before capturing.
        # This is what makes the bench an "encyclopedia capture" tool: the schema
        # records WHICH preset was loaded, anyone can reproduce the exact capture.
        ch = args.midi_channel - 1  # 0-indexed for mido
        if args.bank_msb is not None:
            midi_port.send(mido.Message("control_change", control=0, value=args.bank_msb, channel=ch))
        if args.bank_lsb is not None:
            midi_port.send(mido.Message("control_change", control=32, value=args.bank_lsb, channel=ch))
        if args.program_change is not None:
            midi_port.send(mido.Message("program_change", program=args.program_change, channel=ch))
            print(f"  → sent Program Change {args.program_change}"
                  + (f" (bank MSB {args.bank_msb})" if args.bank_msb is not None else "")
                  + (f" (LSB {args.bank_lsb})" if args.bank_lsb is not None else ""))
            time.sleep(args.pc_settle_ms / 1000.0)

        # Optional CC sweep: parse "CC:val1,val2,..." → list of (cc_num, cc_value) states
        # If present, the multisample loop runs ONCE PER cc_value, writing into
        # <patch>/<chain>/cc<num>_<value>/ subdirs so the encyclopedia knows
        # "this WAV was captured with filter cutoff at 60".
        cc_states = [(None, None)]  # default: one state, no CC sent
        if args.cc_sweep:
            try:
                cc_num_str, vals_str = args.cc_sweep.split(":", 1)
                cc_num = int(cc_num_str)
                cc_values = [int(v) for v in vals_str.split(",")]
                cc_states = [(cc_num, v) for v in cc_values]
                print(f"  → CC sweep: CC{cc_num} → {cc_values}  (multisample captured at each state)")
            except Exception as _e:
                print(f"  ⚠ bad --cc-sweep value '{args.cc_sweep}', ignoring: {_e}")

        subtype = SUBTYPE[args.bit_depth]
        done = skipped = failed = 0
        t_start = time.time()

        # Pre-compute total adjusted for CC sweep multiplier
        per_state_total = len(notes) * len(cfg["velocities"]) * cfg["rr"]
        total = per_state_total * len(cc_states)

        # SILENCE GUARD — refuse to save WAVs with peak below this threshold (≈-46 dBFS).
        # 3 consecutive silent notes = abort the whole run (signal isn't reaching the bench).
        # Stops the "157 silent WAVs shipped" failure mode dead.
        SILENT_PEAK_THRESHOLD = 0.005   # 0.5% peak = ~-46 dBFS
        ABORT_AFTER_CONSECUTIVE_SILENT = 3
        consecutive_silent = 0
        silent_skipped = 0

        # ---------- CONTINUOUS TAKE MODE ----------
        # Record ONE unbroken WAV while firing every note in sequence, then slice it into
        # per-note files (slice-take.py). Same as recording in Ableton + slicing — avoids the
        # per-note start/stop that was dropping notes. One clean stream, no silence-abort loop.
        if args.take:
            sustain = cfg["sustain"]
            gap = max(2.0, cfg["tail"])           # ≥2s silence between notes so the slicer can split
            lead = 0.5
            total_dur = lead + len(notes) * (sustain + gap)
            n = int(total_dur * args.sample_rate)
            print(f"\n🎙 continuous take — {len(notes)} notes · {sustain:.0f}s hold + {gap:.0f}s gap · {total_dur:.0f}s total")
            rec = sd.rec(n, samplerate=args.sample_rate, channels=len(input_channels),
                         device=audio_dev, mapping=input_channels, dtype="float32",
                         blocking=False, latency="high", blocksize=max(512, args.blocksize))
            time.sleep(lead)
            v = cfg["velocities"][0]
            for i, note in enumerate(notes):
                ns = midi_to_note_name(note, args.octave_offset)
                print(f"  [{i+1:>2}/{len(notes)}] {ns}")
                midi_port.send(mido.Message("note_on", note=note, velocity=v, channel=args.midi_channel - 1))
                time.sleep(sustain)
                midi_port.send(mido.Message("note_off", note=note, velocity=0, channel=args.midi_channel - 1))
                time.sleep(gap)
            sd.wait()
            all_notes_off(midi_port, args.midi_channel)
            peak = float(np.max(np.abs(rec))) if rec.size else 0.0
            peak_db = 20 * np.log10(max(1e-10, peak))
            if peak < SILENT_PEAK_THRESHOLD:
                print(f"\n✗ TAKE SILENT ({peak_db:+.1f} dBFS) — no signal reached the recorder.")
                print(f"  This is a true signal problem (not per-note timing). Check synth → TX-6 ch {args.input_channels},")
                print(f"  the live meter, and that nothing else holds the device. (Or bounce from Ableton instead.)")
                raise SystemExit(2)
            patch_dir.mkdir(parents=True, exist_ok=True)
            take_path = patch_dir / f"_take_{args.patch}.wav"
            sf.write(str(take_path), rec, args.sample_rate, subtype=subtype)
            print(f"  ✓ take recorded — peak {peak_db:+.1f} dBFS → {take_path.name}")
            # slice it into per-note files
            import subprocess
            slicer = Path(__file__).resolve().parent.parent / "pack" / "slice-take.py"
            lo = midi_to_note_name(notes[0], args.octave_offset).upper()
            hi = midi_to_note_name(notes[-1], args.octave_offset).upper()
            step = (notes[1] - notes[0]) if len(notes) > 1 else 1
            cmd = [sys.executable, str(slicer), "--wav", str(take_path),
                   "--instrument", args.instrument, "--patch", args.patch, "--chain", args.chain,
                   "--start-note", lo, "--end-note", hi, "--step", str(step), "--velocity", str(v)]
            r = subprocess.run(cmd, capture_output=True, text=True)
            print(r.stdout)
            if r.returncode != 0:
                print(r.stderr)
                print("  ⚠ slicing didn't land all notes — the raw take is saved; adjust --gate-db and re-slice.")
            else:
                take_path.unlink(missing_ok=True)   # slices written; drop the big continuous take
            return

        for cc_num, cc_value in cc_states:
            # Apply this CC state (if any) then run the inner multisample loop
            if cc_num is not None and cc_value is not None:
                midi_port.send(mido.Message("control_change", control=cc_num, value=cc_value, channel=ch))
                time.sleep(0.05)
                print(f"\n  ── CC{cc_num} = {cc_value} ──")
                state_subdir = patch_dir / f"cc{cc_num}_{cc_value}"
                state_subdir.mkdir(parents=True, exist_ok=True)
                target_dir = state_subdir
            else:
                target_dir = patch_dir

            for note in notes:
                note_str = midi_to_note_name(note, args.octave_offset)
                for vel in cfg["velocities"]:
                    for rr in range(1, cfg["rr"] + 1):
                        progress = f"[{done + skipped + failed + 1:>3}/{total}]"
                        fname = f"{instr_slug_flat}_{args.patch}_{note_str}_v{vel:03d}_rr{rr}.wav"
                        fpath = target_dir / fname

                        if args.resume and fpath.exists():
                            print(f"  {progress} skip  {fname}")
                            skipped += 1
                            continue

                        print(f"  {progress} {fname}  ...", end="", flush=True)
                        try:
                            rec = capture_one(
                                midi_port=midi_port,
                                midi_channel=args.midi_channel,
                                audio_device=audio_dev,
                                channels=input_channels,
                                sample_rate=args.sample_rate,
                                note=note,
                                velocity=vel,
                                sustain=cfg["sustain"],
                                tail=cfg["tail"],
                                blocksize=args.blocksize,
                            )
                            peak = float(np.max(np.abs(rec)))
                            peak_db = 20 * np.log10(max(1e-10, peak))

                            # SILENCE GUARD — refuse to save WAVs with no signal.
                            # Protects against the "157 silent WAVs shipped" failure mode.
                            if peak < SILENT_PEAK_THRESHOLD:
                                consecutive_silent += 1
                                silent_skipped += 1
                                print(f" ⚠SILENT {peak_db:+.1f}dBFS — NOT SAVED")
                                # Abort the whole run if too many silent in a row
                                if consecutive_silent >= ABORT_AFTER_CONSECUTIVE_SILENT:
                                    print(f"\n✗ ABORT — {consecutive_silent} silent notes in a row.")
                                    print(f"  Signal isn't reaching the recorder. Check:")
                                    print(f"    - Synth volume knob (turn it up)")
                                    print(f"    - TX-6 channel + level matches --input-channels {args.input_channels}")
                                    print(f"    - Audio cable from synth → recorder")
                                    print(f"    - Use the bench's LIVE waveform to verify signal before re-running")
                                    raise RuntimeError(f"aborted — {consecutive_silent} consecutive silent captures")
                                # else: skip this note, continue
                                all_notes_off(midi_port, args.midi_channel)
                                time.sleep(0.1)
                                continue

                            # Audible — save it + reset the consecutive-silent counter
                            consecutive_silent = 0
                            sf.write(str(fpath), rec, args.sample_rate, subtype=subtype)
                            tag = " ⚠clip" if peak >= 0.99 else f" {peak_db:+.1f}dBFS"
                            print(f" done{tag}")
                            done += 1
                        except KeyboardInterrupt:
                            print(" interrupted")
                            raise
                        except RuntimeError:
                            # silence-abort already printed its diagnostic, bubble up
                            raise
                        except Exception as e:
                            print(f" failed: {e}")
                            failed += 1
                            all_notes_off(midi_port, args.midi_channel)
                            time.sleep(0.2)

        elapsed = time.time() - t_start
        summary = f"\n✓ {done} captured · {skipped} skipped · {failed} failed"
        if silent_skipped:
            summary += f" · {silent_skipped} ⚠silent-skipped"
        summary += f" · {elapsed/60:.1f} min"
        print(summary)
        print(f"  → {patch_dir}")

        # POST-CLEAN — trim dead air + clip-safe make-up gain so every capture
        # leaves the bench ready to drop into Ableton hot. On by default; the
        # app's queue + single-capture both inherit this automatically.
        if done > 0 and not args.no_post_clean:
            try:
                n = _post_clean_dir(patch_dir, gain_db=args.gain_db)
                print(f"  ✓ trimmed + {args.gain_db:+.0f}dB make-up on {n} file(s)")
            except Exception as _ce:
                print(f"  ⚠ post-clean skipped ({_ce}) — WAVs saved un-trimmed", file=sys.stderr)
    except KeyboardInterrupt:
        print("\n\ninterrupted. all notes off sent. partial captures saved.")
    except RuntimeError as _e:
        # Silence-abort or similar capture-side abort — diagnostic was already printed
        print(f"\n  capture aborted: {_e}", file=sys.stderr)
    finally:
        all_notes_off(midi_port, args.midi_channel)
        midi_port.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nbye.")
