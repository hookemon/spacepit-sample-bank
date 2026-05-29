#!/usr/bin/env python3
"""Build a complete spacepit sample pack from an instrument's bank folder.

Takes the source `instruments/<slug>/` and produces a ready-to-zip pack at
`releases/spacepit-<slug>-vol1/` with:
  - Organized WAVs (multisamples + loops)
  - SFZ presets (one per patch + chain) — universal cross-DAW
  - Decent Sampler presets (one per patch + chain)
  - Ableton presets — .adv (raw Sampler) + .adg (Rack-wrapped, Move-exportable)
  - Treated photos (rotated + scaled)
  - Auto-generated README.md from the manifest

Usage:
  build-pack.py --instrument grandmother
  build-pack.py --instrument grandmother --vol 1
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

# Pull in the Ableton .adv / .adg generator (lives in same dir)
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from build_ableton_adv import build_presets_for_wavs as _build_ableton_presets
except Exception as _e:
    _build_ableton_presets = None
    print(f"  ⚠ Ableton preset generator unavailable: {_e}")

# We need sys for sys.executable in the sub-tool calls.


def _compute_loudness_gains(patches_dir: Path, ref_chain: str = "raw",
                            ceiling_db: float = -1.0, max_boost_db: float = 15.0,
                            target_loudness_db: float = -12.0) -> dict:
    """Per-NOTE makeup gain (dB) so every note of every patch lands at a FIXED hot target
    loudness (~-12 dB short-term RMS = ~-12 on the Ableton meter) — the pro-library
    treatment. Fixes two things at once:
      • across patches: a dense saw vs a sub bass (~16 dB apart raw) -> same level,
      • up each keyboard: raw multisamples roll off ~14 dB low->high -> evened.
    Target is a FIXED value (NOT the bank median — the median gets dragged down by quiet
    captures, which then pulls the hot patches DOWN, the bug Nick caught where the sub
    came out -24 instead of -12). Held under a true-peak ceiling so nothing clips, with a
    boost cap so very quiet captures aren't over-amplified into hiss. Returns
    {patch: {wav_name: gain_db}}; {} if audio libs are missing so the pack still builds."""
    try:
        import importlib.util
        import math
        import numpy as np
        ml_path = Path(__file__).resolve().parent / "match-loudness.py"
        spec = importlib.util.spec_from_file_location("match_loudness", ml_path)
        ml = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ml)
    except Exception as e:
        print(f"  ⚠ loudness match unavailable ({e}) — patches keep their raw levels")
        return {}
    db = lambda x: 20 * math.log10(x + 1e-12)
    # measure every note of every patch
    patch_notes = {}            # patch -> [(wav_name, loud_db, peak_db)]
    all_loud = []
    for pd in sorted(patches_dir.iterdir()):
        if not pd.is_dir() or pd.name.endswith("_original"):
            continue
        cd = pd / ref_chain
        if not cd.is_dir():
            cand = [c for c in sorted(pd.iterdir())
                    if c.is_dir() and not c.name.endswith("_original") and list(c.glob("*.wav"))]
            cd = cand[0] if cand else None
        if cd is None:
            continue
        notes = []
        for w in sorted(cd.glob("*.wav")):
            rms, peak = ml.note_loudness(w)
            notes.append((w.name, db(rms), db(peak)))
            all_loud.append(db(rms))
        if notes:
            patch_notes[pd.name] = notes
    if not all_loud:
        return {}
    # Each note -> the FIXED target loudness: gain up quiet notes (capped so a too-quiet
    # capture isn't boosted into hiss), pull down hot ones, all clamped under the peak
    # ceiling so nothing clips. No median, no second "push" pass — the target IS the hot level.
    out = {}
    boosted_to_cap = 0
    for patch, notes in patch_notes.items():
        gmap = {}
        for name, ld, pk in notes:
            want = target_loudness_db - ld                       # + = boost, - = pull down
            want = min(want, max_boost_db)                       # don't over-boost quiet hiss
            gain = min(want, ceiling_db - pk)                    # never clip
            gmap[name] = gain
            if want >= max_boost_db - 0.01:
                boosted_to_cap += 1
        out[patch] = gmap
    print(f"  loudness: every note -> {target_loudness_db:.0f} dB target (peak-safe, -1 ceiling)"
          + (f" · {boosted_to_cap} note(s) hit the +{max_boost_db:.0f}dB cap — captured too quiet" if boosted_to_cap else ""))
    return out


def _apply_gain_copy(src: Path, dst: Path, gain_db: float) -> None:
    """Copy a WAV applying a dB gain (peak-guarded). Falls back to a plain copy when
    the gain is ~0 or audio libs are absent, so the pack always builds."""
    if abs(gain_db) < 0.05:
        shutil.copy2(src, dst)
        return
    try:
        import numpy as np
        import soundfile as sf
        a, sr = sf.read(str(src))
        subtype = sf.info(str(src)).subtype
        a = a * (10.0 ** (gain_db / 20.0))
        peak = float(np.max(np.abs(a))) if a.size else 0.0
        if peak > 0.999:                       # never clip (gain is already peak-bounded)
            a = a * (0.999 / peak)
        sf.write(str(dst), a, sr, subtype=subtype)
    except Exception as e:
        print(f"  ⚠ gain apply failed for {src.name} ({e}) — copied flat")
        shutil.copy2(src, dst)


def find_bank_root() -> Path | None:
    p = Path.cwd().resolve()
    for c in [p, *p.parents]:
        if (c / "instruments").is_dir() and (c / "manifest.template.json").exists():
            return c
    fallback = Path.home() / "projects" / "spacepit-sample-bank"
    if (fallback / "instruments").is_dir():
        return fallback
    return None


# ---------- note name <-> MIDI ----------

NOTE_NAMES = ["c", "cs", "d", "ds", "e", "f", "fs", "g", "gs", "a", "as", "b"]


def note_name_to_midi(name: str) -> int:
    m = re.match(r"^([a-g])(s|#|b)?(-?\d+)$", name.lower())
    if not m:
        raise ValueError(f"bad note name: {name}")
    letter, acc, oct_ = m.groups()
    base = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}[letter]
    if acc in ("s", "#"):
        base += 1
    elif acc == "b":
        base -= 1
    return base + (int(oct_) + 1) * 12


# ---------- parse filename: <slug>_<patch>_<note>_v<vel>_rr<rr>.wav ----------

def parse_sample_filename(fname: str) -> dict | None:
    """Returns {'slug', 'patch', 'note', 'velocity', 'rr'} or None."""
    base = fname.replace(".wav", "")
    parts = base.split("_")
    if len(parts) < 5:
        return None
    *prefix_parts, note, vel_part, rr_part = parts
    if len(prefix_parts) < 2:
        return None
    slug = prefix_parts[0]
    patch = "_".join(prefix_parts[1:])
    vel_match = re.match(r"^v(\d+)$", vel_part)
    rr_match = re.match(r"^rr(\d+)$", rr_part)
    if not vel_match or not rr_match:
        return None
    try:
        midi = note_name_to_midi(note)
    except ValueError:
        return None
    return {
        "slug": slug,
        "patch": patch,
        "note": note,
        "midi": midi,
        "velocity": int(vel_match.group(1)),
        "rr": int(rr_match.group(1)),
        "filename": fname,
    }


# ---------- SFZ generation ----------

def generate_sfz(patch_dir: Path, samples: list[dict], patch_name: str, chain: str) -> str:
    """Generate an SFZ preset that maps the captured notes across the MIDI keyboard."""
    samples_sorted = sorted(samples, key=lambda s: s["midi"])
    lines = [
        f"// spacepit sample pack — {patch_name} ({chain})",
        f"// {len(samples_sorted)} captured notes, mapped chromatically with pitch interpolation",
        "",
        "<group>",
        "ampeg_attack=0.001",
        "ampeg_release=0.5",
        "loop_mode=no_loop",
        "",
    ]

    for i, s in enumerate(samples_sorted):
        midi = s["midi"]
        if i == 0:
            lo = 0
        else:
            prev_midi = samples_sorted[i - 1]["midi"]
            lo = (prev_midi + midi) // 2 + 1
        if i == len(samples_sorted) - 1:
            hi = 127
        else:
            next_midi = samples_sorted[i + 1]["midi"]
            hi = (midi + next_midi) // 2
        lines.append("<region>")
        lines.append(f"sample={s['filename']}")
        lines.append(f"lokey={lo}")
        lines.append(f"hikey={hi}")
        lines.append(f"pitch_keycenter={midi}")
        lines.append("")

    return "\n".join(lines)


# ---------- Decent Sampler generation ----------

def generate_decent_sampler(patch_name: str, chain: str, samples: list[dict], sample_rel_dir: str) -> str:
    """Generate a Decent Sampler .dspreset XML file."""
    samples_sorted = sorted(samples, key=lambda s: s["midi"])

    sample_regions = []
    for i, s in enumerate(samples_sorted):
        midi = s["midi"]
        if i == 0:
            lo = 0
        else:
            prev_midi = samples_sorted[i - 1]["midi"]
            lo = (prev_midi + midi) // 2 + 1
        if i == len(samples_sorted) - 1:
            hi = 127
        else:
            next_midi = samples_sorted[i + 1]["midi"]
            hi = (midi + next_midi) // 2
        path = xml_escape(f"{sample_rel_dir}/{s['filename']}")
        sample_regions.append(
            f'      <sample path="{path}" rootNote="{midi}" loNote="{lo}" hiNote="{hi}" />'
        )

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<DecentSampler minVersion="1.0.0">
  <ui width="812" height="375" bgImage="">
    <tab name="main">
      <labeled-knob x="40" y="100" label="Amp Env" type="float" minValue="0.0" maxValue="2.0" value="0.5">
        <binding type="amp" level="instrument" position="0" parameter="ENV_ATTACK" />
      </labeled-knob>
      <labeled-knob x="160" y="100" label="Release" type="float" minValue="0.0" maxValue="5.0" value="0.5">
        <binding type="amp" level="instrument" position="0" parameter="ENV_RELEASE" />
      </labeled-knob>
      <label x="20" y="20" width="800" height="50" text="spacepit — {xml_escape(patch_name)} ({xml_escape(chain)})" textSize="22" textColor="FFF2B705" />
    </tab>
  </ui>
  <groups>
    <group attack="0.001" release="0.5">
{chr(10).join(sample_regions)}
    </group>
  </groups>
</DecentSampler>
"""


# ---------- photo treatment via sips (macOS built-in) ----------

def detect_orientation(image_path: Path) -> str:
    """Use sips to check image dimensions and return 'portrait' or 'landscape'."""
    try:
        r = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(image_path)],
                           capture_output=True, text=True, check=True)
        width = height = None
        for line in r.stdout.split("\n"):
            line = line.strip()
            if line.startswith("pixelWidth:"):
                width = int(line.split(":")[1].strip())
            elif line.startswith("pixelHeight:"):
                height = int(line.split(":")[1].strip())
        if width and height:
            return "portrait" if height > width else "landscape"
    except Exception:
        pass
    return "landscape"


def treat_photo(src: Path, dst: Path, max_dim: int = 2400, rotate_deg: int = 0) -> None:
    """Copy + optional rotate + resize via sips. rotate_deg is clockwise."""
    # First copy
    shutil.copy2(src, dst)
    # Optional rotation (clockwise degrees, in 90° increments)
    if rotate_deg and rotate_deg % 360 != 0:
        subprocess.run(
            ["sips", "--rotate", str(rotate_deg % 360), str(dst)],
            capture_output=True, check=True,
        )
    # Resize to max dimension (keeps aspect, downsizes only)
    subprocess.run(
        ["sips", "--resampleHeightWidthMax", str(max_dim), str(dst)],
        capture_output=True, check=True,
    )


# Manual rotation map for known-rotated photos (filename → CW degrees to apply).
# Empty/missing entries = no rotation.
PHOTO_ROTATIONS: dict[str, int] = {
    "IMG_3313.JPG": 90,   # studio panorama — was tilted
    "IMG_3314.JPG": 90,   # brand badge close-up
    "IMG_3315.JPG": 90,   # panel + filter section
    "IMG_3317.JPG": 90,   # top section with patch cable
    "IMG_3322.JPG": 90,   # 3/4 angle with Eurorack
    # IMG_3316, 3321, 3323 are already correct landscape — no rotation
}


# ---------- main pack builder ----------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instrument", required=True, help="instrument slug (e.g. grandmother)")
    ap.add_argument("--vol", type=int, default=1)
    ap.add_argument("--bank-root", default=None)
    ap.add_argument("--output-base", default=None, help="where to write the pack (default: <bank-root>/releases/)")
    args = ap.parse_args()

    bank_root = Path(args.bank_root).resolve() if args.bank_root else find_bank_root()
    if not bank_root:
        print("can't find bank root", file=sys.stderr); sys.exit(1)

    instr_dir = bank_root / "instruments" / args.instrument
    if not instr_dir.exists():
        print(f"no instrument folder: {instr_dir}", file=sys.stderr); sys.exit(1)

    manifest_path = instr_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"no manifest: {manifest_path}", file=sys.stderr); sys.exit(1)
    manifest = json.loads(manifest_path.read_text())

    output_base = Path(args.output_base).resolve() if args.output_base else bank_root / "releases"
    pack_name = f"spacepit-{args.instrument}-vol{args.vol}"
    pack_dir = output_base / pack_name

    # Clean + create
    if pack_dir.exists():
        print(f"removing existing {pack_dir}")
        shutil.rmtree(pack_dir)
    pack_dir.mkdir(parents=True)

    print(f"\n=== building pack: {pack_name} ===")
    print(f"  source: {instr_dir}")
    print(f"  output: {pack_dir}")

    # Create folder structure — only folders that will actually have content
    # (Tier 2/3 formats like ableton/.adv, logic-exs24, move-kit, ep-133 come in v1.1)
    for sub in ["audio/multisamples", "audio/loops", "instruments/sfz",
                "instruments/decent-sampler", "instruments/ableton",
                "images", "docs"]:
        (pack_dir / sub).mkdir(parents=True, exist_ok=True)

    # ----- 1. Copy multisamples + generate SFZ + Decent Sampler -----
    print("\n[1/5] multisamples + SFZ + Decent Sampler + Ableton presets")
    patches_dir = instr_dir / "patches"
    multisample_count = 0
    sfz_count = 0
    dspreset_count = 0
    ableton_preset_count = 0

    # Loudness pass: every patch → the bank's median loudness, peak-safe, so switching
    # patches in Ableton feels even (raw spread is 16+ dB: sub bass vs a bell). Computed
    # on the source masters; the gain is baked into the pack copies at copy time below.
    loudness_gains = _compute_loudness_gains(patches_dir) if patches_dir.exists() else {}
    if loudness_gains:
        _all = [g for m in loudness_gains.values() for g in m.values()]
        print(f"  loudness-match: {len(loudness_gains)} patches, {len(_all)} notes → bank median "
              f"(per-note gains {min(_all):+.1f}..{max(_all):+.1f} dB, peak-safe)")

    if patches_dir.exists():
        for patch_folder in sorted(patches_dir.iterdir()):
            if not patch_folder.is_dir():
                continue
            patch_name = patch_folder.name

            # Real Roland factory name for everything the user SEES (Ableton device name,
            # README, pack list) — e.g. "A57 · Euro SAW". The slug stays the folder/file id.
            _mp = next((p for p in manifest.get("patches", []) if p.get("name") == patch_name), {})
            if _mp.get("preset_name") and _mp.get("preset_position"):
                display_name = f"{_mp['preset_position']} · {_mp['preset_name']}"
            else:
                display_name = _mp.get("preset_name") or patch_name

            # iterate chains (raw, spring, etc.) — skip backup folders the cleaner left behind
            for chain_folder in sorted(patch_folder.iterdir()):
                if not chain_folder.is_dir():
                    continue
                chain = chain_folder.name
                # skip clean-wavs.py backup folders (e.g. raw_original) — those aren't real chains
                if chain.endswith("_original"):
                    continue

                # find WAVs
                wavs = sorted(chain_folder.glob("*.wav"))
                if not wavs:
                    continue

                # parse each
                samples = []
                for wav in wavs:
                    parsed = parse_sample_filename(wav.name)
                    if parsed:
                        samples.append(parsed)

                if not samples:
                    print(f"  ⚠ {patch_name}/{chain}: no parseable WAVs"); continue

                # destination
                dest = pack_dir / "audio/multisamples" / patch_name / chain
                dest.mkdir(parents=True, exist_ok=True)
                _pgmap = loudness_gains.get(patch_name, {})   # per-note loudness-match gains
                for wav in wavs:
                    _apply_gain_copy(wav, dest / wav.name, _pgmap.get(wav.name, 0.0))
                    multisample_count += 1

                # SFZ in instruments/sfz/
                sfz_name = f"{patch_name}-{chain}.sfz" if chain != "raw" else f"{patch_name}.sfz"
                sfz_content = generate_sfz(dest, samples, patch_name, chain)
                # SFZ references samples — place SFZ alongside the WAVs for the cleanest setup
                (pack_dir / "instruments/sfz" / sfz_name).write_text(sfz_content)
                # Also write a copy NEXT TO the WAVs for direct loading
                (dest / sfz_name).write_text(sfz_content)
                sfz_count += 1

                # Decent Sampler — references samples via relative path
                rel_path = f"../../audio/multisamples/{patch_name}/{chain}"
                ds_content = generate_decent_sampler(patch_name, chain, samples, rel_path)
                ds_name = f"{patch_name}-{chain}.dspreset" if chain != "raw" else f"{patch_name}.dspreset"
                (pack_dir / "instruments/decent-sampler" / ds_name).write_text(ds_content)
                dspreset_count += 1

                # Ableton .adv (raw Sampler) + .adg (Rack-wrapped for Move export).
                # The .adv embeds absolute paths to the WAVs we just copied into pack/audio/...
                # so anyone who unzips the pack at the same location will get working presets.
                # For shipping: the .adv samples references point INTO the pack itself.
                ableton_label = display_name if chain == "raw" else f"{display_name} ({chain})"
                wav_dest_paths = sorted((pack_dir / "audio/multisamples" / patch_name / chain).glob("*.wav"))
                ableton_extras = ""
                # Find seamless loop points on the copied WAVs (writes loops.json beside
                # them) so the Ableton build inherits the locked loop recipe.
                # Loop-vs-one-shot is decided PER PATCH by the manifest "loop" flag
                # (default True = sustained/looping). Percussive patches (plucks, bells)
                # set "loop": false so they ring out as one-shots — no false loop seam.
                if wav_dest_paths:
                    _loop_script = Path(__file__).resolve().parent / "bake-loops.py"
                    if _loop_script.exists():
                        _loop_on = next(
                            (p.get("loop", True) for p in manifest.get("patches", [])
                             if p.get("name") == patch_name),
                            True)
                        _bake_cmd = [sys.executable, str(_loop_script), "--dir",
                                     str(pack_dir / "audio/multisamples" / patch_name / chain)]
                        if not _loop_on:
                            _bake_cmd.append("--no-loop")
                        try:
                            subprocess.run(_bake_cmd, capture_output=True, text=True, timeout=120)
                        except Exception as _le:
                            print(f"  ⚠ loop-finder skipped for {patch_name}/{chain}: {_le}")
                # Amp-envelope release by patch ROLE so note-off rings out like the synth
                # (the template default is a too-short 50ms that cuts pads off). Pads ring,
                # basses stay tight. Deterministic — no fragile audio guessing.
                _role = next((p.get("role", "") for p in manifest.get("patches", [])
                              if p.get("name") == patch_name), "")
                _release_ms = {"pad": 1000.0, "keys": 400.0, "lead": 250.0,
                               "bass": 120.0, "synth": 350.0}.get(_role, 300.0)
                if _build_ableton_presets and wav_dest_paths:
                    try:
                        result = _build_ableton_presets(
                            wavs=wav_dest_paths,
                            out_dir=pack_dir / "instruments/ableton",
                            patch_name=ableton_label,
                            formats=("adv", "adg"),
                            release_ms=_release_ms,
                        )
                        if result.get("written"):
                            ableton_preset_count += len(result["written"])
                            ableton_extras = " + adv + adg"
                    except Exception as ex:
                        print(f"  ⚠ Ableton preset failed for {patch_name}/{chain}: {ex}")

                print(f"  ✓ {patch_name}/{chain}: {len(samples)} samples → SFZ + dspreset{ableton_extras}")

    # ----- 1b. Ableton-friendly renamed copies (for drag-drop into Sampler) -----
    print("\n[1b/8] Ableton-friendly multisample copies (renamed for auto-pitch)")
    ableton_copies = 0
    if patches_dir.exists():
        for patch_folder in sorted(patches_dir.iterdir()):
            if not patch_folder.is_dir():
                continue
            patch_name = patch_folder.name
            for chain_folder in sorted(patch_folder.iterdir()):
                if not chain_folder.is_dir():
                    continue
                chain = chain_folder.name
                wavs = sorted(chain_folder.glob("*.wav"))
                if not wavs:
                    continue
                # Destination: audio/ableton-ready/<patch>-<chain>/
                folder_label = patch_name if chain == "raw" else f"{patch_name}-{chain}"
                dest = pack_dir / "audio" / "ableton-ready" / folder_label
                dest.mkdir(parents=True, exist_ok=True)
                for wav in wavs:
                    parsed = parse_sample_filename(wav.name)
                    if not parsed:
                        continue
                    # Ableton parses note names like "A1", "C#3", "Bb4" in filenames.
                    # Convert "ds2" → "D#2", "fs3" → "F#3" — Ableton prefers # over s.
                    note = parsed["note"]
                    m = re.match(r"^([a-g])(s|b)?(-?\d+)$", note.lower())
                    if not m:
                        continue
                    letter, acc, octv = m.groups()
                    acc_str = "#" if acc == "s" else ("b" if acc == "b" else "")
                    nice_note = f"{letter.upper()}{acc_str}{octv}"
                    new_name = f"{folder_label} {nice_note}.wav"
                    shutil.copy2(wav, dest / new_name)
                    ableton_copies += 1
    print(f"  ✓ {ableton_copies} Ableton-friendly copies in audio/ableton-ready/")

    # ----- 2. Copy loops -----
    print("\n[2/5] loops")
    loops_src = instr_dir / "loops" / "raw"
    loop_count = 0
    if loops_src.exists():
        for wav in sorted(loops_src.glob("*.wav")):
            shutil.copy2(wav, pack_dir / "audio/loops" / wav.name)
            loop_count += 1
        print(f"  ✓ {loop_count} loops copied")
    else:
        print("  (no loops folder)")

    # ----- 3. Photos -----
    print("\n[3/5] photos (treated via sips)")
    photos_src = instr_dir / "photos"
    photo_count = 0
    if photos_src.exists():
        for img in sorted(photos_src.glob("*.[Jj][Pp][Gg]")):
            dst = pack_dir / "images" / img.name
            rotate = PHOTO_ROTATIONS.get(img.name, 0)
            try:
                treat_photo(img, dst, rotate_deg=rotate)
                photo_count += 1
                if rotate:
                    print(f"    rotated {img.name} {rotate}° CW")
            except Exception as e:
                print(f"  ⚠ failed to treat {img.name}: {e}")
                shutil.copy2(img, dst)
        print(f"  ✓ {photo_count} photos treated + copied")
    else:
        print("  (no photos folder)")

    # ----- 4. Patch notes -----
    print("\n[4/5] patch notes")
    patch_notes_count = 0
    docs_dir = pack_dir / "docs"
    if patches_dir.exists():
        combined = ["# Patch Notes\n"]
        combined.append(f"_From: {manifest.get('name', args.instrument)}_\n\n---\n")
        for patch_folder in sorted(patches_dir.iterdir()):
            if not patch_folder.is_dir():
                continue
            pn_path = patch_folder / "patch-notes.md"
            if pn_path.exists():
                combined.append(f"\n## {patch_folder.name}\n")
                combined.append(pn_path.read_text())
                combined.append("\n---\n")
                patch_notes_count += 1
        (docs_dir / "PATCH_NOTES.md").write_text("\n".join(combined))
        print(f"  ✓ {patch_notes_count} patch notes combined into docs/PATCH_NOTES.md")

    # ----- 5. README -----
    print("\n[5/5] README")
    readme = build_readme(manifest, multisample_count, loop_count, photo_count, sfz_count, dspreset_count, args.vol)
    (pack_dir / "README.md").write_text(readme)
    print(f"  ✓ README.md")

    # ----- 6. Inventory + demo audio + landing page + pack tour -----
    print("\n[6/8] inventory")
    inv_script = bank_root / "tools" / "pack" / "build-inventory.py"
    demo_script = bank_root / "tools" / "pack" / "build-demo-audio.py"
    venv_py = bank_root / "tools" / "capture" / ".venv" / "bin" / "python"
    py = str(venv_py) if venv_py.exists() else sys.executable

    if inv_script.exists():
        r = subprocess.run([py, str(inv_script), "--pack", str(pack_dir)])
        if r.returncode == 0:
            print(f"  ✓ pack-inventory.json generated")

    print("\n[7/8] demo audio (46-sec pack tour)")
    if demo_script.exists():
        r = subprocess.run([py, str(demo_script), "--pack", str(pack_dir)])
        if r.returncode == 0:
            print(f"  ✓ demo.wav generated")

    print("\n[8/8] landing page + pack tour markdown")
    landing_path = pack_dir / "landing.html"
    landing_path.write_text(build_landing_html(manifest, multisample_count, loop_count))
    print(f"  ✓ landing.html generated")

    tour_path = pack_dir / "PACK_TOUR.md"
    tour_path.write_text(build_pack_tour(manifest, multisample_count, loop_count, sfz_count, dspreset_count, photo_count))
    print(f"  ✓ PACK_TOUR.md generated")

    # Summary
    print(f"\n{'=' * 60}")
    print(f"✓ pack built: {pack_dir}")
    print(f"   {multisample_count} multisample WAVs")
    print(f"   {loop_count} loop WAVs")
    print(f"   {sfz_count} SFZ presets")
    print(f"   {dspreset_count} Decent Sampler presets")
    print(f"   {ableton_preset_count} Ableton presets (.adv + .adg, Move-exportable)")
    print(f"   {photo_count} treated photos")
    print(f"   {patch_notes_count} patch notes consolidated")
    print(f"\nNext steps (future sessions):")
    print(f"   - .ablpresetbundle direct generation (no Live round-trip)")
    print(f"   - Logic EXS24 presets")
    print(f"   - EP-133 K.O. II project")
    print(f"   - Demo beat")
    print(f"   - Zip + Gumroad upload")
    print(f"\nTo preview: cd {pack_dir} && ls -R")


def build_pack_tour(manifest: dict, ms_count: int, loop_count: int, sfz_count: int,
                    dspreset_count: int, photo_count: int) -> str:
    name = manifest.get("name", "Unknown")
    return f"""# Pack Tour — {name}

Creator-facing walkthrough. Open this when you want to remember what's in the pack and what's next.

## Currently in this folder

```
spacepit-{manifest.get('slug', 'unknown')}-vol1/
├── README.md              ← producer-facing pack docs (= Gumroad listing copy)
├── PACK_TOUR.md           ← this file (creator-facing)
├── landing.html           ← interactive landing page (browse all loops + audio previews)
├── demo.wav               ← 46-sec pack tour audio (headline preview)
├── pack-inventory.json    ← machine-readable inventory of every file
├── audio/
│   ├── multisamples/      ← {ms_count} WAVs across patches × chains
│   └── loops/             ← {loop_count} tempo-tagged loops
├── instruments/
│   ├── sfz/               ← {sfz_count} SFZ presets (universal cross-DAW)
│   ├── decent-sampler/    ← {dspreset_count} Decent Sampler .dspreset files
│   ├── ableton/           ← (empty — next session)
│   ├── logic-exs24/       ← (empty — next session)
│   ├── move-kit/          ← (empty — Tier 3)
│   └── ep-133/            ← (empty — Tier 3)
├── images/                ← {photo_count} hero photos, color + rotation corrected
└── docs/PATCH_NOTES.md    ← combined patch documentation
```

## To view the landing page

```bash
gmlanding              # starts a local server + opens in browser
```

Or manually: `cd` into this folder, `python3 -m http.server 8765`, browse to http://localhost:8765/landing.html

## To rebuild after changes

```bash
gmpack                 # full rebuild from source bank state
gmpackopen             # opens this folder in Finder
gmpackzip              # zips for Gumroad upload
gmdemo                 # rebuilds just the demo audio
gminventory            # regenerates just the inventory JSON
```

## What's left to ship

1. **Ableton native presets** (Tier 2) — Sampler/Drum Rack .adv/.adg files
2. **Logic EXS24 presets** (Tier 2)
3. **TE Move kit** (Tier 3)
4. **EP-133 K.O. II project** (Tier 3)
5. **Demo BEAT** (vs the current demo audio which is a tour) — make a beat in Ableton using the pack samples
6. **Gumroad listing** — copy from README.md, hero photo from images/, audio from demo.wav
7. **Spacepit-web landing page** — port landing.html to the spacepit-web Sanity / Next.js stack
8. **IG drop** — tag @teenage.engineering, link to Gumroad
"""


def build_landing_html(manifest: dict, ms_count: int, loop_count: int) -> str:
    name = manifest.get("name", "Unknown")
    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>spacepit — {xml_escape(name)} Vol 1 (sample pack)</title>
<style>
  :root {{
    --bg: #0a0a0a; --bg-2: #141414; --fg: #f0e9d9; --fg-dim: #888; --fg-faint: #555;
    --amber: #F2B705; --shadow: 6px 6px 0 #000;
    --mono: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
    --display: 'Antonio', system-ui, sans-serif;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html {{ background: var(--bg); color: var(--fg); font-family: var(--mono); line-height: 1.5; }}
  body {{ max-width: 1100px; margin: 0 auto; padding: 32px 24px 80px; }}
  header.hero {{ display: grid; grid-template-columns: 1fr 1fr; gap: 32px; align-items: center; margin-bottom: 64px; padding-bottom: 64px; border-bottom: 1px solid #1f1f1f; }}
  @media (max-width: 720px) {{ header.hero {{ grid-template-columns: 1fr; }} }}
  .hero-img {{ width: 100%; aspect-ratio: 4/3; background: #000; overflow: hidden; box-shadow: var(--shadow); }}
  .hero-img img {{ width: 100%; height: 100%; object-fit: cover; }}
  .eyebrow {{ color: var(--amber); font-size: 11px; letter-spacing: 0.2em; text-transform: uppercase; margin-bottom: 12px; }}
  h1 {{ font-family: var(--display); font-weight: 700; font-size: 52px; line-height: 1.0; letter-spacing: 0.02em; text-transform: uppercase; margin-bottom: 16px; }}
  h1 .accent {{ color: var(--amber); }}
  .tagline {{ font-size: 16px; color: var(--fg-dim); margin-bottom: 24px; max-width: 480px; }}
  .price-row {{ display: flex; align-items: baseline; gap: 16px; margin-bottom: 24px; }}
  .price {{ font-family: var(--display); font-size: 36px; color: var(--amber); }}
  .price-note {{ font-size: 11px; color: var(--fg-faint); letter-spacing: 0.1em; text-transform: uppercase; }}
  .cta {{ display: inline-block; padding: 18px 28px; background: var(--amber); color: #000; text-decoration: none; font-weight: 700; font-size: 14px; letter-spacing: 0.1em; text-transform: uppercase; box-shadow: var(--shadow); cursor: pointer; transition: transform 0.05s ease; }}
  .cta:active {{ transform: translate(3px, 3px); box-shadow: 3px 3px 0 #000; }}
  .quick-stats {{ display: flex; gap: 20px; margin-top: 24px; flex-wrap: wrap; font-size: 11px; color: var(--fg-dim); text-transform: uppercase; letter-spacing: 0.1em; }}
  .quick-stats div b {{ color: var(--fg); display: block; font-size: 14px; }}
  section {{ margin-bottom: 64px; }}
  h2 {{ font-family: var(--display); font-weight: 700; font-size: 36px; letter-spacing: 0.03em; text-transform: uppercase; color: var(--amber); margin-bottom: 24px; }}
  h3 {{ font-family: var(--display); font-size: 20px; letter-spacing: 0.05em; text-transform: uppercase; color: var(--fg); margin-bottom: 12px; }}
  p {{ color: var(--fg-dim); margin-bottom: 12px; max-width: 720px; }}
  .gallery {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; }}
  @media (max-width: 720px) {{ .gallery {{ grid-template-columns: 1fr 1fr; }} }}
  .gallery img {{ width: 100%; aspect-ratio: 4/3; object-fit: cover; box-shadow: 4px 4px 0 #000; transition: transform 0.1s; }}
  .gallery img:hover {{ transform: translate(-2px, -2px); box-shadow: 6px 6px 0 #000; }}
  footer {{ padding-top: 48px; border-top: 1px solid #1f1f1f; margin-top: 80px; font-size: 11px; color: var(--fg-faint); text-align: center; letter-spacing: 0.1em; text-transform: uppercase; }}
  footer a {{ color: var(--amber); text-decoration: none; }}
  audio {{ width: 100%; filter: invert(0.9) hue-rotate(180deg); }}
</style>
</head>
<body>

<header style="text-align: center; padding: 96px 0 64px; border-bottom: 1px solid #1f1f1f; margin-bottom: 64px;">
    <div class="eyebrow">spacepit · sample pack · vol 1</div>
    <h1 style="font-size: 96px; margin-bottom: 32px;">
      {xml_escape(name.split(' ')[0]) if ' ' in name else 'Moog'}<br/>
      <span class="accent">{xml_escape(name.split(' ')[-1])}</span>
    </h1>
    <p class="tagline" style="margin: 0 auto 32px; text-align: center; max-width: 560px;">A definitive sample pack. {ms_count} multisamples + {loop_count} tempo-tagged loops + DAW-ready presets. Captured by Nick Hook at thespacepit studio.</p>
    <div style="display: flex; gap: 24px; align-items: center; justify-content: center; margin-bottom: 24px; flex-wrap: wrap;">
      <div class="price">$39</div>
      <div class="price-note">launch · royalty-free</div>
      <a class="cta" href="#buy">Buy on Gumroad</a>
    </div>
    <div class="quick-stats" style="justify-content: center;">
      <div><b>{ms_count}</b> Multisamples</div>
      <div><b>{loop_count}</b> Loops</div>
      <div><b>11</b> Patches</div>
      <div><b>16</b> Presets</div>
    </div>
</header>

<section style="background: linear-gradient(180deg, var(--bg-2) 0%, var(--bg) 100%); padding: 32px; border: 2px solid var(--amber); margin-bottom: 64px;">
  <div style="display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 16px;">
    <h3 style="color: var(--amber);">▶ 46-second pack tour</h3>
    <div style="font-size: 11px; color: var(--fg-faint); letter-spacing: 0.1em; text-transform: uppercase;">5 loops · crossfaded</div>
  </div>
  <p style="margin-bottom: 16px;">Five loops from the pack crossfaded: house · hip-hop · dub · trap · future R&amp;B. Same Moog Grandmother throughout.</p>
  <audio controls preload="auto" src="demo.wav"></audio>
</section>

<section>
  <div style="display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 16px; flex-wrap: wrap; gap: 16px;">
    <h2 style="margin: 0;">Browse all <span id="loop-count">{loop_count}</span> loops</h2>
    <div style="display: flex; gap: 8px; align-items: center;">
      <label style="font-size: 11px; color: var(--fg-dim); text-transform: uppercase; letter-spacing: 0.1em;">filter:</label>
      <select id="loop-filter" style="background: #111; color: var(--fg); border: 1px solid #333; padding: 6px 10px; font-family: var(--mono); font-size: 12px;"><option value="">all genres</option></select>
      <select id="tempo-filter" style="background: #111; color: var(--fg); border: 1px solid #333; padding: 6px 10px; font-family: var(--mono); font-size: 12px;">
        <option value="">all tempos</option>
        <option value="60-90">60–90 BPM</option>
        <option value="91-120">91–120 BPM</option>
        <option value="121-140">121–140 BPM</option>
        <option value="141-200">141+ BPM</option>
      </select>
    </div>
  </div>
  <p>Every loop in the pack with audio preview, tempo, and key.</p>
  <div id="loop-browser" style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; max-height: 520px; overflow-y: auto; padding-right: 8px; border: 1px solid #1f1f1f;"></div>
</section>

<section>
  <h2>From the bench</h2>
  <p style="margin-bottom: 8px;">Behind-the-scenes shots from the capture session. <em>Proper cover photography coming in v1.1.</em></p>
  <div class="gallery" style="opacity: 0.85;">
    <img src="images/IMG_3323.JPG" />
    <img src="images/IMG_3321.JPG" />
    <img src="images/IMG_3315.JPG" />
    <img src="images/IMG_3314.JPG" />
    <img src="images/IMG_3322.JPG" />
    <img src="images/IMG_3313.JPG" />
  </div>
</section>

<section id="buy">
  <h2>Get the pack</h2>
  <div style="background: var(--bg-2); padding: 32px; border-left: 3px solid var(--amber);">
    <div style="display: flex; align-items: baseline; gap: 16px; margin-bottom: 16px;">
      <div class="price">$39</div>
      <div class="price-note">{ms_count} multisamples · {loop_count} loops · 16 presets · royalty-free</div>
    </div>
    <a class="cta" href="https://gumroad.com/PLACEHOLDER">Buy on Gumroad →</a>
  </div>
</section>

<footer>© spacepit · captured by nick hook · <a href="https://thespacepit.com">thespacepit.com</a></footer>

<script>
(async () => {{
  const res = await fetch('pack-inventory.json');
  const data = await res.json();
  const loops = data.loops || [];
  document.getElementById('loop-count').textContent = loops.length;
  const allTags = new Set();
  loops.forEach(l => (l.tags || []).forEach(t => allTags.add(t)));
  const filterEl = document.getElementById('loop-filter');
  [...allTags].sort().forEach(t => {{
    const opt = document.createElement('option'); opt.value = t; opt.textContent = t; filterEl.appendChild(opt);
  }});
  function render() {{
    const browser = document.getElementById('loop-browser');
    browser.innerHTML = '';
    const tagFilter = filterEl.value;
    const tempoFilter = document.getElementById('tempo-filter').value;
    let [loBpm, hiBpm] = [0, 999];
    if (tempoFilter) [loBpm, hiBpm] = tempoFilter.split('-').map(Number);
    const filtered = loops.filter(l => {{
      if (tagFilter && !(l.tags || []).includes(tagFilter)) return false;
      if (tempoFilter && l.bpm && (l.bpm < loBpm || l.bpm > hiBpm)) return false;
      return true;
    }});
    if (!filtered.length) {{ browser.innerHTML = '<div style="padding: 20px; color: var(--fg-faint); grid-column: 1/-1;">No loops match.</div>'; return; }}
    filtered.forEach(l => {{
      const card = document.createElement('div');
      card.style.cssText = 'background: var(--bg-2); padding: 10px 14px; border-left: 2px solid var(--amber); display: flex; flex-direction: column; gap: 4px;';
      const tags = (l.tags || []).slice(0, 3).join(' · ');
      card.innerHTML = `<div style="display: flex; justify-content: space-between; align-items: baseline; gap: 8px;"><div style="font-size: 13px; color: var(--fg); font-weight: 700;">${{l.name}}</div><div style="font-size: 10px; color: var(--amber); letter-spacing: 0.1em; white-space: nowrap;">${{l.bpm || '?'}} BPM ${{l.key ? '· ' + l.key : ''}}</div></div><div style="font-size: 10px; color: var(--fg-faint);">${{tags}}</div><audio controls preload="none" src="audio/loops/${{l.filename}}" style="width: 100%; height: 28px; margin-top: 4px;"></audio>`;
      browser.appendChild(card);
    }});
  }}
  filterEl.addEventListener('change', render);
  document.getElementById('tempo-filter').addEventListener('change', render);
  render();
}})();
</script>
</body>
</html>
'''


def _normalize_chains(manifest: dict) -> list[dict]:
    """Return chains_captured as a list of dicts, even if manifest still has legacy string list.
    Backward-compat shim so this function works whether the manifest has been migrated yet."""
    chains = manifest.get("chains_captured", ["raw"])
    if not chains:
        return []
    if isinstance(chains[0], dict):
        return chains
    # Legacy string-list form — promote to minimal objects
    return [{"name": c, "signal_chain": None, "description": None, "credit_line": None} for c in chains]


def _render_chains_section(manifest: dict) -> str:
    """Render the 'Signal chains' Markdown section for the README.
    Every shipped sample carries its provenance — this is the moat in writing."""
    chains = _normalize_chains(manifest)
    if not chains:
        return ""
    lines = ["## Signal chains", "", "Every sample in this pack carries its provenance. Where it came from, exactly:", ""]
    for ch in chains:
        cname = ch.get("name", "raw")
        signal_chain = ch.get("signal_chain")
        desc = ch.get("description")
        credit = ch.get("credit_line")
        studio = ch.get("studio")
        lines.append(f"### `{cname}` chain")
        lines.append("")
        if desc:
            lines.append(f"*{desc}*")
            lines.append("")
        if signal_chain:
            lines.append("**Path:**")
            lines.append("")
            lines.append(f"```")
            lines.append(signal_chain)
            lines.append(f"```")
            lines.append("")
        if studio:
            lines.append(f"**Captured at:** {studio}")
            lines.append("")
        if credit:
            lines.append(f"**Credit line** (use this when crediting samples in your work):")
            lines.append("")
            lines.append(f"> {credit}")
            lines.append("")
        lines.append("")
    return "\n".join(lines)


def build_readme(manifest: dict, ms_count: int, loop_count: int, photo_count: int,
                 sfz_count: int, dspreset_count: int, vol: int) -> str:
    name = manifest.get("name", "Unknown")
    slug = manifest.get("slug", "unknown")
    patches = manifest.get("patches", [])

    patch_list = "\n".join(
        f"- **{p['name']}** — {p.get('notes', '')}" for p in patches
    )

    chains_section = _render_chains_section(manifest)
    chains_summary = ", ".join(f"`{c['name']}`" for c in _normalize_chains(manifest))

    return f"""# spacepit — {name} (Vol {vol})

> A definitive sample pack of the {name}. Multisamples, loops, chord progressions, hi-hat suites, and DAW-ready presets. Captured by Nick Hook at the spacepit.
> {f"Chains included: {chains_summary}" if chains_summary else ""}

## What's in the box

- **{ms_count} multisample WAVs** — every patch captured chromatically across the keyboard at 24-bit / 48 kHz
- **{loop_count} tempo-tagged loops** — chord progressions, dub holds, hi-hat suites across 60-174 BPM
- **{sfz_count} SFZ presets** — universal cross-DAW (Decent Sampler, Sforzando, Bitwig, Logic via plugins)
- **{dspreset_count} Decent Sampler `.dspreset` presets** — free + popular indie sampler
- **{photo_count} hero photos** of the actual gear, color-graded in the spacepit visual language
- **patch notes documentation** — every patch's panel positions + vibe

{chains_section}

## How to load

### Decent Sampler (free, recommended)
1. Download Decent Sampler from [decentsamples.com](https://www.decentsamples.com/product/decent-sampler-plugin/) (free)
2. Open `instruments/decent-sampler/<patch>.dspreset` from this pack
3. Play

### SFZ (any compatible sampler)
1. Most samplers support SFZ — Plogue Sforzando (free), Bitwig, FL Studio, Logic via plugin
2. Open `instruments/sfz/<patch>.sfz` in your sampler
3. Play

### Ableton Live (Sampler + Instrument Rack)
Open any `.adg` file in `instruments/ableton/` — drops directly into Ableton as an Instrument Rack with multisamples mapped + envelopes set.

## Patches included

{patch_list}

## Loops included

In `audio/loops/`:
- **Chord progressions** — 25+ progressions across house, hip-hop, soul, dub, jazz, lofi, R&B, trap, garage, D&B
- **Hi-hat suite** — `trap-140_*` files: 1/4, 1/8, 1/16, 1/32, and a random fill at 140 BPM
- **Dub holds** — slow held chord arps with spring reverb tails

Tempos span 60-174 BPM. All loops are tempo-tagged in the filename for drag-and-drop into any DAW.

## File naming conventions

**Multisamples**: `{slug}_<patch>_<note>_v<velocity>_rr<roundrobin>.wav`
- e.g. `{slug}_moogbass_a2_v100_rr1.wav`
- Note names use lowercase scientific pitch (`c4` = middle C = MIDI 60)

**Loops**: `<name>_<bpm>bpm_<bars>bars.wav`
- e.g. `chicago-house_120bpm_8bars.wav`

## License

Royalty-free for use in your music productions. Do not resell or redistribute the raw samples. When crediting, use the credit line listed under the chain you used (see "Signal chains" above) — appreciated but not required.

## Credits

Captured + designed by **Nick Hook** at the spacepit, NYC. Built with [the bench](https://github.com/hookemon/spacepit-sample-bank) — open-source capture pipeline by the spacepit.

[thespacepit.com](https://thespacepit.com) · [@nickhook](https://instagram.com/nickhook) · [@thespacepit](https://instagram.com/thespacepit)
"""


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\naborted.")
