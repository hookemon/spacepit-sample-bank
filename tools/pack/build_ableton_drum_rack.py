#!/usr/bin/env python3
"""Generate an Ableton Live 12 Drum Rack (.adg) from a folder of one-shot WAVs.

Takes a list of drum-element WAVs (kick.wav, snare.wav, hh-closed.wav, etc.),
slots each onto a pad (MIDI 36 = C1, 37 = C#1, ... 51 = D#2 for the standard
16-pad Move layout), and outputs a .adg that drops into Live's browser. From there
you can right-click the outer Rack → Export ABL Preset → upload to Move.

Each pad uses a DrumCell (Live 12's "Drum Sampler" — Move's required device).

Usage:
    python build_ableton_drum_rack.py --instrument tr-808 --kit classic
    python build_ableton_drum_rack.py --wavs-dir /path/to/drums --out my-kit.adg
"""
import argparse
import gzip
import re
import sys
from pathlib import Path
from typing import Optional

import soundfile as sf


# Standard GM-aligned 16-pad drum layout (matches Ableton's Drum Rack default).
# Pad 0 = MIDI 36 (C1, GM kick). Pad 15 = MIDI 51 (D#2). 16 pads total.
PAD_MIDI_START = 36

# Element name → preferred pad index. If your WAV is named "kick.wav" we put it
# on pad 0 (MIDI 36) by default. If your filename doesn't match, the next free
# pad gets used. Override-friendly per-instrument via --pad-map.
DEFAULT_ELEMENT_TO_PAD = {
    "kick":      0,   # 36 — C1
    "bd":        0,
    "kick2":     1,   # 37 — C#1
    "rim":       1,
    "snare":     2,   # 38 — D1
    "sd":        2,
    "clap":      3,   # 39 — D#1
    "tom-low":   4,   # 40 — E1
    "tom-mid":   5,   # 41 — F1
    "hh-closed": 6,   # 42 — F#1 (GM closed hi-hat)
    "ch":        6,
    "hh":        6,
    "tom-hi":    7,   # 43 — G1
    "hh-pedal":  8,   # 44 — G#1 (GM pedal hi-hat)
    "ph":        8,
    "hh-open":   10,  # 46 — A#1 (GM open hi-hat)
    "oh":        10,
    "tom":       9,   # 45 — A1
    "crash":     12,  # 48 — C2
    "ride":      13,  # 49 — C#2
    "ride-bell": 14,  # 50 — D2
    "cowbell":   15,  # 51 — D#2
    "perc":      11,  # 47 — B1
    "shaker":    11,
    "tamb":      11,
    "tambourine": 11,
}


def parse_element_from_filename(fname: str) -> Optional[str]:
    """Try to detect the drum element from a filename — match against DEFAULT_ELEMENT_TO_PAD keys."""
    stem = Path(fname).stem.lower()
    # Try direct match first (longest key first to favor 'hh-closed' over 'hh')
    keys_by_length = sorted(DEFAULT_ELEMENT_TO_PAD.keys(), key=len, reverse=True)
    for k in keys_by_length:
        if k in stem:
            return k
    return None


def midi_to_label(m: int) -> str:
    """Ableton-style label (C-1 to G8). Used in pad names + log output."""
    names = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
    return f"{names[m % 12]}{(m // 12) - 2}"


def update_pad(pad_xml: str, pad_index: int, drum_name: str, midi_note: int,
               wav_path: Path, sample_count: int, sample_rate: int) -> str:
    """Take the template pad0 block and substitute the per-pad values.

    The pad has multiple <Path> / <RelativePath> elements: one in the
    AbletonDefaultPresetRef (the device-preset reference, empty) and one in the
    SampleRef (the actual audio file). We surgically target the SampleRef block
    so we don't accidentally overwrite the preset ref.
    """
    abs_path = str(wav_path.resolve())
    rel_path = f"Samples/{wav_path.name}"
    file_size = wav_path.stat().st_size
    end_sample = max(0, sample_count - 1)

    # Pad-level: ID + name + receiving note (these are unique inside the pad)
    pad_xml = re.sub(r'<DrumBranchPreset Id="\d+">', f'<DrumBranchPreset Id="{pad_index}">', pad_xml, count=1)
    pad_xml = re.sub(r'<Name Value="[^"]*" />', f'<Name Value="{drum_name}" />', pad_xml, count=1)
    pad_xml = re.sub(r'<ReceivingNote Value="\d+" />', f'<ReceivingNote Value="{midi_note}" />', pad_xml, count=1)

    # Sample-specific fields — only target the SampleRef block, which is bounded
    # by <SampleRef ...> ... </SampleRef>. Replace just inside that bounded region.
    def sub_inside_sampleref(text: str, pattern: str, repl: str) -> str:
        m = re.search(r'<SampleRef[^>]*>.*?</SampleRef>', text, re.DOTALL)
        if not m:
            return text
        block = m.group(0)
        new_block = re.sub(pattern, repl, block, count=1)
        return text[:m.start()] + new_block + text[m.end():]

    pad_xml = sub_inside_sampleref(pad_xml, r'<RelativePath Value="[^"]*" />', f'<RelativePath Value="{rel_path}" />')
    pad_xml = sub_inside_sampleref(pad_xml, r'<Path Value="[^"]*" />', f'<Path Value="{abs_path}" />')
    pad_xml = sub_inside_sampleref(pad_xml, r'<OriginalFileSize Value="\d+" />', f'<OriginalFileSize Value="{file_size}" />')
    pad_xml = sub_inside_sampleref(pad_xml, r'<DefaultDuration Value="\d+" />', f'<DefaultDuration Value="{sample_count}" />')
    pad_xml = sub_inside_sampleref(pad_xml, r'<DefaultSampleRate Value="\d+" />', f'<DefaultSampleRate Value="{sample_rate}" />')
    pad_xml = sub_inside_sampleref(pad_xml, r'<OriginalCrc Value="\d+" />', '<OriginalCrc Value="0" />')

    # SampleEnd + loop ends live in the DrumCell, NOT in the SampleRef. They're unique
    # in the pad (only one DrumCell per pad), so the regular count=1 sub works.
    pad_xml = re.sub(r'<SampleEnd Value="\d+" />', f'<SampleEnd Value="{end_sample}" />', pad_xml, count=1)
    pad_xml = re.sub(r'(<SustainLoop>\s*<Start Value="0" />\s*<End Value=")\d+',
                     r'\g<1>' + str(end_sample), pad_xml, count=1)
    pad_xml = re.sub(r'(<ReleaseLoop>\s*<Start Value="0" />\s*<End Value=")\d+',
                     r'\g<1>' + str(end_sample), pad_xml, count=1)
    return pad_xml


def extract_pad_template(template_xml: str) -> str:
    """Pull out the first DrumBranchPreset block as a reusable string template."""
    start = template_xml.find('<DrumBranchPreset ')
    if start == -1:
        raise ValueError("template doesn't contain a DrumBranchPreset")
    # Manually find matching close tag (handle possible nesting)
    depth = 0
    pos = start
    while pos < len(template_xml):
        open_m = re.search(r'<DrumBranchPreset[\s>]', template_xml[pos:])
        close_m = re.search(r'</DrumBranchPreset>', template_xml[pos:])
        if not close_m:
            break
        next_open = open_m.start() if open_m else 10**9
        next_close = close_m.start()
        if next_open < next_close:
            depth += 1
            pos += next_open + 1
        else:
            depth -= 1
            pos += next_close + len('</DrumBranchPreset>')
            if depth == 0:
                return template_xml[start:pos]
    raise ValueError("couldn't find matching </DrumBranchPreset>")


def build_drum_rack_from_wavs(
    wavs: list,
    out_path: Path,
    template_path: Optional[Path] = None,
    pad_map: Optional[dict] = None,
) -> dict:
    """Build a .adg Drum Rack from a list of WAV paths. Returns stats dict."""
    if template_path is None:
        template_path = Path(__file__).resolve().parent / "templates" / "ableton-drum-rack-template.xml"
    if not template_path.exists():
        raise FileNotFoundError(f"template missing: {template_path}")

    template_xml = template_path.read_text()
    pad_template = extract_pad_template(template_xml)

    # Decide each WAV's pad index
    assignments = []  # list of (pad_index, name, wav, frames, sr)
    used_pads = set()
    pad_map = pad_map or DEFAULT_ELEMENT_TO_PAD
    leftover_wavs = []

    for w in wavs:
        w = Path(w)
        info = sf.info(str(w))
        element = parse_element_from_filename(w.name)
        if element and element in pad_map and pad_map[element] not in used_pads:
            pad = pad_map[element]
            name = element
            used_pads.add(pad)
            assignments.append((pad, name, w, info.frames, info.samplerate))
        else:
            leftover_wavs.append((w, info))

    # Fill any leftovers into next free pad index
    next_pad = 0
    for w, info in leftover_wavs:
        while next_pad in used_pads and next_pad < 16:
            next_pad += 1
        if next_pad >= 16:
            print(f"  ⚠ {w.name}: no free pad — skipped")
            continue
        used_pads.add(next_pad)
        name = w.stem
        assignments.append((next_pad, name, w, info.frames, info.samplerate))

    if not assignments:
        return {"written": False, "error": "no pads assigned"}

    # Sort by pad index so the resulting XML has pads in order
    assignments.sort(key=lambda a: a[0])

    # Build the new BranchPresets content
    pad_blocks = []
    for pad_index, name, wav, frames, sr in assignments:
        midi_note = PAD_MIDI_START + pad_index
        block = update_pad(pad_template, pad_index, name, midi_note, wav, frames, sr)
        pad_blocks.append(block)

    new_branches = "\n							".join(pad_blocks)

    # Splice into the template's INNER BranchPresets (the one containing DrumBranchPresets).
    # The outer Instrument Rack has its own BranchPresets > InstrumentBranchPreset wrapping
    # the Drum Rack. The INNER BranchPresets is the one we want to replace contents of.
    # We find the inner one by looking for <BranchPresets> containing DrumBranchPreset.
    pattern = re.compile(
        r'<BranchPresets>\s*<DrumBranchPreset.*?</BranchPresets>',
        re.DOTALL,
    )
    replacement = f"<BranchPresets>\n							{new_branches}\n						</BranchPresets>"
    new_xml, n_sub = pattern.subn(replacement, template_xml, count=1)
    if n_sub == 0:
        return {"written": False, "error": "couldn't find inner BranchPresets to splice"}

    adv_bytes = gzip.compress(new_xml.encode("utf-8"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(adv_bytes)

    return {
        "written": True,
        "out_path": out_path,
        "pad_count": len(assignments),
        "size": len(adv_bytes),
        "assignments": [(p, n, str(w)) for p, n, w, _, _ in assignments],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wavs-dir", required=True, help="folder of one-shot WAVs")
    ap.add_argument("--out", default=None, help="output .adg path")
    ap.add_argument("--kit-name", default="drum-kit", help="name for the output preset (used in default --out)")
    ap.add_argument("--template", default=None, help="override template path")
    args = ap.parse_args()

    wavs_dir = Path(args.wavs_dir).resolve()
    if not wavs_dir.exists():
        print(f"folder not found: {wavs_dir}", file=sys.stderr); sys.exit(1)
    wavs = sorted(wavs_dir.glob("*.wav"))
    if not wavs:
        print(f"no WAVs in {wavs_dir}", file=sys.stderr); sys.exit(1)

    print(f"Found {len(wavs)} WAVs in {wavs_dir.name}")
    for w in wavs[:16]:
        print(f"  · {w.name}")
    if len(wavs) > 16:
        print(f"  (+ {len(wavs) - 16} more — only 16 will fit on the rack)")

    out_path = Path(args.out) if args.out else (Path.cwd() / f"{args.kit_name}.adg")
    template = Path(args.template) if args.template else None
    result = build_drum_rack_from_wavs(wavs, out_path, template_path=template)

    if not result.get("written"):
        print(f"\n✗ failed: {result.get('error')}", file=sys.stderr); sys.exit(1)

    print(f"\n✓ wrote {result['out_path']} ({result['size']} bytes)")
    print(f"  {result['pad_count']} pads mapped")
    print(f"\n  pad map:")
    for pad, name, wav_path in result['assignments']:
        print(f"    pad {pad:>2}  MIDI {PAD_MIDI_START + pad}  {midi_to_label(PAD_MIDI_START + pad):>4}  {name:<14}  {Path(wav_path).name}")

    print(f"\n→ drag {Path(result['out_path']).name} into Live's browser to test.")
    print(f"  Right-click outer Rack → Export ABL Preset → upload to Move.")


if __name__ == "__main__":
    main()
