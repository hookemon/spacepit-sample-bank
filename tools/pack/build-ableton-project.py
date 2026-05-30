#!/usr/bin/env python3
"""Package Ableton presets into a PORTABLE Ableton Live Project (Samples-From-Mars layout).

Every sample reference is rewritten to RelativePathType=3 + "Samples/Imported/<file>" — Ableton's
own project-relative format, verified to load AND survive a move/send. This is what makes the pack
open clean on a friend/manager's machine instead of landing as broken links.

Sources, in priority order:
  --touched-dir   your hand-finished presets (User Library) — these WIN. Renamed back to the
                  original factory name + filed by category. Only the sample <FileRef> links are
                  rewritten; your loop/envelope/tuning work is untouched.
  --release       the bench-built presets — used only for patches you haven't hand-touched yet.

Each preset is identified by its samples (slug) / name against the manifest, so it gets the right
original name (e.g. "A58 · Dance Sweep" even if you saved it as "hook-dance-sweep") and category.

Layout produced:
  <pack>/Ableton Live/<pack>/
    ├── Ableton Project Info/      (empty folder — marks the folder as a Live Project)
    ├── Presets/<Category>/        (the .adg instruments, grouped by manifest role)
    └── Samples/Imported/          (every WAV, bundled in one place)

Built to a .staging twin first, then swapped in — so presets whose samples point into the old pack
stay readable during the build.

Usage:
  build-ableton-project.py --touched-dir "~/Music/Ableton/User Library/Presets/Instruments/Instrument Rack/JP8000" \
      --release releases/spacepit-jp8000-vol1 --manifest instruments/jp8000/manifest.json \
      --pack-name "spacepit JP-8000 Vol 1" --out ~/Desktop
"""
import argparse
import gzip
import json
import re
import shutil
from pathlib import Path

ROLE_FOLDER = {
    "lead": "Leads", "bass": "Bass", "pad": "Pads", "keys": "Keys", "organ": "Keys",
    "sfx": "FX", "synth": "Synths", "arp": "Synths", "pluck": "Synths",
    "drums": "Drums", "perc": "Perc",
}
NOTE_TAIL = re.compile(r"_[a-gA-G]s?-?\d+_v\d+.*$")     # "..._a2_v100_rr1.wav" tail


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def identify(stem, xml, by_name, by_pname):
    """Which manifest patch is this preset? Try sample-slug, then .adg name, then fuzzy preset-name."""
    fns = [Path(p).name for p in re.findall(r'Value="([^"]*\.wav)"', xml)]
    slug = NOTE_TAIL.sub("", re.sub(r"^jp8000_", "", fns[0])) if fns else ""
    for k in (slug, stem):
        if k in by_name:
            return by_name[k]
    for c in (re.sub(r"^hook-", "", slug), slugify(stem)):
        if c in by_pname:
            return by_pname[c]
    return None


def repackage(xml, imported, samples_abs, repo_index, bundled, missing):
    """Bundle each referenced WAV into Samples/Imported (from its own <Path>, else the repo by name)
    and rewrite the ref to type-3 project-relative."""
    def fix(mo):
        b = mo.group(0)
        rm = re.search(r'<RelativePath Value="(?:[^"]*/)?([^"/]+\.wav)" />', b)
        pm = re.search(r'<Path Value="([^"]*\.wav)" />', b)
        if not (rm or pm):
            return b                                            # the rack's .adv ref, not a sample
        fn = rm.group(1) if rm else Path(pm.group(1)).name
        src = Path(pm.group(1)) if (pm and Path(pm.group(1)).exists()) else repo_index.get(fn)
        if src and src.exists():
            if fn not in bundled:
                shutil.copy2(src, imported / fn)
                bundled.add(fn)
        else:
            missing.append(fn)
        b = re.sub(r'<RelativePathType Value="-?\d+" />', '<RelativePathType Value="3" />', b)
        b = re.sub(r'<RelativePath Value="[^"]*" />', f'<RelativePath Value="Samples/Imported/{fn}" />', b)
        b = re.sub(r'<Path Value="[^"]*\.wav" />', f'<Path Value="{samples_abs}/{fn}" />', b)
        return b
    xml = re.sub(r"<FileRef>.*?</FileRef>", fix, xml, flags=re.S)
    # drop machine-path source-preset lineage pointers (any User Library .adv/.adg "where this came
    # from" tag, in whatever element); the devices themselves are embedded, so nothing is lost
    xml = re.sub(r'(Value=)"/Users/[^"]*\.(?:adv|adg)"', r'\1""', xml)
    return xml


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--touched-dir", default=None, help="your hand-finished presets (primary source)")
    ap.add_argument("--release", required=True, help="release dir (bench presets + sample WAVs)")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--pack-name", required=True)
    ap.add_argument("--out", default="~/Desktop")
    a = ap.parse_args()

    rel = Path(a.release).expanduser()
    out = Path(a.out).expanduser()
    pack = out / a.pack_name
    staging = out / (a.pack_name + ".staging")
    if staging.exists():
        shutil.rmtree(staging)
    proj = staging / "Ableton Live" / a.pack_name
    (proj / "Ableton Project Info").mkdir(parents=True)
    imported = proj / "Samples" / "Imported"
    imported.mkdir(parents=True)
    # absolute fallback <Path> must point at the FINAL location (post-swap), never the .staging twin
    samples_abs = str(pack / "Ableton Live" / a.pack_name / "Samples" / "Imported")

    man = json.loads(Path(a.manifest).read_text())
    by_name = {p["name"]: p for p in man.get("patches", [])}
    by_pname = {slugify(p.get("preset_name", "")): p for p in man.get("patches", []) if p.get("preset_name")}
    repo_index = {}
    for base in (rel / "audio" / "multisamples", Path("instruments/jp8000/patches")):
        if base.exists():
            for w in base.rglob("*.wav"):
                repo_index.setdefault(w.name, w)

    bundled, missing, done, rows = set(), [], set(), []

    def process(adg, tag):
        xml = gzip.decompress(adg.read_bytes()).decode("utf-8", "replace")
        patch = identify(adg.stem, xml, by_name, by_pname)
        if not patch:
            rows.append((adg.stem, "—", "UNMAPPED"))
            return
        canon = f"{patch.get('preset_position', '')} · {patch.get('preset_name', '')}".strip(" ·")
        if canon in done:
            return                                              # touched version already won
        cat = ROLE_FOLDER.get((patch.get("role") or "").lower(), "Synths")
        xml2 = repackage(xml, imported, samples_abs, repo_index, bundled, missing)
        (proj / "Presets" / cat).mkdir(parents=True, exist_ok=True)
        (proj / "Presets" / cat / f"{canon}.adg").write_bytes(gzip.compress(xml2.encode("utf-8")))
        done.add(canon)
        rows.append((adg.stem, f"{cat}/{canon}", tag))

    if a.touched_dir:
        for adg in sorted(Path(a.touched_dir).expanduser().glob("*.adg")):
            process(adg, "touched")
    for adg in sorted((rel / "instruments" / "ableton" / "Sampler").glob("*.adg")):
        process(adg, "bench")

    # swap staging -> pack (now that all source samples have been copied in)
    if pack.exists():
        shutil.rmtree(pack)
    staging.rename(pack)

    print(f"✓ {pack}")
    for src, dest, tag in rows:
        mark = "✋" if tag == "touched" else ("🔧" if tag == "bench" else "⚠️")
        print(f"   {mark} {src:24} → {dest}")
    print(f"\n   {sum(1 for r in rows if r[2]=='touched')} touched · "
          f"{sum(1 for r in rows if r[2]=='bench')} bench · {len(bundled)} WAVs bundled")
    if missing:
        print(f"   ⚠ {len(missing)} sample(s) not found: {sorted(set(missing))[:4]}…")


if __name__ == "__main__":
    main()
