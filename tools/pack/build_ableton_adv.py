#!/usr/bin/env python3
"""Generate an Ableton Live 12 Sampler preset (.adv) from captured WAVs.

Reads the captured multisamples for a patch, parses the note from each filename,
and writes a .adv with each sample placed at its correct root note + key zones
evenly distributed between roots. Drop the resulting .adv into Ableton Live's
browser and play — it works out of the box, no manual root remapping.

Usage:
    python build_ableton_adv.py --instrument grandmother --patch hookesquelch-bite
    python build_ableton_adv.py --instrument grandmother --patch moogbass --out /custom/path.adv
"""
import argparse
import gzip
import re
import sys
import textwrap
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile as sf


# Standard MIDI note numbering:
#   C-1 = 0, C0 = 12, C2 = 36, C3 = 48, C4 (middle C) = 60, A4 = 69
# Ableton labels these as one octave lower (C3 = middle C in Ableton's display).
NOTE_NAMES = ["c", "c#", "d", "d#", "e", "f", "f#", "g", "g#", "a", "a#", "b"]
# Our captured filenames use 'cs', 'ds', 'fs', 'gs', 'as' for sharps (no #)
SHARP_ALIASES = {"cs": "c#", "ds": "d#", "fs": "f#", "gs": "g#", "as": "a#"}


def note_to_midi(name: str) -> int:
    """Convert 'a3' or 'ds2' (D#2) or 'c#4' to MIDI note number."""
    name = name.strip().lower()
    # Handle 'ds', 'fs' style sharps
    for k, v in SHARP_ALIASES.items():
        if name.startswith(k):
            name = v + name[len(k):]
            break
    if "#" in name:
        letter_part, oct_part = name[:2], name[2:]
    else:
        letter_part, oct_part = name[:1], name[1:]
    base = NOTE_NAMES.index(letter_part)
    octave = int(oct_part)
    # MIDI standard: C-1 = 0, C0 = 12. So octave N starts at (octave+1)*12.
    return (octave + 1) * 12 + base


def parse_note_from_filename(fname: str) -> Optional[int]:
    """Pull the MIDI note from a filename like 'grandmother_hookesquelch-bite_a3_v100_rr1.wav'.

    Looks for the note token between the patch name and the velocity marker.
    """
    # match patterns like '_a3_', '_c#4_', '_ds2_', '_fs4_'
    m = re.search(r"_([a-g][s#]?\d)_v\d+", fname)
    if not m:
        return None
    return note_to_midi(m.group(1))


def find_loop_points(wav_path, sample_rate):
    """Find a clean sustain-loop region so holding a key loops forever.

    Skips the attack, stops before the release/tail, and snaps both ends to
    RISING zero-crossings (mono mix) so the seam lines up. Returns
    (loop_start, loop_end, crossfade_samples) in sample frames, or None when the
    sample is too short or is decaying (percussive) -- those ring out as one-shots.
    """
    audio, sr = sf.read(str(wav_path))
    mono = audio.mean(axis=1) if getattr(audio, "ndim", 1) > 1 else audio
    n = len(mono)
    if n < int(0.8 * sr):
        return None
    attack = min(int(0.5 * sr), int(0.30 * n))
    tail_guard = int(0.30 * sr)
    region_start, region_end = attack, n - tail_guard
    if region_end - region_start < int(0.3 * sr):
        return None
    pos = mono > 0
    rising = np.where((~pos[:-1]) & (pos[1:]))[0]
    rising = rising[(rising >= region_start) & (rising <= region_end)]
    if len(rising) < 2:
        return None
    ls, le = int(rising[0]), int(rising[-1])
    if (le - ls) < int(0.1 * sr):
        return None
    # don't loop a decaying tail: compare the loop's first half vs second half,
    # averaged over long windows so chorus/beating amplitude swings don't fool it.
    mid = (ls + le) // 2
    rms_first = float(np.sqrt(np.mean(mono[ls:mid] ** 2))) if mid > ls else 0.0
    rms_second = float(np.sqrt(np.mean(mono[mid:le] ** 2))) if le > mid else 0.0
    if rms_first <= 0 or rms_second < 0.5 * rms_first:
        return None
    crossfade = int(min(0.05 * sr, (le - ls) / 4))
    return ls, le, crossfade


def build_sample_part(
    part_id: int,
    name: str,
    root_midi: int,
    key_min: int,
    key_max: int,
    wav_path: Path,
    sample_count: int,
    sample_rate: int,
    loop_start: Optional[int] = None,
    loop_end: Optional[int] = None,
    loop_crossfade: int = 0,
    loop_mode: int = 1,
    sample_start: int = 0,
) -> str:
    """Generate one <MultiSamplePart> XML block for a single WAV.

    Format reverse-engineered from a real Live 12.4.5 .adv export.
    """
    abs_path = str(wav_path.resolve())
    rel_path = f"Samples/{wav_path.name}"
    file_size = wav_path.stat().st_size
    end_sample = max(0, sample_count - 1)

    # Sustain loop -- if loop points were found, holding the key loops forever.
    # loop_mode: 1 = forward, 2 = back-and-forth (ping-pong). Ping-pong reverses
    # at the endpoints so there's no jump to click or pump -- seamless by nature,
    # the way the pro factory leads do it (no baked crossfade needed).
    if loop_start is not None and loop_end is not None and loop_end > loop_start:
        sus_mode, sus_start, sus_end, sus_xfade = int(loop_mode), int(loop_start), int(loop_end), int(loop_crossfade)
    else:
        sus_mode, sus_start, sus_end, sus_xfade = 0, 0, end_sample, 0

    return f"""					<MultiSamplePart Id="{part_id}" InitUpdateAreSlicesFromOnsetsEditableAfterRead="false" HasImportedSlicePoints="false" NeedsAnalysisData="false">
						<LomId Value="0" />
						<Name Value="{name}" />
						<Selection Value="{'true' if part_id == 0 else 'false'}" />
						<IsActive Value="true" />
						<Solo Value="false" />
						<KeyRange>
							<Min Value="{key_min}" />
							<Max Value="{key_max}" />
							<CrossfadeMin Value="{key_min}" />
							<CrossfadeMax Value="{key_max}" />
						</KeyRange>
						<VelocityRange>
							<Min Value="1" />
							<Max Value="127" />
							<CrossfadeMin Value="1" />
							<CrossfadeMax Value="127" />
						</VelocityRange>
						<SelectorRange>
							<Min Value="0" />
							<Max Value="127" />
							<CrossfadeMin Value="0" />
							<CrossfadeMax Value="127" />
						</SelectorRange>
						<RootKey Value="{root_midi}" />
						<Detune Value="0" />
						<TuneScale Value="100" />
						<Panorama Value="0" />
						<Volume Value="1" />
						<Link Value="false" />
						<SampleStart Value="{sample_start}" />
						<SampleEnd Value="{end_sample}" />
						<SustainLoop>
							<Start Value="{sus_start}" />
							<End Value="{sus_end}" />
							<Mode Value="{sus_mode}" />
							<Crossfade Value="{sus_xfade}" />
							<Detune Value="0" />
						</SustainLoop>
						<ReleaseLoop>
							<Start Value="0" />
							<End Value="{end_sample}" />
							<Mode Value="3" />
							<Crossfade Value="0" />
							<Detune Value="0" />
						</ReleaseLoop>
						<SampleRef>
							<FileRef>
								<RelativePathType Value="6" />
								<RelativePath Value="{rel_path}" />
								<Path Value="{abs_path}" />
								<Type Value="2" />
								<LivePackName Value="" />
								<LivePackId Value="" />
								<OriginalFileSize Value="{file_size}" />
								<OriginalCrc Value="0" />
								<SourceHint Value="" />
							</FileRef>
							<LastModDate Value="0" />
							<SourceContext />
							<SampleUsageHint Value="0" />
							<DefaultDuration Value="{sample_count}" />
							<DefaultSampleRate Value="{sample_rate}" />
							<SamplesToAutoWarp Value="1" />
						</SampleRef>
						<SlicingThreshold Value="100" />
						<SlicingBeatGrid Value="4" />
						<SlicingRegions Value="8" />
						<SlicingStyle Value="0" />
						<SampleWarpProperties>
							<WarpMarkers />
							<WarpMode Value="4" />
							<GranularityTones Value="30" />
							<GranularityTexture Value="65" />
							<FluctuationTexture Value="25" />
							<ComplexProFormants Value="100" />
							<ComplexProEnvelope Value="128" />
							<TransientResolution Value="6" />
							<TransientLoopMode Value="2" />
							<TransientEnvelope Value="100" />
							<IsWarped Value="false" />
							<Onsets>
								<UserOnsets />
								<HasUserOnsets Value="false" />
							</Onsets>
							<TimeSignature>
								<TimeSignatures>
									<RemoteableTimeSignature Id="0">
										<Numerator Value="4" />
										<Denominator Value="4" />
										<Time Value="0" />
									</RemoteableTimeSignature>
								</TimeSignatures>
							</TimeSignature>
							<BeatGrid>
								<FixedNumerator Value="1" />
								<FixedDenominator Value="16" />
								<GridIntervalPixel Value="20" />
								<Ntoles Value="2" />
								<SnapToGrid Value="true" />
								<Fixed Value="false" />
							</BeatGrid>
						</SampleWarpProperties>
						<InitialSlicePointsFromOnsets />
						<SlicePoints />
						<ManualSlicePoints />
						<BeatSlicePoints />
						<RegionSlicePoints />
						<UseDynamicBeatSlices Value="true" />
						<UseDynamicRegionSlices Value="true" />
						<AreSlicesFromOnsetsEditable Value="false" />
					</MultiSamplePart>"""


def build_adv(template_xml: str, sample_parts_xml: str, patch_name: Optional[str] = None) -> bytes:
    """Splice generated SampleParts into the template, gzip the result.

    Works for both .adv (raw Sampler) and .adg (Instrument-Rack-wrapped Sampler)
    templates — both have a <SampleParts>...</SampleParts> section that holds
    one or more <MultiSamplePart> blocks. We just replace its contents. If
    patch_name is given, the leftover template device name (a stray
    "hookesquelch-bite" from the original export) is renamed to it.
    """
    if patch_name:
        template_xml = template_xml.replace("hookesquelch-bite", patch_name)
    # Loop Snap ON (snaps loop points to zero crossings) — the factory presets all
    # ship with this on; ours was off, which can leave a click at the loop seam.
    template_xml = re.sub(
        r'(<Snap>\s*<LomId Value="0" />\s*<Manual Value=)"false"',
        r'\1"true"', template_xml, count=1)
    new_xml = re.sub(
        r"<SampleParts>.*?</SampleParts>",
        f"<SampleParts>\n{sample_parts_xml}\n				</SampleParts>",
        template_xml,
        count=1,
        flags=re.DOTALL,
    )
    return gzip.compress(new_xml.encode("utf-8"))


def find_onset_sample(wav_path, sample_rate):
    """First sample above ~-45 dBFS, snapped back to a zero crossing (<=6ms) so
    the instrument can start playback right on the transient (no dead air)."""
    audio, sr = sf.read(str(wav_path))
    mono = audio.mean(axis=1) if getattr(audio, "ndim", 1) > 1 else audio
    thr = 10 ** (-45 / 20)
    idx = np.where(np.abs(mono) > thr)[0]
    if len(idx) == 0:
        return 0
    o = int(idx[0])
    for j in range(o, max(0, o - int(0.006 * sr)), -1):
        if j > 0 and ((mono[j - 1] <= 0 < mono[j]) or (mono[j - 1] >= 0 > mono[j])):
            return j
    return o


# Public API — used by build-pack.py to integrate Ableton preset generation
# into the full pack pipeline without subprocess'ing this script.
def build_presets_for_wavs(
    wavs: list,
    out_dir: Path,
    patch_name: str,
    formats: tuple = ("adv", "adg"),
    templates_dir: Optional[Path] = None,
    loop: bool = True,
) -> dict:
    """Build Ableton .adv and/or .adg files from a list of WAV paths.

    Each WAV must have a parseable note in its filename (e.g. '_a3_', '_c#4_', '_ds2_').
    Returns dict with keys 'written' (list of Paths) and 'samples' (list of (midi, wav)).
    """
    if templates_dir is None:
        templates_dir = Path(__file__).resolve().parent / "templates"

    samples = []
    for w in wavs:
        w = Path(w)
        midi = parse_note_from_filename(w.name)
        if midi is None:
            continue
        info = sf.info(str(w))
        samples.append((midi, w, info.frames, info.samplerate))

    if not samples:
        return {"written": [], "samples": [], "error": "no parseable WAVs"}

    samples.sort(key=lambda s: s[0])
    roots = [s[0] for s in samples]

    # bake-loops sidecar: exact matched loop points (crossfade already in audio)
    _loops_sidecar = {}
    try:
        import json as _json
        _sc = Path(samples[0][1]).parent / "loops.json"
        if _sc.exists():
            _loops_sidecar = _json.loads(_sc.read_text())
    except Exception:
        _loops_sidecar = {}

    parts_xml = []
    for i, (root, wav, frames, sr) in enumerate(samples):
        if i == 0:
            key_min = 0
        else:
            key_min = (roots[i - 1] + root) // 2 + 1
        if i == len(samples) - 1:
            key_max = 127
        else:
            key_max = (root + roots[i + 1]) // 2
        ss = find_onset_sample(wav, sr)
        sc = _loops_sidecar.get(wav.name, "MISSING")
        if isinstance(sc, dict):
            ls, le = int(sc["start"]), int(sc["end"])
        elif sc is None:
            ls, le = None, None                                 # explicitly a one-shot
        else:
            lp = find_loop_points(wav, sr) if loop else None
            ls, le = (lp[0], lp[1]) if lp else (None, None)
        # Pro recipe (from the factory presets): forward loop + crossfade ~7% of
        # the loop length (Glidesynth 7.1%, PAD 7.4%), Ableton-handled on a
        # pristine WAV, capped so it fits the pre-loop room.
        if ls is not None and le is not None and le > ls:
            # keep the crossfade pre-roll inside the sustain (reserve ~0.45s for the
            # attack) so Ableton's loop crossfade never blends in the onset transient
            xf = min(int(0.07 * (le - ls)), max(0, ls - int(0.45 * sr)))
        else:
            xf = 0
        block = build_sample_part(i, wav.stem, root, key_min, key_max, wav, frames, sr,
                                  loop_start=ls, loop_end=le, loop_crossfade=xf,
                                  loop_mode=1, sample_start=ss)
        parts_xml.append(block)

    sample_parts_combined = "\n".join(parts_xml)

    written = []
    out_dir.mkdir(parents=True, exist_ok=True)
    for ext in formats:
        template_name = "ableton-sampler-template.xml" if ext == "adv" else "ableton-rack-template.xml"
        template_xml = (templates_dir / template_name).read_text()
        data = build_adv(template_xml, sample_parts_combined, patch_name)
        out_path = out_dir / f"{patch_name}.{ext}"
        out_path.write_bytes(data)
        written.append(out_path)
    return {"written": written, "samples": samples}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instrument", required=True, help="instrument slug, e.g. 'grandmother'")
    ap.add_argument("--patch", required=True, help="patch name, e.g. 'hookesquelch-bite'")
    ap.add_argument("--chain", default="raw", help="audio chain dir (raw/spring/processed)")
    ap.add_argument("--out", default=None, help="output path (default: bench/<patch>.<ext>)")
    ap.add_argument("--format", default="adg", choices=["adv", "adg", "both"],
                    help="output format. adv = raw Sampler, adg = Instrument-Rack-wrapped "
                         "Sampler (needed for export to Move). 'both' writes both files. "
                         "Default: adg (rack-wrapped — the format you can export to Move from Live)")
    ap.add_argument("--bank-root", default=None,
                    help="path to spacepit-sample-bank repo (default: auto-detect)")
    ap.add_argument("--template", default=None,
                    help="path to template XML (overrides format selection)")
    args = ap.parse_args()

    # Resolve paths
    here = Path(__file__).resolve()
    if args.bank_root:
        bank = Path(args.bank_root).resolve()
    else:
        bank = here.parent.parent.parent  # tools/pack/X.py → repo root
    templates_dir = here.parent / "templates"
    patch_dir = bank / "instruments" / args.instrument / "patches" / args.patch / args.chain

    # Pick template(s) based on --format
    output_specs = []  # list of (template_path, extension)
    if args.template:
        # explicit template override — extension follows what user asked for
        ext = "adg" if args.format == "adg" else "adv"
        output_specs.append((Path(args.template), ext))
    else:
        if args.format in ("adv", "both"):
            output_specs.append((templates_dir / "ableton-sampler-template.xml", "adv"))
        if args.format in ("adg", "both"):
            output_specs.append((templates_dir / "ableton-rack-template.xml", "adg"))

    for tp, _ in output_specs:
        if not tp.exists():
            print(f"template missing: {tp}", file=sys.stderr); sys.exit(1)
    if not patch_dir.exists():
        print(f"patch dir missing: {patch_dir}", file=sys.stderr); sys.exit(1)

    # Find all WAVs + parse their root notes
    wavs = sorted(patch_dir.glob("*.wav"))
    if not wavs:
        print(f"no WAVs in {patch_dir}", file=sys.stderr); sys.exit(1)

    samples = []  # list of (midi_root, wav_path, sample_count, sample_rate)
    for w in wavs:
        midi = parse_note_from_filename(w.name)
        if midi is None:
            print(f"  skip (no note): {w.name}")
            continue
        info = sf.info(str(w))
        samples.append((midi, w, info.frames, info.samplerate))
        print(f"  ✓ {w.name} → MIDI {midi}")

    if not samples:
        print("no captures with parseable notes found", file=sys.stderr); sys.exit(1)

    # Sort by pitch ascending so KeyRange computation is monotonic
    samples.sort(key=lambda s: s[0])
    roots = [s[0] for s in samples]

    # Compute KeyRange per sample — split halfway between adjacent roots.
    # Edges extend to 0 (low) and 127 (high) so the whole keyboard is covered.
    parts_xml = []
    for i, (root, wav, frames, sr) in enumerate(samples):
        if i == 0:
            key_min = 0
        else:
            # halfway down to the previous root, rounded down so this sample owns the midpoint
            key_min = (roots[i - 1] + root) // 2 + 1
        if i == len(samples) - 1:
            key_max = 127
        else:
            key_max = (root + roots[i + 1]) // 2
        name = wav.stem  # filename without .wav extension
        lp = find_loop_points(wav, sr)
        block = build_sample_part(i, name, root, key_min, key_max, wav, frames, sr,
                                  loop_start=(lp[0] if lp else None),
                                  loop_end=(lp[1] if lp else None),
                                  loop_crossfade=(lp[2] if lp else 0))
        parts_xml.append(block)

    sample_parts_combined = "\n".join(parts_xml)

    # Compute default output dir if --out wasn't given
    out_dir = bank / "bench"
    out_dir.mkdir(parents=True, exist_ok=True)

    written_files = []
    for template_path, ext in output_specs:
        template_xml = template_path.read_text()
        adv_bytes = build_adv(template_xml, sample_parts_combined, args.patch)
        # Where to write this format
        if args.out and len(output_specs) == 1:
            out_path = Path(args.out)
        else:
            out_path = out_dir / f"{args.patch}.{ext}"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(adv_bytes)
        written_files.append((out_path, len(adv_bytes), ext))

    print()
    for out_path, size, ext in written_files:
        print(f"✓ wrote {out_path}  ({size} bytes)")
    print(f"  {len(samples)} samples, root range MIDI {roots[0]}-{roots[-1]}, "
          f"key zones covering MIDI 0-127")
    print()
    if any(ext == "adg" for _, _, ext in written_files):
        print("→ drag the .adg into Live's browser, right-click the Rack header,")
        print("  choose 'Export ABL Preset' to ship it to your Ableton Move.")
    elif written_files:
        print(f"→ drag {written_files[0][0].name} into Live's browser to test")


if __name__ == "__main__":
    main()
