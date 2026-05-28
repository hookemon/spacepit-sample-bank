#!/usr/bin/env python3
"""Generate a JSON inventory of a pack — for the landing page browser UI.

Reads the pack folder + matches loop filenames against the pattern library
to get rich metadata (BPM, key, genre tags, vibe). Outputs to pack-inventory.json.

Usage:
  build-inventory.py --pack releases/spacepit-grandmother-vol1
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import soundfile as sf


def find_bank_root() -> Path | None:
    p = Path.cwd().resolve()
    for c in [p, *p.parents]:
        if (c / "instruments").is_dir() and (c / "manifest.template.json").exists():
            return c
    fallback = Path.home() / "projects" / "spacepit-sample-bank"
    if (fallback / "instruments").is_dir():
        return fallback
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", required=True)
    ap.add_argument("--bank-root", default=None)
    args = ap.parse_args()

    pack_dir = Path(args.pack).resolve()
    bank_root = Path(args.bank_root).resolve() if args.bank_root else find_bank_root()

    # Load saved pattern library for rich loop metadata
    patterns_by_name: dict[str, dict] = {}
    if bank_root:
        pat_dir = bank_root / "patterns" / "progressions"
        if pat_dir.exists():
            for p in pat_dir.glob("*.json"):
                try:
                    data = json.loads(p.read_text())
                    patterns_by_name[data.get("name", p.stem)] = data
                except Exception:
                    pass

    # Scan loops
    loops_dir = pack_dir / "audio" / "loops"
    loops = []
    if loops_dir.exists():
        for wav in sorted(loops_dir.glob("*.wav")):
            info = sf.info(str(wav))
            stem = wav.stem
            # parse filename for BPM/bars
            bpm = None
            bars = None
            m = re.search(r"_(\d+)bpm_([\d.]+)bars", stem)
            if m:
                bpm = int(m.group(1))
                bars = float(m.group(2))
            # extract loop name from filename (everything before _<bpm>bpm_)
            loop_name = re.sub(r"_\d+bpm_[\d.]+bars$", "", stem)
            # match to pattern library
            pat = patterns_by_name.get(loop_name)
            entry = {
                "filename": wav.name,
                "name": loop_name,
                "size_bytes": wav.stat().st_size,
                "duration_sec": round(info.duration, 2),
                "sample_rate": info.samplerate,
                "bit_depth": getattr(info, "subtype", "?"),
            }
            if bpm:
                entry["bpm"] = bpm
            if bars:
                entry["bars"] = bars
            if pat:
                entry["key"] = pat.get("key", "")
                entry["vibe"] = pat.get("vibe", "")
                entry["tags"] = pat.get("tags", [])
                if "best_with_spring" in pat:
                    entry["best_with_spring"] = pat["best_with_spring"]
                if "best_with_arp_division" in pat:
                    entry["best_with_arp_division"] = pat["best_with_arp_division"]
            loops.append(entry)

    # Scan multisamples (just count per patch/chain)
    ms_dir = pack_dir / "audio" / "multisamples"
    multisamples = []
    if ms_dir.exists():
        for patch_dir in sorted(ms_dir.iterdir()):
            if not patch_dir.is_dir():
                continue
            for chain_dir in sorted(patch_dir.iterdir()):
                if not chain_dir.is_dir():
                    continue
                wavs = list(chain_dir.glob("*.wav"))
                if wavs:
                    multisamples.append({
                        "patch": patch_dir.name,
                        "chain": chain_dir.name,
                        "count": len(wavs),
                        "directory": f"audio/multisamples/{patch_dir.name}/{chain_dir.name}",
                    })

    # Scan photos
    images_dir = pack_dir / "images"
    photos = []
    if images_dir.exists():
        for img in sorted(images_dir.glob("*.JPG")):
            photos.append({"filename": img.name, "size_bytes": img.stat().st_size})

    inventory = {
        "pack": pack_dir.name,
        "loops": loops,
        "multisamples": multisamples,
        "photos": photos,
        "totals": {
            "loops": len(loops),
            "multisample_files": sum(m["count"] for m in multisamples),
            "patches": len({m["patch"] for m in multisamples}),
            "chains": len(multisamples),
            "photos": len(photos),
        },
    }

    out_path = pack_dir / "pack-inventory.json"
    out_path.write_text(json.dumps(inventory, indent=2))
    print(f"✓ wrote {out_path}")
    print(f"   {len(loops)} loops")
    print(f"   {sum(m['count'] for m in multisamples)} multisample files in {len(multisamples)} chains")
    print(f"   {len(photos)} photos")


if __name__ == "__main__":
    main()
