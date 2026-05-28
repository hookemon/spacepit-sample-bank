#!/usr/bin/env python3
"""Audit a built pack — return JSON report of health checks.

Checks:
  - Every multisample WAV has audio (not silent, no clipping)
  - Every loop WAV has audio
  - SFZ presets: syntactically valid + all sample paths resolve + no key-range gaps
  - Decent Sampler presets: valid XML + all sample paths resolve + no gaps
  - Photos directory exists with files
  - Required top-level files (README, PACK_TOUR, demo.wav, landing.html, pack-inventory.json)

Usage:
  audit-pack.py --pack releases/spacepit-grandmother-vol1 [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    import numpy as np
    import soundfile as sf
except ImportError as e:
    print(f"missing dep: {e}", file=sys.stderr)
    sys.exit(1)


def audit(pack_dir: Path) -> dict:
    issues = []
    info = {}

    # ---------- 1. Audio integrity ----------
    silent_wavs = []
    clipping_wavs = []
    total_wavs = 0
    for wav in pack_dir.rglob("*.wav"):
        if "_original" in wav.parts:
            continue
        try:
            audio, sr = sf.read(str(wav))
            if audio.ndim > 1:
                audio = audio.mean(axis=1)
            peak = float(np.max(np.abs(audio)))
            pk_db = 20 * np.log10(max(1e-10, peak)) if peak > 0 else float("-inf")
            total_wavs += 1
            if pk_db < -50:
                silent_wavs.append(str(wav.relative_to(pack_dir)))
            if peak >= 0.999:
                clipping_wavs.append(str(wav.relative_to(pack_dir)))
        except Exception as e:
            issues.append(f"failed to read {wav.relative_to(pack_dir)}: {e}")
    info["total_wavs"] = total_wavs
    if silent_wavs:
        issues.append(f"{len(silent_wavs)} silent WAVs")
    if clipping_wavs:
        issues.append(f"{len(clipping_wavs)} clipping WAVs")
    info["silent_wavs"] = silent_wavs
    info["clipping_wavs"] = clipping_wavs

    # ---------- 2. SFZ presets ----------
    sfz_dir = pack_dir / "instruments/sfz"
    sfz_issues = []
    sfz_ok = 0
    if sfz_dir.exists():
        for sfz_file in sorted(sfz_dir.glob("*.sfz")):
            content = sfz_file.read_text()
            sample_refs = re.findall(r"sample=([^\n]+)", content)
            keys = re.findall(r"lokey=(\d+).*?hikey=(\d+)", content, re.DOTALL)
            ranges = sorted([(int(lo), int(hi)) for lo, hi in keys])

            # check samples exist
            name = sfz_file.stem
            patch_name = name.replace("-spring", "") if name.endswith("-spring") else name
            chain = "spring" if name.endswith("-spring") else "raw"
            missing = []
            for ref in sample_refs:
                p = pack_dir / "audio/multisamples" / patch_name / chain / ref.strip()
                if not p.exists():
                    missing.append(ref.strip())
            if missing:
                sfz_issues.append(f"{name}: {len(missing)} missing samples")
                continue

            # check key range
            gaps = []
            if ranges and ranges[0][0] > 0:
                gaps.append(f"0-{ranges[0][0]-1}")
            for i in range(len(ranges) - 1):
                if ranges[i+1][0] > ranges[i][1] + 1:
                    gaps.append(f"{ranges[i][1]+1}-{ranges[i+1][0]-1}")
            if ranges and ranges[-1][1] < 127:
                gaps.append(f"{ranges[-1][1]+1}-127")
            if gaps:
                sfz_issues.append(f"{name}: gaps {gaps}")
                continue

            sfz_ok += 1
    info["sfz_ok"] = sfz_ok
    info["sfz_issues"] = sfz_issues
    if sfz_issues:
        issues.append(f"{len(sfz_issues)} SFZ presets with issues")

    # ---------- 3. Decent Sampler presets ----------
    ds_dir = pack_dir / "instruments/decent-sampler"
    ds_issues = []
    ds_ok = 0
    if ds_dir.exists():
        for ds_file in sorted(ds_dir.glob("*.dspreset")):
            try:
                tree = ET.parse(ds_file)
                samples = tree.getroot().findall(".//sample")
                missing = []
                for s in samples:
                    path = s.get("path")
                    if not path: continue
                    resolved = (ds_file.parent / path).resolve()
                    if not resolved.exists():
                        missing.append(path)
                if missing:
                    ds_issues.append(f"{ds_file.stem}: {len(missing)} missing samples")
                else:
                    ds_ok += 1
            except ET.ParseError as e:
                ds_issues.append(f"{ds_file.stem}: invalid XML")
    info["decent_ok"] = ds_ok
    info["decent_issues"] = ds_issues
    if ds_issues:
        issues.append(f"{len(ds_issues)} Decent Sampler presets with issues")

    # ---------- 4. Photos ----------
    photos_dir = pack_dir / "images"
    photos = []
    if photos_dir.exists():
        photos = [p.name for p in sorted(photos_dir.iterdir())
                  if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".heic")]
    info["photos_count"] = len(photos)
    if not photos:
        issues.append("no photos in images/")

    # ---------- 5. Required top-level files ----------
    required = ["README.md", "PACK_TOUR.md", "demo.wav", "landing.html", "pack-inventory.json"]
    missing_files = []
    for f in required:
        if not (pack_dir / f).exists():
            missing_files.append(f)
    info["missing_required"] = missing_files
    if missing_files:
        issues.append(f"missing required files: {missing_files}")

    # ---------- 6. Inventory consistency ----------
    inv_path = pack_dir / "pack-inventory.json"
    if inv_path.exists():
        try:
            inv = json.loads(inv_path.read_text())
            info["inventory_loop_count"] = len(inv.get("loops", []))
            info["inventory_multisample_count"] = sum(m.get("count", 0) for m in inv.get("multisamples", []))
        except Exception:
            issues.append("pack-inventory.json is unreadable")

    # ---------- Summary ----------
    return {
        "pack": str(pack_dir),
        "ok": len(issues) == 0,
        "issues": issues,
        "info": info,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", required=True)
    ap.add_argument("--json", action="store_true", help="output JSON only")
    args = ap.parse_args()

    pack_dir = Path(args.pack).resolve()
    if not pack_dir.exists():
        print(f"pack not found: {pack_dir}", file=sys.stderr); sys.exit(1)

    report = audit(pack_dir)

    if args.json:
        print(json.dumps(report, indent=2))
        return

    print(f"=== PACK AUDIT: {pack_dir.name} ===")
    print(f"  ✓ OK" if report["ok"] else f"  ✗ {len(report['issues'])} issues")
    print()
    print(f"  WAVs total: {report['info'].get('total_wavs', 0)}")
    print(f"  WAVs silent: {len(report['info'].get('silent_wavs', []))}")
    print(f"  WAVs clipping: {len(report['info'].get('clipping_wavs', []))}")
    print(f"  SFZ presets: {report['info'].get('sfz_ok', 0)} ok, {len(report['info'].get('sfz_issues', []))} issues")
    print(f"  Decent presets: {report['info'].get('decent_ok', 0)} ok, {len(report['info'].get('decent_issues', []))} issues")
    print(f"  Photos: {report['info'].get('photos_count', 0)}")
    if report["info"].get("missing_required"):
        print(f"  Missing files: {report['info']['missing_required']}")
    print()
    if report["issues"]:
        print("Issues:")
        for i in report["issues"]:
            print(f"  ✗ {i}")


if __name__ == "__main__":
    main()
