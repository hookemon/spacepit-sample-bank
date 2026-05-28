#!/usr/bin/env python3
"""Scrape JP-8000 preset list from Roland's official Patch Listing Faxback PDF.

Source: https://static.roland.com/assets/media/pdf/JP8000PT.pdf (Faxback #10307, 1998).

The PDF is laid out as a table where each row carries a row-label like "11/12"
on the left and THREE columns of "P:A__ Name" entries on the right. pdfplumber's
default `extract_text()` joins all three columns into one line, which is why the
earlier scrape collapsed three presets into a single `name` field.

This script uses `extract_words()` (which gives bounding boxes per token),
groups words by their x-coordinate band into the correct column, then walks the
result top-to-bottom in column order to recover all 128 presets in the proper
A11..A88, B11..B88 sequence.

Output schema (one entry per preset):
    {
        "id": "P:A11",
        "name": "Spit'n Slide Bs",
        "bank": "A",
        "position": 0,
        "program_change": 0,
        "bank_msb": 80,
        "bank_lsb": 0,
        "category": "bass",
        "flags": []
    }

Run:
    python scrape_jp8000_presets.py
        # downloads PDF to /tmp/JP8000PT.pdf if missing, writes JSON
        # to data/gearbase/presets/jp8000.json under the repo root.

Adaptable for other Roland Faxback patch lists — point PDF_URL at the new file
and re-tune COLUMN_BAND_FRAC if the column layout differs.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

import pdfplumber

PDF_URL = "https://static.roland.com/assets/media/pdf/JP8000PT.pdf"
PDF_PATH = Path("/tmp/JP8000PT.pdf")
# Fallback locations if a previous run cached the PDF elsewhere
PDF_FALLBACKS = [Path("/tmp/jp8000/patches.pdf")]

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_PATH = REPO_ROOT / "data" / "gearbase" / "presets" / "jp8000.json"

# --- Category heuristics --------------------------------------------------- #
# Best-effort tag inferred from the preset name. Falls back to "synth".
CATEGORY_RULES: list[tuple[str, str]] = [
    # order matters — first hit wins
    (r"\b(bass|bs|sub|808)\b", "bass"),
    (r"\b(lead|saw|squar|solo|portam|portamen)\b", "lead"),
    (r"\b(pad|atmo|str(in)?g?s?|warm|drift|wash|airy|haze)\b", "pad"),
    (r"\b(brass|horn|trumpet|tromb)\b", "brass"),
    (r"\b(piano|rhodes|wurly|wurl|ep|clav|harps)\b", "keys"),
    (r"\b(organ|hammond|b3)\b", "organ"),
    (r"\b(bell|mallet|marimba|kalimba|glock|vibe)\b", "keys"),
    (r"\b(sfx|fx|noise|hit|impact|swoosh|zap|riser|drop|sweep|effect)\b", "sfx"),
    (r"\b(arp|seq|sequenc|bpm|pulse|loop)\b", "synth"),
    (r"\b(perc|drum|kick|snare|hat|clap|tom|cymbal)\b", "drum"),
    (r"\b(vox|vocal|choir|voice|aaah|ooh)\b", "vox"),
]


def categorize(name: str) -> str:
    lower = name.lower()
    for pattern, tag in CATEGORY_RULES:
        if re.search(pattern, lower):
            return tag
    return "synth"


# --- PDF download ---------------------------------------------------------- #
def ensure_pdf() -> Path:
    if PDF_PATH.exists() and PDF_PATH.stat().st_size > 1000:
        return PDF_PATH
    for fb in PDF_FALLBACKS:
        if fb.exists() and fb.stat().st_size > 1000:
            print(f"Using cached PDF at {fb}", file=sys.stderr)
            return fb
    print(f"Downloading {PDF_URL} -> {PDF_PATH} ...", file=sys.stderr)
    PDF_PATH.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(PDF_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(PDF_PATH, "wb") as f:
        f.write(resp.read())
    return PDF_PATH


# --- PDF parsing ----------------------------------------------------------- #
PRESET_RE = re.compile(r"P:\s*([AB])\s*([1-8])\s*([1-8])\s*[:\-]?\s*(.+?)\s*$")


def extract_all_words(pdf_path: Path) -> list[dict]:
    """Pull every word from every page with its page index + bbox."""
    out: list[dict] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_idx, page in enumerate(pdf.pages):
            words = page.extract_words(
                x_tolerance=2,
                y_tolerance=2,
                keep_blank_chars=False,
                use_text_flow=False,
            )
            for w in words:
                out.append(
                    {
                        "page": page_idx,
                        "page_width": float(page.width),
                        "text": w["text"],
                        "x0": float(w["x0"]),
                        "x1": float(w["x1"]),
                        "top": float(w["top"]),
                        "bottom": float(w["bottom"]),
                    }
                )
    return out


def assemble_lines(words: list[dict], y_tol: float = 3.0) -> list[list[dict]]:
    """Group words into lines by their `top` coordinate per page."""
    lines: list[list[dict]] = []
    by_page: dict[int, list[dict]] = {}
    for w in words:
        by_page.setdefault(w["page"], []).append(w)
    for page_idx in sorted(by_page):
        page_words = sorted(by_page[page_idx], key=lambda w: (w["top"], w["x0"]))
        current: list[dict] = []
        current_top: float | None = None
        for w in page_words:
            if current_top is None or abs(w["top"] - current_top) <= y_tol:
                current.append(w)
                current_top = w["top"] if current_top is None else current_top
            else:
                lines.append(current)
                current = [w]
                current_top = w["top"]
        if current:
            lines.append(current)
    return lines


def split_line_into_columns(line: list[dict]) -> list[list[dict]]:
    """Split a single visual line into separate column groups by gaps in x."""
    line = sorted(line, key=lambda w: w["x0"])
    if not line:
        return []
    cols: list[list[dict]] = [[line[0]]]
    for prev, curr in zip(line, line[1:]):
        gap = curr["x0"] - prev["x1"]
        if gap > 20:  # large horizontal gap = new column
            cols.append([curr])
        else:
            cols[-1].append(curr)
    return cols


def column_text(col: list[dict]) -> str:
    return " ".join(w["text"] for w in sorted(col, key=lambda w: w["x0"])).strip()


def parse_presets(pdf_path: Path) -> dict[str, str]:
    """Return {id: name} for every preset found in the PDF."""
    words = extract_all_words(pdf_path)
    lines = assemble_lines(words)

    presets: dict[str, str] = {}
    for line in lines:
        cols = split_line_into_columns(line)
        for col in cols:
            text = column_text(col)
            # text should look like "P:A11 Spit'n Slide Bs" (sometimes with stray punct)
            # be permissive: allow "P:A 1 1" or "P:A11:" forms
            m = PRESET_RE.search(text)
            if not m:
                continue
            bank, group, position = m.group(1), m.group(2), m.group(3)
            name = m.group(4).strip()
            # Strip a leading ":" if pdfplumber attached one
            name = name.lstrip(":").strip()
            # Clean trailing garbage like "**"
            name = re.sub(r"\s*\*+\s*$", "", name).strip()
            if not name:
                continue
            pid = f"P:{bank}{group}{position}"
            # First hit wins (the table reads top-to-bottom, left-to-right)
            if pid not in presets:
                presets[pid] = name
    return presets


# --- Position math --------------------------------------------------------- #
def position_index(bank: str, group: int, pos: int) -> int:
    """A11=0, A12=1, ..., A18=7, A21=8, ..., A88=63 (same for Bank B)."""
    return (group - 1) * 8 + (pos - 1)


def all_expected_ids() -> list[str]:
    ids: list[str] = []
    for bank in ("A", "B"):
        for g in range(1, 9):
            for p in range(1, 9):
                ids.append(f"P:{bank}{g}{p}")
    return ids


# --- Main ------------------------------------------------------------------ #
def build_preset_entries(raw: dict[str, str]) -> list[dict]:
    entries: list[dict] = []
    for pid in all_expected_ids():
        bank = pid[2]  # "A" or "B"
        group = int(pid[3])
        pos = int(pid[4])
        position = position_index(bank, group, pos)
        name = raw.get(pid, "").strip()
        entries.append(
            {
                "id": pid,
                "name": name,
                "bank": bank,
                "position": position,
                "program_change": position,
                "bank_msb": 80,
                "bank_lsb": 0 if bank == "A" else 1,
                "category": categorize(name) if name else "synth",
                "flags": [] if name else ["missing-name"],
            }
        )
    return entries


def main() -> None:
    pdf_path = ensure_pdf()
    raw = parse_presets(pdf_path)
    print(f"Parsed {len(raw)} unique preset ids from PDF.", file=sys.stderr)

    entries = build_preset_entries(raw)

    # Preserve existing top-level fields
    existing = json.loads(OUT_PATH.read_text()) if OUT_PATH.exists() else {}
    doc = {
        "gear_slug": existing.get("gear_slug", "jp8000"),
        "gear_name": existing.get("gear_name", "Roland JP-8000"),
        "manufacturer": existing.get("manufacturer", "Roland"),
        "year": existing.get("year", 1996),
        "voices": existing.get("voices", 8),
        "preset_count": len(entries),
        "source": existing.get("source", "Roland Patch Listing Faxback #10307 (1998)"),
        "source_url": existing.get("source_url", PDF_URL),
        "midi_implementation": existing.get(
            "midi_implementation",
            {
                "program_change_receive": True,
                "bank_select": "MSB CC0 = 80; LSB CC32 = 0 (Bank A) | 1 (Bank B)",
            },
        ),
        "presets": entries,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")

    # Reporting
    by_cat: dict[str, int] = {}
    bank_a = bank_b = missing = 0
    for e in entries:
        by_cat[e["category"]] = by_cat.get(e["category"], 0) + 1
        if e["bank"] == "A":
            bank_a += 1
        else:
            bank_b += 1
        if not e["name"]:
            missing += 1
    print(f"Wrote {OUT_PATH} ({len(entries)} presets, A={bank_a}, B={bank_b}, missing_name={missing})", file=sys.stderr)
    for cat in sorted(by_cat):
        print(f"  {cat}: {by_cat[cat]}", file=sys.stderr)


if __name__ == "__main__":
    main()
