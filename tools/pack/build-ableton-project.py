#!/usr/bin/env python3
"""Package built Ableton presets into a PORTABLE Ableton Live Project (Samples-From-Mars layout).

Every sample reference is rewritten to RelativePathType=3 + "Samples/Imported/<file>" — Ableton's
own project-relative format, verified to load AND survive a move/send (the absolute <Path> is a
local convenience; type-3 is what resolves on someone else's machine). This is what makes the pack
open clean for a friend/manager off rip instead of landing as broken links.

Layout produced:

  <pack>/Ableton Live/<pack>/
    ├── Ableton Project Info/      (empty folder — marks the folder as a Live Project)
    ├── Presets/<Category>/        (the .adg instruments, grouped by manifest role)
    └── Samples/Imported/          (every WAV, bundled in one place)

The .adg device settings (loop points, envelopes, tuning — your hand-touches) live elsewhere in the
file and are left untouched; only the sample <FileRef> links are rewritten. So you can hand-finish a
preset, drop it back in instruments/ableton/Sampler/, re-run this, and ship — touches preserved,
links portable.

Usage:
  build-ableton-project.py --release releases/spacepit-jp8000-vol1 \
      --manifest instruments/jp8000/manifest.json \
      --pack-name "spacepit JP-8000 Vol 1" --out ~/Desktop
"""
import argparse
import gzip
import json
import re
import shutil
from pathlib import Path

# manifest role -> Presets subfolder (SFM-style category drawers)
ROLE_FOLDER = {
    "lead": "Leads", "bass": "Bass", "pad": "Pads", "keys": "Keys", "organ": "Keys",
    "sfx": "FX", "synth": "Synths", "arp": "Synths", "pluck": "Synths",
    "drums": "Drums", "perc": "Perc",
}
POS_RE = re.compile(r"^([A-H]\d{1,2})")          # leading bank slot in a preset name, e.g. "A57 · Euro SAW"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--release", required=True, help="release dir (instruments/ableton/Sampler + audio/multisamples)")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--pack-name", required=True)
    ap.add_argument("--out", default="~/Desktop")
    a = ap.parse_args()

    rel = Path(a.release).expanduser()
    sampler = rel / "instruments" / "ableton" / "Sampler"
    pack = Path(a.out).expanduser() / a.pack_name
    if pack.exists():
        shutil.rmtree(pack)
    proj = pack / "Ableton Live" / a.pack_name
    (proj / "Ableton Project Info").mkdir(parents=True)          # empty folder = Live Project marker
    imported = proj / "Samples" / "Imported"
    imported.mkdir(parents=True)
    samples_abs = str(imported)

    # every WAV by basename, so each preset's refs resolve to the real captured file
    wav_index = {w.name: w for w in (rel / "audio" / "multisamples").rglob("*.wav")}

    # bank slot -> category folder, from the manifest roles
    man = json.loads(Path(a.manifest).read_text())
    cat_of = {}
    for p in man.get("patches", []):
        pos = (p.get("preset_position") or "").strip()
        if pos:
            cat_of[pos] = ROLE_FOLDER.get((p.get("role") or "").lower(), "Synths")

    bundled = set()
    missing = []
    counts = {}
    for adg in sorted(sampler.glob("*.adg")):
        m = POS_RE.match(adg.stem)
        cat = cat_of.get(m.group(1), "Synths") if m else "Synths"
        xml = gzip.decompress(adg.read_bytes()).decode("utf-8")

        def fix(mo):
            b = mo.group(0)
            rm = re.search(r'<RelativePath Value="(?:[^"]*/)?([^"/]+\.wav)" />', b)
            if not rm:
                return b                                          # the rack's .adv ref, not a sample
            fn = rm.group(1)
            if fn in wav_index:
                if fn not in bundled:
                    shutil.copy2(wav_index[fn], imported / fn)
                    bundled.add(fn)
            else:
                missing.append(fn)
            b = re.sub(r'<RelativePathType Value="\d+" />', '<RelativePathType Value="3" />', b)
            b = re.sub(r'<RelativePath Value="[^"]*" />', f'<RelativePath Value="Samples/Imported/{fn}" />', b)
            b = re.sub(r'<Path Value="[^"]*" />', f'<Path Value="{samples_abs}/{fn}" />', b)
            return b

        xml2 = re.sub(r"<FileRef>.*?</FileRef>", fix, xml, flags=re.S)
        catdir = proj / "Presets" / cat
        catdir.mkdir(parents=True, exist_ok=True)
        (catdir / adg.name).write_bytes(gzip.compress(xml2.encode("utf-8")))
        counts[cat] = counts.get(cat, 0) + 1

    print(f"✓ {pack}")
    for c in sorted(counts):
        print(f"   Presets/{c}: {counts[c]} preset(s)")
    print(f"   Samples/Imported: {len(bundled)} WAVs")
    if missing:
        print(f"   ⚠ {len(missing)} sample refs had no matching WAV: {sorted(set(missing))[:4]}…")


if __name__ == "__main__":
    main()
