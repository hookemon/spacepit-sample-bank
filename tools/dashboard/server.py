#!/usr/bin/env python3
"""spacepit capture dashboard — local web UI for sample bank captures.

Run from the bank root:
  .venv/bin/python tools/dashboard/server.py

Then open http://localhost:8001/

REST API:
  GET    /                       → dashboard UI
  GET    /api/instruments        → list of all instruments + progress
  GET    /api/instruments/<slug> → details + state for one instrument
  POST   /api/capture            → trigger a capture (params in JSON body)
  POST   /api/keep               → confirm a capture as a keeper, write sidecar
  POST   /api/discard            → delete a capture
  GET    /audio/<path>           → serve a WAV from the bank
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

try:
    from flask import Flask, jsonify, request, send_from_directory, abort
except ImportError:
    print("Flask not installed. Run: .venv/bin/pip install flask", file=sys.stderr)
    sys.exit(1)


# ---------- paths ----------

BANK_ROOT = Path(__file__).resolve().parent.parent.parent
INSTRUMENTS_DIR = BANK_ROOT / "instruments"
TOOLS_DIR = BANK_ROOT / "tools"
VENV_PY = TOOLS_DIR / "capture" / ".venv" / "bin" / "python"

# Pull in the session composer (tools/synthesis/compose.py) as a library
sys.path.insert(0, str(TOOLS_DIR / "synthesis"))
try:
    import compose as _composer  # type: ignore
except Exception as _e:
    _composer = None
    print(f"  ⚠ Session composer unavailable: {_e}", file=sys.stderr)


# ---------- helpers ----------

def get_instrument_manifest(slug: str) -> dict | None:
    path = INSTRUMENTS_DIR / slug / "manifest.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def count_files(directory: Path, pattern: str = "*.wav") -> int:
    if not directory.exists():
        return 0
    return len(list(directory.glob(pattern)))


def compute_instrument_progress(slug: str) -> dict:
    """Compute the completion state for one instrument."""
    instr_dir = INSTRUMENTS_DIR / slug
    if not instr_dir.exists():
        return {}

    manifest = get_instrument_manifest(slug) or {}

    # patches: each has raw/spring chains.
    # Wrapped in try/except — if patches/ is a symlink to an external drive the
    # server can't read (launchd sandboxing), skip gracefully instead of 500-ing
    # the whole instrument list.
    patches_dir = instr_dir / "patches"
    patches = []
    if patches_dir.exists():
        try:
            for patch_dir in sorted(patches_dir.iterdir()):
                if not patch_dir.is_dir():
                    continue
                chains = {}
                try:
                    for chain_dir in patch_dir.iterdir():
                        if chain_dir.is_dir():
                            n = count_files(chain_dir, "*.wav")
                            if n > 0:
                                chains[chain_dir.name] = n
                except (PermissionError, OSError):
                    pass
                patches.append({"name": patch_dir.name, "chains": chains})
        except (PermissionError, OSError) as e:
            print(f"  ⚠ can't read {patches_dir} ({e}) — external drive access? showing 0 captures", file=sys.stderr)

    # loops
    loops_raw = count_files(instr_dir / "loops" / "raw")

    # sweeps
    sweeps_raw = count_files(instr_dir / "sweeps" / "raw")

    # photos
    photos_dir = instr_dir / "photos"
    photos = (count_files(photos_dir, "*.jpg") + count_files(photos_dir, "*.JPG")
              + count_files(photos_dir, "*.jpeg") + count_files(photos_dir, "*.png"))

    # targets (defaults — could come from manifest later)
    tier = manifest.get("tier", "standard")
    targets = {
        "standard": {"patches": 5, "loops": 15, "sweeps": 8, "photos": 5},
        "gold":     {"patches": 11, "loops": 30, "sweeps": 15, "photos": 8},
        "quick":    {"patches": 2, "loops": 5, "sweeps": 0, "photos": 3},
    }.get(tier, {"patches": 5, "loops": 15, "sweeps": 8, "photos": 5})

    return {
        "slug": slug,
        "name": manifest.get("name", slug),
        "category": manifest.get("category", "?"),
        "tier": tier,
        "patches": patches,
        # total = how many patches the instrument HAS (from the manifest), not how many
        # folders exist on disk yet — so the progress reads "1/12" not "1/1" mid-capture.
        "patch_count": len(manifest.get("patches", [])) or len(patches),
        "loops_count": loops_raw,
        "sweeps_count": sweeps_raw,
        "photos_count": photos,
        "targets": targets,
        "manifest": manifest,
    }


# ---------- Flask app ----------

app = Flask(__name__, static_folder="static", static_url_path="/static")

# in-memory capture state (cleared on server restart)
last_capture: dict = {"path": None, "instrument": None, "params": None}
capture_lock = threading.Lock()
current_capture_proc: subprocess.Popen | None = None  # track running capture for stop

# --- Monitor (live audio passthrough from TX-6 input to system output) ---
# When enabled, server bridges the TX-6 input to the Mac's default output device
# (W+, speakers, AirPods, whatever) so Nick can hear the synth WITHOUT plugging
# headphones into the TX-6 itself or routing through Ableton.
# Auto-mutes during captures so the live monitor doesn't bleed back into recordings.
monitor_stop_flag = threading.Event()
monitor_thread: "threading.Thread | None" = None
monitor_state: dict = {
    "running": False,
    "input_device": None,
    "input_channels": None,
    "muted_by_capture": False,
    "last_error": None,
}

# Session log — last N captures with status. Persisted to disk so it survives restart.
LOG_FILE = BANK_ROOT / "tools" / "dashboard" / "capture-log.json"
capture_log: list[dict] = []
MAX_LOG_ENTRIES = 30


def _load_log():
    global capture_log
    if LOG_FILE.exists():
        try:
            capture_log = json.loads(LOG_FILE.read_text())
        except Exception:
            capture_log = []


def _save_log():
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        LOG_FILE.write_text(json.dumps(capture_log, indent=2))
    except Exception as e:
        print(f"failed to save capture log: {e}", file=sys.stderr)


def log_capture(entry: dict):
    """Add to the head of the log, capped at MAX_LOG_ENTRIES, persist to disk."""
    capture_log.insert(0, entry)
    while len(capture_log) > MAX_LOG_ENTRIES:
        capture_log.pop()
    _save_log()


def log_update(wav_path: str, **updates):
    """Update an existing log entry's status + persist."""
    for entry in capture_log:
        if entry.get("wav_path") == wav_path:
            entry.update(updates)
            _save_log()
            return True
    return False


def log_remove(wav_path: str) -> bool:
    """Drop an entry from the log entirely so the row disappears from the captures list."""
    global capture_log
    before = len(capture_log)
    capture_log = [e for e in capture_log if e.get("wav_path") != wav_path]
    if len(capture_log) != before:
        _save_log()
        return True
    return False


def _move_to_trash(path: Path) -> bool:
    """Move a file to the macOS Trash — 'gone' from the bench but recoverable, never shredded."""
    try:
        if not path.exists():
            return False
        trash = Path.home() / ".Trash"
        dest = trash / path.name
        n = 1
        while dest.exists():
            dest = trash / f"{path.stem}_{n}{path.suffix}"
            n += 1
        path.rename(dest)
        return True
    except Exception as e:
        print(f"trash move failed for {path}: {e}", file=sys.stderr)
        return False


# Load existing log on startup
_load_log()


@app.route("/")
def index():
    # Auto cache-bust the JS/CSS by their file mtime, so editing app.js always loads the
    # new version on a normal refresh — no more "the bench still shows the old thing".
    html = (Path(app.static_folder) / "index.html").read_text()
    for asset in ("app.js", "style.css"):
        f = Path(app.static_folder) / asset
        if f.exists():
            v = int(f.stat().st_mtime)
            html = html.replace(f"/static/{asset}", f"/static/{asset}?v={v}")
    return app.response_class(html, mimetype="text/html")


@app.route("/dreamer")
def dreamer_page():
    """the studio dreamer — generative band that plays your gear. Its own little side app."""
    f = Path(app.static_folder) / "dreamer.html"
    if not f.exists():
        return "dreamer.html missing", 404
    html = f.read_text()
    return app.response_class(html, mimetype="text/html")


@app.route("/api/devices")
def list_devices():
    """Return MIDI output ports + audio input devices currently available on this machine.

    Truly hot-plug aware: bypasses mido's cache by calling rtmidi directly, so when
    you UNPLUG gear (Grandmother out, MIO in), the list updates immediately. Without
    this, unplugged devices appear as ghost entries in the dropdown.
    """
    midi_ports = []
    try:
        # Go directly to rtmidi — bypass mido's Python-level cache. Creating a
        # fresh MidiOut object forces rtmidi to re-poll Core MIDI for the current
        # port list, so unplugged devices disappear and newly-plugged ones appear.
        import rtmidi
        midi_out = rtmidi.MidiOut()
        try:
            n_ports = midi_out.get_port_count()
            midi_ports = [midi_out.get_port_name(i) for i in range(n_ports)]
        finally:
            try:
                midi_out.close_port()
            except Exception:
                pass
            del midi_out
    except Exception as e:
        # Fall back to mido if rtmidi isn't available directly
        try:
            import mido
            midi_ports = mido.get_output_names()
        except Exception:
            midi_ports = []

    # sounddevice cache: _terminate + _initialize forces a re-poll of Core Audio
    audio_devs = []
    try:
        import sounddevice as sd
        try:
            sd._terminate()
            sd._initialize()
        except Exception:
            pass
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                audio_devs.append({
                    "index": i,
                    "name": d["name"],
                    "channels": d["max_input_channels"],
                    "sample_rate": int(d.get("default_samplerate", 0)),
                })
    except Exception as e:
        return jsonify({"error": str(e), "midi_ports": midi_ports, "audio_devices": []}), 500

    return jsonify({"midi_ports": midi_ports, "audio_devices": audio_devs})


@app.route("/api/instruments/<slug>/settings", methods=["GET", "POST"])
def instrument_settings(slug):
    """Get or update capture settings + polyphony hint for an instrument."""
    manifest_path = INSTRUMENTS_DIR / slug / "manifest.json"
    if not manifest_path.exists():
        return jsonify({"error": f"instrument not found: {slug}"}), 404

    manifest = json.loads(manifest_path.read_text())

    if request.method == "GET":
        # MIDI routing graph: resolve which port to ACTUALLY use right now.
        # 1) If a port matching synth_name is currently visible (direct USB), prefer that.
        # 2) Else fall back to the saved midi_port (which may be a routing interface like "mio").
        # 3) If neither is visible, leave it blank — the UI will warn.
        synth_name = manifest.get("synth_name") or manifest.get("name") or ""
        saved_port = manifest.get("capture_settings", {}).get("midi_port", "")
        resolved_port, resolved_via = _resolve_midi_port(synth_name, saved_port)
        return jsonify({
            "capture_settings": manifest.get("capture_settings") or {},
            "polyphony": manifest.get("polyphony") or "unknown",
            "voices": manifest.get("voices"),
            # New routing-graph fields:
            "synth_name": synth_name,
            "resolved_port": resolved_port,  # what to actually send MIDI to right now
            "resolved_via": resolved_via,    # "synth_name" | "saved_port" | "none"
        })

    new_settings = request.get_json() or {}
    existing = manifest.get("capture_settings") or {}
    merged = {
        "audio_device": new_settings.get("audio_device") or existing.get("audio_device", ""),
        "input_channels": new_settings.get("input_channels") or existing.get("input_channels", ""),
        "midi_port": new_settings.get("midi_port") or existing.get("midi_port", ""),
        "midi_channel": new_settings.get("midi_channel") or existing.get("midi_channel", 1),
    }
    manifest["capture_settings"] = merged
    if "polyphony" in new_settings and new_settings["polyphony"]:
        manifest["polyphony"] = new_settings["polyphony"]
    if "voices" in new_settings:
        manifest["voices"] = new_settings["voices"]
    # Allow setting synth_name from the UI too (the canonical name of the gear)
    if "synth_name" in new_settings and new_settings["synth_name"]:
        manifest["synth_name"] = new_settings["synth_name"]
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return jsonify({"ok": True, "saved": merged})


def _resolve_midi_port(synth_name: str, saved_port: str):
    """Given a synth's natural name + saved port, find the best matching active MIDI port.

    Returns (resolved_port_name, via) where `via` is one of:
        "synth_name" — direct match on the synth's natural name (best case, direct USB)
        "saved_port" — fell back to the manually-saved port (routing interface like 'mio')
        "none"       — neither matched; the user needs to pick a port
    """
    try:
        import rtmidi
        out = rtmidi.MidiOut()
        try:
            ports = [out.get_port_name(i) for i in range(out.get_port_count())]
        finally:
            try: out.close_port()
            except Exception: pass
            del out
    except Exception:
        try:
            import mido
            ports = mido.get_output_names()
        except Exception:
            ports = []

    # Try synth_name first (case-insensitive substring)
    if synth_name:
        sn = synth_name.lower()
        for p in ports:
            if sn in p.lower():
                return p, "synth_name"
    # Fall back to saved port
    if saved_port:
        sp = saved_port.lower()
        for p in ports:
            if sp in p.lower():
                return p, "saved_port"
    return "", "none"


@app.route("/api/instruments")
def list_instruments():
    if not INSTRUMENTS_DIR.exists():
        return jsonify([])
    instruments = []
    for instr_dir in sorted(INSTRUMENTS_DIR.iterdir()):
        if not instr_dir.is_dir():
            continue
        if not (instr_dir / "manifest.json").exists():
            continue
        instruments.append(compute_instrument_progress(instr_dir.name))
    return jsonify(instruments)


@app.route("/api/instruments/<slug>")
def get_instrument(slug):
    progress = compute_instrument_progress(slug)
    if not progress:
        abort(404)
    return jsonify(progress)


def _register_factory_capture(instr, patch, params):
    """Upsert a manifest.patches entry that LINKS a captured patch to its factory preset slot.

    When you capture a factory preset from the catalog, the UI passes that preset's slot id +
    position. We write/refresh a manifest.patches row keyed to it — so the catalog marks the
    factory slot CAPTURED (✓) and clicking it loads these multisamples, even when the patch was
    renamed "hook-…". The slot key (gearbase_preset_id / preset_position) is the link, NOT the
    folder name. No-op unless a slot id/position was provided (e.g. a free-form capture).
    """
    link = {k: params.get(k) for k in
            ("preset_name", "preset_position", "gearbase_preset_id",
             "program_change", "bank_msb", "bank_lsb", "role")
            if params.get(k) not in (None, "")}
    if not (link.get("gearbase_preset_id") or link.get("preset_position")):
        return
    try:
        mpath = INSTRUMENTS_DIR / instr / "manifest.json"
        if not mpath.exists():
            return
        man = json.loads(mpath.read_text())
        plist = man.setdefault("patches", [])
        entry = next((pp for pp in plist if pp.get("name") == patch), None)
        if entry is None:
            entry = {"name": patch}
            plist.append(entry)
        entry.update(link)
        entry.setdefault("source", "factory-capture")
        mpath.write_text(json.dumps(man, indent=2) + "\n")
    except Exception:
        pass   # never fail a good capture over a manifest bookkeeping write


@app.route("/api/instruments/<slug>/patches")
def list_instrument_patches(slug):
    """Return the manifest.patches list with capture status per patch.

    For each patch, checks if patches/<name>/<chain>/ exists and has WAV files.
    Status: 'captured' (has WAVs) | 'pending' (folder exists, empty) | 'todo' (no folder)
    Drives the "iconic patches to capture" checklist in the bench UI.
    """
    manifest_path = INSTRUMENTS_DIR / slug / "manifest.json"
    if not manifest_path.exists():
        abort(404)
    manifest = json.loads(manifest_path.read_text())
    patches = manifest.get("patches", [])
    chains_raw = manifest.get("chains_captured", ["raw"])
    # chains_captured can be a list of strings (legacy) or list of {name, signal_chain, ...} objects (current).
    # Normalize to a list of chain-name strings for the path-iteration logic below.
    chains = [c["name"] if isinstance(c, dict) else c for c in chains_raw]
    out = []
    for p in patches:
        # Walk all chains for this patch, summarize capture state
        chains_with_wavs = []
        chains_empty = []
        wav_total = 0
        captured_at = None       # newest WAV mtime — so the UI can flag stale/old takes
        for chain in chains:
            chain_dir = INSTRUMENTS_DIR / slug / "patches" / p["name"] / chain
            if chain_dir.exists() and chain_dir.is_dir():
                wavs = list(chain_dir.glob("*.wav"))
                if wavs:
                    chains_with_wavs.append(chain)
                    wav_total += len(wavs)
                    newest = max(w.stat().st_mtime for w in wavs)
                    captured_at = newest if captured_at is None else max(captured_at, newest)
                else:
                    chains_empty.append(chain)
        if chains_with_wavs:
            status = "captured"
        elif chains_empty:
            status = "pending"
        else:
            status = "todo"
        out.append({
            "name": p["name"],
            # Real Roland factory name + bank slot (e.g. "Euro SAW", "A57"), sourced from
            # gearbase. Shown at capture time so the bench label matches the synth screen =
            # visual proof the right preset is loaded before recording.
            "preset_name": p.get("preset_name"),
            "preset_position": p.get("preset_position"),
            "role": p.get("role"),
            "notes": p.get("notes", ""),
            "captured_at": captured_at,   # epoch secs of newest WAV; null if not captured
            "samples_dir": p.get("samples_dir"),
            "status": status,
            "wav_count": wav_total,
            "chains_with_wavs": chains_with_wavs,
            # PC + Bank Select for one-click load on hardware (if hand-linked or auto-derived)
            "program_change": p.get("program_change"),
            "bank_msb": p.get("bank_msb"),
            "bank_lsb": p.get("bank_lsb"),
            "gearbase_preset_id": p.get("gearbase_preset_id"),
            "source": p.get("source"),   # 'factory-capture' = a preset you grabbed vs a curated iconic
        })
    summary = {
        "total": len(patches),
        "captured": sum(1 for p in out if p["status"] == "captured"),
        "pending": sum(1 for p in out if p["status"] == "pending"),
        "todo": sum(1 for p in out if p["status"] == "todo"),
    }
    return jsonify({"patches": out, "summary": summary})


@app.route("/api/instruments/<slug>/patches/<patch>/<chain>/wavs")
def list_patch_wavs(slug, patch, chain):
    """Return list of captured WAV files for a patch/chain, sorted by note pitch.
    Used by the patch detail card's waveform strip — frontend fetches + renders each."""
    chain_dir = INSTRUMENTS_DIR / slug / "patches" / patch / chain
    if not chain_dir.exists():
        return jsonify({"wavs": []})
    # manual loop points set in the loop editor (override auto-detection at build time)
    manual = {}
    mlf = chain_dir / "manual_loops.json"
    if mlf.exists():
        try:
            manual = json.loads(mlf.read_text())
        except Exception:
            manual = {}
    wavs = sorted(chain_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime)
    out = []
    for w in wavs:
        rel = w.relative_to(BANK_ROOT)
        # Parse note name from filename: jp8000_supersaw-1_a4_v100_rr1.wav → "a4"
        # Pattern: <slug>_<patch>_<note>_v<vel>_rr<rr>.wav
        m = re.match(r".+_([a-g]s?#?\d+)_v(\d+)_rr(\d+)\.wav$", w.name.lower())
        note = m.group(1) if m else ""
        out.append({
            "name": w.name,
            "url": f"/audio/{rel}",
            "size_kb": round(w.stat().st_size / 1024, 1),
            "note": note,
            "loop": manual.get(w.name),     # {start,end,crossfade} if user locked one, else None
        })
    return jsonify({"wavs": out, "count": len(out)})


@app.route("/api/patch/loop-points", methods=["POST"])
def save_loop_points():
    """Save a loop the user dialed in the bench loop editor. Writes to manual_loops.json in
    the source patch/chain dir; the pack build reads it and bakes EXACTLY these points
    (overriding auto-detection). This is the ear-in-the-loop path — Nick sets it, we bake it.
    Pass crossfade<0 or loop:null semantics via clear=true to remove an override."""
    p = request.get_json() or {}
    instrument = (p.get("instrument") or "").strip()
    patch = (p.get("patch") or "").strip()
    chain = (p.get("chain") or "raw").strip()
    filename = (p.get("filename") or "").strip()
    for v in (instrument, patch, chain, filename):
        if not v or "/" in v or "\\" in v or ".." in v:
            return jsonify({"error": "bad/empty name"}), 400
    chain_dir = (INSTRUMENTS_DIR / instrument / "patches" / patch / chain).resolve()
    if not str(chain_dir).startswith(str(INSTRUMENTS_DIR.resolve())):
        return jsonify({"error": "path outside instruments"}), 400
    if not (chain_dir / filename).exists():
        return jsonify({"error": f"no such wav: {filename}"}), 404
    mlf = chain_dir / "manual_loops.json"
    manual = {}
    if mlf.exists():
        try:
            manual = json.loads(mlf.read_text())
        except Exception:
            manual = {}
    if p.get("clear"):
        manual.pop(filename, None)
    else:
        try:
            start, end = int(p["start"]), int(p["end"])
        except (KeyError, ValueError, TypeError):
            return jsonify({"error": "start/end required (ints)"}), 400
        if end <= start:
            return jsonify({"error": "end must be > start"}), 400
        manual[filename] = {"start": start, "end": end, "crossfade": int(p.get("crossfade", 0))}
    mlf.write_text(json.dumps(manual, indent=2))
    return jsonify({"ok": True, "filename": filename, "loop": manual.get(filename),
                    "total_set": len(manual)})


@app.route("/api/instruments/<slug>/factory-presets")
def list_factory_presets(slug):
    """Return the full official preset list for this gear from data/gearbase/presets/<slug>.json.

    Used by the patch browser's "📋 All factory presets" collapsible — every preset
    is clickable (fires PC + Bank Select via /api/load-patch).
    """
    presets_path = BANK_ROOT / "data" / "gearbase" / "presets" / f"{slug}.json"
    if not presets_path.exists():
        return jsonify({"presets": [], "note": "no factory preset list scraped for this gear"})
    try:
        data = json.loads(presets_path.read_text())
        return jsonify({
            "gear_slug": data.get("gear_slug", slug),
            "gear_name": data.get("gear_name", ""),
            "source": data.get("source", ""),
            "source_url": data.get("source_url", ""),
            "preset_count": len(data.get("presets", [])),
            "midi_implementation": data.get("midi_implementation", {}),
            "presets": data.get("presets", []),
        })
    except Exception as e:
        return jsonify({"presets": [], "error": str(e)}), 500


@app.route("/api/load-patch", methods=["POST"])
def load_patch():
    """Fire MIDI Bank Select (CC0 MSB + CC32 LSB) + Program Change to load a patch on hardware.

    Body: {
      midi_port: "mio",            # required — destination port (substring match ok)
      channel: 1,                  # 1-indexed MIDI channel (default 1)
      program_change: 38,          # 0-127 — required
      bank_msb: 80,                # 0-127 — optional (only sent if present)
      bank_lsb: 0,                 # 0-127 — optional (only sent if present)
      patch_name: "Euro SAW"       # cosmetic — surfaced in response/logs
    }

    The synth's display jumps to the requested preset. No-op if the synth doesn't
    support program change (e.g. MS-20). Returns 404 if port can't be matched.
    """
    params = request.get_json() or {}
    midi_port_name = params.get("midi_port")
    channel = int(params.get("channel", 1)) - 1  # 0-indexed for mido
    pc = params.get("program_change")
    msb = params.get("bank_msb")
    lsb = params.get("bank_lsb")
    patch_name = params.get("patch_name", "")

    if pc is None:
        return jsonify({"error": "program_change is required (0-127)"}), 400
    if not midi_port_name:
        return jsonify({"error": "midi_port is required"}), 400

    try:
        pc = int(pc)
        if not 0 <= pc <= 127:
            return jsonify({"error": f"program_change out of range (0-127): {pc}"}), 400
    except (TypeError, ValueError):
        return jsonify({"error": f"program_change must be int, got {pc!r}"}), 400

    try:
        import mido
        available = mido.get_output_names()
        matches = [p for p in available if midi_port_name.lower() in p.lower()]
        if not matches:
            return jsonify({"error": f"no MIDI port matches '{midi_port_name}'",
                            "available": available}), 404
        port_real = matches[0]
        port = mido.open_output(port_real)
        try:
            sent = []
            # Bank Select: MSB (CC0) first, then LSB (CC32). Synths latch the bank on PC.
            if msb is not None:
                port.send(mido.Message("control_change", control=0,  value=int(msb), channel=channel))
                sent.append(f"CC0={msb}")
            if lsb is not None:
                port.send(mido.Message("control_change", control=32, value=int(lsb), channel=channel))
                sent.append(f"CC32={lsb}")
            port.send(mido.Message("program_change", program=pc, channel=channel))
            sent.append(f"PC={pc}")
        finally:
            try: port.close()
            except Exception: pass
        return jsonify({
            "ok": True,
            "patch_name": patch_name,
            "midi_port": port_real,
            "channel": channel + 1,
            "messages_sent": sent,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================================
# MIDI INPUT LISTENER — the bench LISTENS to incoming MIDI from gear
# ----------------------------------------------------------------------------
# Foundation for: "bind iconic patch to whatever PC the synth fires", knob-twist
# capture, melodic gesture sampling, MIDI clock sync detection. Single in-port
# at a time; UI polls /api/midi-listen/poll to drain captured events.
# ============================================================================

def _midi_listen_callback(event, _data):
    """rtmidi callback — called on the rtmidi I/O thread. Push parsed events
    to the deque so the Flask poll endpoint can return them to the UI."""
    try:
        msg, _delta_t = event  # raw bytes + time-since-last
        if not msg:
            return
        status = msg[0]
        msg_type_nibble = status & 0xF0
        channel = (status & 0x0F) + 1  # 1-indexed for humans
        ts = _time_mod.time()
        if msg_type_nibble == 0xC0:  # Program Change
            midi_listen_events.append({"t": ts, "type": "program_change", "value": msg[1], "channel": channel})
        elif msg_type_nibble == 0xB0:  # Control Change
            midi_listen_events.append({"t": ts, "type": "control_change", "control": msg[1], "value": msg[2], "channel": channel})
        elif msg_type_nibble == 0x90 and len(msg) >= 3 and msg[2] > 0:  # Note On (vel>0)
            midi_listen_events.append({"t": ts, "type": "note_on", "note": msg[1], "velocity": msg[2], "channel": channel})
        elif msg_type_nibble == 0x80 or (msg_type_nibble == 0x90 and len(msg) >= 3 and msg[2] == 0):  # Note Off
            midi_listen_events.append({"t": ts, "type": "note_off", "note": msg[1], "channel": channel})
        elif status == 0xF0:  # SysEx — mode toggles + bank changes on Roland gear typically come through this
            # Capture full byte sequence as hex string for documentation
            hex_str = " ".join(f"{b:02X}" for b in msg)
            midi_listen_events.append({"t": ts, "type": "sysex", "bytes": list(msg), "hex": hex_str, "length": len(msg)})
        # Ignore clock + active sense — too noisy
    except Exception as e:
        print(f"midi-listen callback error: {e}", file=sys.stderr)


@app.route("/api/midi-listen/start", methods=["POST"])
def midi_listen_start():
    """Open a MIDI input port + start listening for events.

    Body: { midi_port: "mio" }  (substring match against available inputs)
    Returns: { ok, port_name, listening: true }
    """
    global midi_listen_port, midi_listen_port_name
    params = request.get_json() or {}
    requested = params.get("midi_port") or ""
    if not requested:
        return jsonify({"error": "midi_port is required"}), 400

    with midi_listen_lock:
        # Close any previously-open input first
        if midi_listen_port is not None:
            try:
                midi_listen_port.close_port()
            except Exception:
                pass
            midi_listen_port = None
            midi_listen_port_name = None

        # Drain any stale events from a prior session
        midi_listen_events.clear()

        try:
            import rtmidi
            mi = rtmidi.MidiIn()
            n = mi.get_port_count()
            available = [mi.get_port_name(i) for i in range(n)]
            matches = [(i, name) for i, name in enumerate(available) if requested.lower() in name.lower()]
            if not matches:
                del mi
                return jsonify({"error": f"no MIDI input matches '{requested}'", "available": available}), 404
            port_idx, port_real = matches[0]
            mi.open_port(port_idx)
            # Don't filter ANY message type — we want PC, CC, notes, everything
            # NOTE: sysex=False so we capture mode toggles + bank changes that Roland sends as SysEx
            mi.ignore_types(sysex=False, timing=True, active_sense=True)
            mi.set_callback(_midi_listen_callback)
            midi_listen_port = mi
            midi_listen_port_name = port_real
            return jsonify({"ok": True, "port_name": port_real, "listening": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500


@app.route("/api/midi-listen/poll")
def midi_listen_poll():
    """Drain captured events since the last poll. Returns and CLEARS the buffer.

    Frontend should poll this every ~300ms while in "listen" mode.
    """
    with midi_listen_lock:
        events = list(midi_listen_events)
        midi_listen_events.clear()
    return jsonify({
        "listening": midi_listen_port is not None,
        "port_name": midi_listen_port_name,
        "events": events,
    })


@app.route("/api/midi-listen/stop", methods=["POST"])
def midi_listen_stop():
    """Close the active MIDI input port."""
    global midi_listen_port, midi_listen_port_name
    with midi_listen_lock:
        if midi_listen_port is not None:
            try:
                midi_listen_port.close_port()
            except Exception:
                pass
            midi_listen_port = None
            midi_listen_port_name = None
        midi_listen_events.clear()
    return jsonify({"ok": True, "listening": False})


@app.route("/api/instruments/<slug>/patches/<patch_name>/bind-pc", methods=["POST"])
def bind_patch_pc(slug, patch_name):
    """Patch an iconic patch's program_change in manifest.json.

    Used by the "bind from synth" flow: Nick scrolls his JP-8000 to his actual
    version of supersaw-1, bench catches the PC=X, this endpoint writes X to
    the manifest's patches[name].program_change so future clicks fire correctly.

    Body: { program_change: 38, gearbase_preset_id?: "A57", bank_msb?, bank_lsb? }
    """
    manifest_path = INSTRUMENTS_DIR / slug / "manifest.json"
    if not manifest_path.exists():
        return jsonify({"error": f"instrument '{slug}' not found"}), 404
    params = request.get_json() or {}
    pc = params.get("program_change")
    if pc is None:
        return jsonify({"error": "program_change is required"}), 400
    try:
        pc = int(pc)
    except (TypeError, ValueError):
        return jsonify({"error": "program_change must be int"}), 400

    manifest = json.loads(manifest_path.read_text())
    patches = manifest.get("patches", [])
    target = next((p for p in patches if p.get("name") == patch_name), None)
    if not target:
        return jsonify({"error": f"patch '{patch_name}' not in manifest"}), 404
    target["program_change"] = pc
    if "gearbase_preset_id" in params: target["gearbase_preset_id"] = params["gearbase_preset_id"]
    if "bank_msb" in params and params["bank_msb"] is not None: target["bank_msb"] = int(params["bank_msb"])
    if "bank_lsb" in params and params["bank_lsb"] is not None: target["bank_lsb"] = int(params["bank_lsb"])
    target["pc_documented_at"] = _time_mod.strftime("%Y-%m-%d", _time_mod.localtime())
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return jsonify({"ok": True, "patch": target})


@app.route("/api/compose", methods=["POST"])
def compose_session():
    """Synthesis machine v0: pick matched progression + bass + drums + patch from the library.

    Body: { genre, bpm, key, mode, seed?, write_midi? }
    Returns: { progression, bass, drums, patch, midi_url? }
    """
    if _composer is None:
        return jsonify({"error": "composer module unavailable"}), 500
    import random
    params = request.get_json() or {}
    genre = params.get("genre", "house")
    bpm = float(params.get("bpm", 120))
    key = params.get("key") or None
    mode = params.get("mode") or None
    seed = params.get("seed")
    bpm_min = bpm - 10
    bpm_max = bpm + 10

    try:
        repo = _composer.find_repo_root()
        patterns = _composer.load_patterns(repo)
        instruments = _composer.load_instruments(repo)
        rng = random.Random(seed)
        prog = _composer.pick(patterns["progressions"], genre, bpm_min, bpm_max, key, mode, rng)
        bass = _composer.pick(patterns["bass"], genre, bpm_min, bpm_max, key, mode, rng)
        drums = _composer.pick(patterns["drums"], genre, bpm_min, bpm_max, None, None, rng)
        patch = _composer.suggest_patch(instruments, genre, rng)

        # Helpful "compact" view of each pattern for the UI
        def compact(p):
            if not p:
                return None
            return {
                "name": p.get("name"),
                "bpm": p.get("bpm"),
                "key": p.get("key") or p.get("key_root"),
                "key_mode": p.get("key_mode"),
                "vibe": p.get("vibe"),
                "tags": p.get("tags", []),
                "midi_path": p.get("_midi"),
            }

        result = {
            "genre": genre,
            "bpm_target": bpm,
            "bpm_range": [bpm_min, bpm_max],
            "key": key,
            "mode": mode,
            "progression": compact(prog),
            "bass": compact(bass),
            "drums": compact(drums),
            "patch": patch,
        }

        # If write_midi, build a combined 3-track .mid in releases/compositions/
        if params.get("write_midi"):
            import time as _time
            stamp = int(_time.time())
            out_dir = BANK_ROOT / "releases" / "compositions"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"compose-{genre}-{int(bpm)}bpm-{stamp}.mid"
            ok = _composer.build_combined_midi(prog, bass, drums, out_path)
            if ok:
                # serve via /audio route (we already have a generic file server)
                result["midi_url"] = f"/audio/{out_path.relative_to(BANK_ROOT)}"
                result["midi_path"] = str(out_path)

        return jsonify(result)
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/api/compose/genres")
def compose_genres():
    """Return all unique genres present in the pattern library (for the dropdown)."""
    if _composer is None:
        return jsonify([])
    repo = _composer.find_repo_root()
    patterns = _composer.load_patterns(repo)
    genres = set()
    for cat in ("progressions", "bass", "drums", "melodies", "arps"):
        for p in patterns.get(cat, []):
            for t in p.get("tags", []):
                genres.add(t.lower())
            g = (p.get("genre") or "").lower()
            if g:
                genres.add(g)
    # Curate to known musical genres + filter junk
    known = {
        "house", "hip-hop", "neo-soul", "rnb", "dub", "soul", "jazz", "trap",
        "lofi", "disco", "dnb", "trance", "techno", "minimal-techno", "acid",
        "afrobeat", "reggae", "dancehall", "ambient", "breakbeat", "uk-garage",
        "synthpop", "funk", "boom-bap", "drill",
    }
    return jsonify(sorted(genres & known))


@app.route("/api/capture", methods=["POST"])
def capture():
    """Trigger a capture. Body params depend on style:
        { style: "progression",
          instrument: "grandmother",
          name: "test",
          progression: "Cm Ab Eb Bb",
          bpm: 120,
          bars_per_chord: 2,
          midi_port: "Moog Grandmother",
          audio_device: "TX-6",
          input_channels: "11,12" }

        { style: "multisample",
          instrument: "grandmother",
          patch: "moogbass",
          chain: "raw",
          note_range: "a1-a4",
          step: 3,
          velocities: "100",
          sustain_sec: 4,
          tail_sec: 2.5,
          midi_port, audio_device, input_channels }

        { style: "hihat",
          instrument: "grandmother",
          name: "trap-140",
          target_bpm: 140,
          midi_port, audio_device, input_channels }
    """
    if not capture_lock.acquire(blocking=False):
        return jsonify({"error": "capture already in progress"}), 409
    try:
        params = request.get_json() or {}
        style = params.get("style")
        if style not in ("progression", "multisample", "hihat", "sweep", "drums"):
            return jsonify({"error": f"unknown style: {style}"}), 400

        py = str(VENV_PY) if VENV_PY.exists() else sys.executable

        cmd = [py]
        if style == "progression":
            cmd += [
                str(TOOLS_DIR / "capture" / "record-progression.py"),
                "--instrument", params.get("instrument", "grandmother"),
                "--name", params.get("name", "untitled"),
                "--progression", params.get("progression", "Cm Ab Eb Bb"),
                "--bpm", str(params.get("bpm", 120)),
                "--bars-per-chord", str(params.get("bars_per_chord", 2)),
                "--midi-port", params.get("midi_port", "Moog Grandmother"),
                "--midi-channel", str(params.get("midi_channel", 1)),
                "--audio-device", params.get("audio_device", "TX-6"),
                "--input-channels", params.get("input_channels", "11,12"),
                "--sample-rate", "48000",
                "--bit-depth", "24",
                "--mode", params.get("send_mode", "chord"),
                "--arp-rate", str(params.get("arp_rate", 16)),  # 16 = 1/16 notes — matches audition
                "--send-clock",
                "-y",
            ]
        elif style == "multisample":
            cmd += [
                str(TOOLS_DIR / "capture" / "capture-synth.py"),
                "--instrument", params.get("instrument", "grandmother"),
                "--patch", params.get("patch", "untitled"),
                "--chain", params.get("chain", "raw"),
                "--midi-port", params.get("midi_port", "Moog Grandmother"),
                "--midi-channel", str(params.get("midi_channel", 1)),
                "--audio-device", params.get("audio_device", "TX-6"),
                "--input-channels", params.get("input_channels", "11,12"),
                "--note-range", params.get("note_range", "a1-a4"),
                "--step", str(params.get("step", 3)),
                "--velocities", str(params.get("velocities", "100")),
                "--round-robins", str(params.get("round_robins", 1)),
                "--sustain-sec", str(params.get("sustain_sec", 4)),
                "--tail-sec", str(params.get("tail_sec", 2.5)),
                "--sample-rate", "48000",
                "--bit-depth", "24",
                "-y",
            ]
            if params.get("take"):           # continuous take + slice (one unbroken WAV)
                cmd.append("--take")
        elif style == "hihat":
            cmd += [
                str(TOOLS_DIR / "capture" / "record-hihat-suite.py"),
                "--instrument", params.get("instrument", "grandmother"),
                "--name", params.get("name", "untitled"),
                "--target-bpm", str(params.get("target_bpm", 140)),
                "--midi-port", params.get("midi_port", "Moog Grandmother"),
                "--audio-device", params.get("audio_device", "TX-6"),
                "--input-channels", params.get("input_channels", "11,12"),
                "--sample-rate", "48000",
                "--bit-depth", "24",
                "-y",
            ]
        elif style == "sweep":
            cmd += [
                str(TOOLS_DIR / "capture" / "record-performance.py"),
                "--instrument", params.get("instrument", "grandmother"),
                "--name", params.get("name", "untitled"),
                "--kind", "sweep",
                "--duration", str(params.get("duration", 8)),
                "--audio-device", params.get("audio_device", "TX-6"),
                "--input-channels", params.get("input_channels", "11,12"),
                "--sample-rate", "48000",
                "--bit-depth", "24",
                "-y",
            ]
        elif style == "drums":
            cmd += [
                str(TOOLS_DIR / "capture" / "record-drums.py"),
                "--instrument", params.get("instrument", "tr-808"),
                "--pattern", params.get("pattern", "four-on-floor"),
                "--bars", str(params.get("bars", 4)),
                "--midi-port", params.get("midi_port", "TR-808"),
                "--midi-channel", str(params.get("midi_channel", 10)),
                "--audio-device", params.get("audio_device", "TX-6"),
                "--input-channels", params.get("input_channels", "11,12"),
                "--sample-rate", "48000",
                "--bit-depth", "24",
                "-y",
            ]
            if params.get("bpm"):
                cmd += ["--bpm", str(params["bpm"])]

        # run the capture (synchronously — captures are short)
        # Tracked via current_capture_proc so /api/capture-stop can kill it mid-run
        global current_capture_proc
        print(f"\n>> CAPTURE: {' '.join(cmd)}", flush=True)
        current_capture_proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            stdout, stderr = current_capture_proc.communicate(timeout=600)
            returncode = current_capture_proc.returncode
        except subprocess.TimeoutExpired:
            current_capture_proc.kill()
            stdout, stderr = current_capture_proc.communicate()
            returncode = -1
        finally:
            current_capture_proc = None

        # build a result-like shim for the rest of the code
        class _R: pass
        result = _R()
        result.returncode = returncode
        result.stdout = stdout or ""
        result.stderr = stderr or ""

        if result.returncode != 0:
            # returncode -15 means we killed it (SIGTERM) — that's a user stop, not an error
            if result.returncode in (-15, -9, -2):
                return jsonify({"ok": False, "stopped": True, "stderr": result.stderr[-500:], "stdout": result.stdout[-500:]}), 200
            return jsonify({
                "ok": False,
                "stderr": result.stderr[-2000:],
                "stdout": result.stdout[-2000:],
            }), 500

        # Progression/chord loops land in <instr>/loops/raw/ and the Splice-style filename can contain
        # spaces, which the generic "saved <fname>" parser below chokes on — so log the freshest loop
        # here explicitly so chord captures ALWAYS show in the captures list.
        if style == "progression":
            _instr = params.get("instrument", "grandmother")
            _loops = INSTRUMENTS_DIR / _instr / "loops" / "raw"
            _wavs = sorted([w for w in _loops.glob("*.wav") if not w.name.endswith("_raw.wav")],
                           key=lambda w: w.stat().st_mtime, reverse=True) if _loops.exists() else []
            if _wavs:
                _rel = _wavs[0].relative_to(BANK_ROOT)
                last_capture["path"] = str(_wavs[0]); last_capture["instrument"] = _instr; last_capture["params"] = params
                log_capture({
                    "wav_path": str(_rel), "wav_url": f"/audio/{_rel}", "instrument": _instr,
                    "style": style, "name": _wavs[0].stem,
                    "captured_at": datetime.now().isoformat(timespec="seconds"),
                    "status": "pending", "params": params,
                })
                return jsonify({"ok": True, "wav_path": str(_rel), "wav_url": f"/audio/{_rel}",
                                "loop": True, "stdout_tail": result.stdout[-800:]})

        # parse the saved path from stdout — record-* scripts print "✓ saved <fname>"
        saved_match = re.search(r"saved\s+([^\s]+\.wav)", result.stdout)
        if saved_match:
            # The script prints just the filename. We need to find it on disk.
            fname = saved_match.group(1)
            # Search the instrument folder for it
            instr = params.get("instrument", "grandmother")
            instr_dir = INSTRUMENTS_DIR / instr
            matches = list(instr_dir.rglob(fname))
            if matches:
                last_capture["path"] = str(matches[0])
                last_capture["instrument"] = instr
                last_capture["params"] = params
                rel = matches[0].relative_to(BANK_ROOT)
                # add to session log
                log_capture({
                    "wav_path": str(rel),
                    "wav_url": f"/audio/{rel}",
                    "instrument": instr,
                    "style": style,
                    "name": params.get("name") or params.get("patch") or "untitled",
                    "captured_at": datetime.now().isoformat(timespec="seconds"),
                    "status": "pending",  # pending / kept / discarded
                    "params": params,
                })
                return jsonify({
                    "ok": True,
                    "wav_path": str(rel),
                    "wav_url": f"/audio/{rel}",
                    "stdout_tail": result.stdout[-1000:],
                })

        # MULTISAMPLE captures (capture-synth.py) write N files to <instr>/patches/<patch>/<chain>/
        # rather than printing "saved <name>.wav". Parse the success line "✓ N captured"
        # and report the patch directory's worth of WAVs.
        # per-note prints "✓ N captured"; continuous-take mode slices and prints "✓ N notes"
        multi_match = re.search(r"✓\s+(\d+)\s+(?:captured|notes)", result.stdout)
        if style == "multisample" and multi_match:
            instr = params.get("instrument", "grandmother")
            patch = params.get("patch", "untitled")
            chain = params.get("chain", "raw")
            patch_dir = INSTRUMENTS_DIR / instr / "patches" / patch / chain
            wavs = sorted(patch_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
            if wavs:
                # If this came from a factory preset, link it to that slot so the catalog marks
                # the slot ✓ + click-to-load finds these multisamples (even when renamed "hook-…").
                _register_factory_capture(instr, patch, params)
                # Use the most-recent WAV as the "preview" (middle-range note ideally, but newest works)
                # so the UI's audio player has something to load. Mid-range pick if we can.
                preview = wavs[len(wavs)//2] if len(wavs) >= 3 else wavs[0]
                preview_rel = preview.relative_to(BANK_ROOT)
                last_capture["path"] = str(preview)
                last_capture["instrument"] = instr
                last_capture["params"] = params
                log_capture({
                    "wav_path": str(preview_rel),
                    "wav_url": f"/audio/{preview_rel}",
                    "instrument": instr,
                    "style": style,
                    "name": patch,
                    "captured_at": datetime.now().isoformat(timespec="seconds"),
                    "status": "pending",
                    "params": params,
                    "multisample_count": int(multi_match.group(1)),
                })
                return jsonify({
                    "ok": True,
                    "wav_path": str(preview_rel),
                    "wav_url": f"/audio/{preview_rel}",
                    "multisample_count": int(multi_match.group(1)),
                    "multisample_dir": str(patch_dir.relative_to(BANK_ROOT)),
                    "stdout_tail": result.stdout[-1000:],
                })

        return jsonify({
            "ok": True,
            "wav_path": None,
            "wav_url": None,
            "stdout_tail": result.stdout[-2000:],
            "note": "capture finished but could not locate output WAV",
        })

    finally:
        capture_lock.release()


@app.route("/api/collect-multi", methods=["POST"])
def collect_multi():
    """Multi-part collector — fire ONE progression across synths on different MIDI ports/channels,
    capture each synth's interface inputs at once, split into named, perfect-looped stems. Runs
    tools/capture/collect-multi.py (proven) under the capture lock.

    Body: { progression, bpm, bars_per_chord, key, audio_device, no_loop,
            parts: [{ name, port, channel ("1"-"16" or "all"), input_channels ("1,2") }] }
    """
    if not capture_lock.acquire(blocking=False):
        return jsonify({"error": "capture already in progress"}), 409
    try:
        params = request.get_json() or {}
        parts = params.get("parts") or []
        if not parts:
            return jsonify({"error": "no parts — add at least one synth to collect"}), 400
        out_dir = params.get("out") or str(Path.home() / "Desktop" / "collected")
        py = str(VENV_PY) if VENV_PY.exists() else sys.executable
        cmd = [py, str(TOOLS_DIR / "capture" / "collect-multi.py"),
               "--audio-device", params.get("audio_device", "TX-6"),
               "--progression", params.get("progression", "Cm Ab Eb Bb"),
               "--bpm", str(params.get("bpm", 120)),
               "--bars-per-chord", str(params.get("bars_per_chord", 2)),
               "--out", out_dir]
        if params.get("key"):
            cmd += ["--key", str(params["key"])]
        if params.get("no_loop"):
            cmd.append("--no-loop")
        for p in parts:
            cmd += ["--part", f"{p.get('name', 'part')}:{p.get('port', '')}:{p.get('channel', '1')}:{p.get('input_channels', '1,2')}"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except Exception as e:
            return jsonify({"ok": False, "error": f"collect process failed: {e}"}), 500
        out = result.stdout
        fm = re.search(r"→\s+(.+)$", out, re.M)
        folder = fm.group(1).strip() if fm else None
        stems = [{"name": m.group(1), "peak_db": float(m.group(2)),
                  "looped": "looped" in m.group(3), "silent": float(m.group(2)) < -45}
                 for m in re.finditer(r"^\s+(\S+)\s+\([^)]*\): peak\s+(-?[\d.]+)\s+dBFS(.*)$", out, re.M)]
        if result.returncode != 0 and not stems:
            return jsonify({"ok": False, "error": "collect failed — check synths / TX-6 routing",
                            "stdout_tail": out[-1500:], "stderr_tail": result.stderr[-800:]}), 500
        log_capture({"name": params.get("progression", "collect"), "style": "collect-multi",
                     "captured_at": datetime.now().isoformat(timespec="seconds"),
                     "status": "pending", "params": params,
                     "collect_folder": folder, "stem_count": len(stems)})
        return jsonify({"ok": True, "folder": folder, "stems": stems, "stdout_tail": out[-1500:]})
    finally:
        capture_lock.release()


@app.route("/api/multisample-multi", methods=["POST"])
def multisample_multi():
    """Multi-synth multisampler — walk a chromatic note range across synths on different MIDI
    ports/channels, record each synth's interface inputs at once, slice into a per-synth folder of
    note-named WAVs (drag each into Ableton Sampler → an instrument per synth). Runs
    tools/capture/multisample-multi.py (mirrors collect-multi) under the capture lock.

    Body: { note_range ("C2-C5"), step, sustain, tail, audio_device,
            parts: [{ name, port, channel ("1"-"16" or "all"), input_channels ("1,2") }] }
    """
    if not capture_lock.acquire(blocking=False):
        return jsonify({"error": "capture already in progress"}), 409
    try:
        params = request.get_json() or {}
        parts = params.get("parts") or []
        if not parts:
            return jsonify({"error": "no parts — add at least one synth to multisample"}), 400
        out_dir = params.get("out") or str(Path.home() / "Desktop" / "multisampled")
        py = str(VENV_PY) if VENV_PY.exists() else sys.executable
        cmd = [py, str(TOOLS_DIR / "capture" / "multisample-multi.py"),
               "--audio-device", params.get("audio_device", "TX-6"),
               "--note-range", params.get("note_range", "C2-C5"),
               "--step", str(params.get("step", 4)),
               "--out", out_dir]
        if params.get("sustain"):
            cmd += ["--sustain", str(params["sustain"])]
        if params.get("tail"):
            cmd += ["--tail", str(params["tail"])]
        for p in parts:
            cmd += ["--part", f"{p.get('name', 'synth')}:{p.get('port', '')}:{p.get('channel', '1')}:{p.get('input_channels', '1,2')}"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except Exception as e:
            return jsonify({"ok": False, "error": f"multisample process failed: {e}"}), 500
        out = result.stdout
        fm = re.search(r"→\s+(.+)$", out, re.M)
        folder = fm.group(1).strip() if fm else None
        synths = [{"name": m.group(1), "notes": int(m.group(2)), "peak_db": float(m.group(3)),
                   "silent": ("SILENT" in m.group(4)) or float(m.group(3)) < -45}
                  for m in re.finditer(r"^\s+(\S+)\s+\((\d+) notes[^)]*\): peak\s+(-?[\d.]+)\s+dBFS(.*)$", out, re.M)]
        if result.returncode != 0 and not synths:
            return jsonify({"ok": False, "error": "multisample failed — check synths / TX-6 routing",
                            "stdout_tail": out[-1500:], "stderr_tail": result.stderr[-800:]}), 500
        log_capture({"name": params.get("note_range", "C2-C5"), "style": "multisample-multi",
                     "captured_at": datetime.now().isoformat(timespec="seconds"),
                     "status": "pending", "params": params,
                     "collect_folder": folder, "synth_count": len(synths)})
        return jsonify({"ok": True, "folder": folder, "synths": synths, "stdout_tail": out[-1500:]})
    finally:
        capture_lock.release()


@app.route("/api/keep", methods=["POST"])
def keep():
    """Confirm the last capture as a keeper. Writes a sidecar JSON next to the WAV."""
    params = request.get_json() or {}
    wav_rel = params.get("wav_path") or last_capture.get("path")
    if not wav_rel:
        return jsonify({"error": "no WAV to keep"}), 400
    wav_path = Path(wav_rel) if Path(wav_rel).is_absolute() else BANK_ROOT / wav_rel
    if not wav_path.exists():
        return jsonify({"error": f"WAV not found: {wav_path}"}), 404

    # write a sidecar JSON next to the WAV
    sidecar = wav_path.with_suffix(".json")
    sidecar_data = {
        "wav": wav_path.name,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "kept": True,
        **(params.get("metadata") or {}),
    }
    if last_capture.get("params"):
        sidecar_data["capture_params"] = last_capture["params"]
    sidecar.write_text(json.dumps(sidecar_data, indent=2))
    log_update(str(wav_path.relative_to(BANK_ROOT)), status="kept", kept_at=datetime.now().isoformat(timespec="seconds"))
    return jsonify({"ok": True, "sidecar": str(sidecar.relative_to(BANK_ROOT))})


@app.route("/api/discard", methods=["POST"])
def discard():
    """Toss a capture: move the WAV + sidecar to the macOS Trash (recoverable) AND drop the row
    from the captures list entirely. Gone from the bench — fishable from Trash if you slip."""
    params = request.get_json() or {}
    wav_rel = params.get("wav_path") or last_capture.get("path")
    if not wav_rel:
        return jsonify({"error": "no WAV to discard"}), 400
    wav_path = Path(wav_rel) if Path(wav_rel).is_absolute() else BANK_ROOT / wav_rel
    try:
        rel = str(wav_path.relative_to(BANK_ROOT))
    except ValueError:
        rel = str(wav_path)
    trashed = _move_to_trash(wav_path)
    _move_to_trash(wav_path.with_suffix(".json"))   # sidecar too, if present
    log_remove(rel)                                  # row disappears — accurate count
    return jsonify({"ok": True, "trashed": trashed, "removed": rel})


@app.route("/api/patch/clear", methods=["POST"])
def clear_patch():
    """Wipe captured audio so you can re-record clean. Pass instrument+patch to clear ONE
    patch, or instrument alone to clear ALL patches for it (the 'start fresh' button).
    Strictly scoped to instruments/<instrument>/patches/ — refuses any path outside it.
    Leaves the empty folder structure + manifest so the patch is ready to capture into."""
    params = request.get_json() or {}
    instrument = (params.get("instrument") or "").strip()
    patch = (params.get("patch") or "").strip()
    if not instrument:
        return jsonify({"error": "instrument required"}), 400
    for v in (instrument, patch):
        if v and ("/" in v or "\\" in v or ".." in v):
            return jsonify({"error": "invalid name"}), 400
    patches_root = (BANK_ROOT / "instruments" / instrument / "patches").resolve()
    if not str(patches_root).startswith(str((BANK_ROOT / "instruments").resolve())):
        return jsonify({"error": "path outside instruments"}), 400
    target = (patches_root / patch).resolve() if patch else patches_root
    if not str(target).startswith(str(patches_root)):
        return jsonify({"error": "path outside patches"}), 400
    if not target.exists():
        return jsonify({"ok": True, "deleted": 0, "patch": patch or "ALL"})
    exts = (".wav", ".aif", ".aiff", ".flac", ".json")
    deleted = 0
    for f in target.rglob("*"):
        if f.is_file() and f.suffix.lower() in exts:
            f.unlink()
            deleted += 1
    return jsonify({"ok": True, "deleted": deleted, "instrument": instrument, "patch": patch or "ALL"})


_bake_mod = None
def _get_bake_loops():
    """Lazy-load the bake-loops module (hyphenated filename) so the loop editor can show the
    SAME loop the pack build would auto-pick — what you see is what ships."""
    global _bake_mod
    if _bake_mod is None:
        import importlib.util
        p = TOOLS_DIR / "pack" / "bake-loops.py"
        spec = importlib.util.spec_from_file_location("bake_loops", p)
        _bake_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_bake_mod)
    return _bake_mod


@app.route("/api/patch/auto-loop")
def auto_loop():
    """The loop the build would auto-detect for one note. The editor shows this when you open
    a note with no manual loop set — so an untouched note already displays the real shipping
    loop (no need to lock unless you change it)."""
    instrument = (request.args.get("instrument") or "").strip()
    patch = (request.args.get("patch") or "").strip()
    chain = (request.args.get("chain") or "raw").strip()
    filename = (request.args.get("filename") or "").strip()
    for v in (instrument, patch, chain, filename):
        if not v or "/" in v or "\\" in v or ".." in v:
            return jsonify({"error": "bad name"}), 400
    wav = (INSTRUMENTS_DIR / instrument / "patches" / patch / chain / filename).resolve()
    if not str(wav).startswith(str(INSTRUMENTS_DIR.resolve())) or not wav.exists():
        return jsonify({"error": "no such wav"}), 404
    try:
        import soundfile as sf
        bake = _get_bake_loops()
        audio, sr = sf.read(str(wav))
        lp = bake.find_loop(audio, sr)
        if not lp:
            return jsonify({"loop": None})
        s, e, xf, route = lp
        return jsonify({"loop": {"start": int(s), "end": int(e), "crossfade": int(xf), "route": route}})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


_dreamer = None
def _get_dreamer():
    global _dreamer
    if _dreamer is None:
        import importlib.util
        p = TOOLS_DIR / "dashboard" / "dreamer.py"
        spec = importlib.util.spec_from_file_location("dreamer", p)
        _dreamer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_dreamer)
    return _dreamer


@app.route("/api/dreamer/start", methods=["POST"])
def dreamer_start():
    """Start the generative band. body: {vibe, key, tempo?, roles:{chords|bass|lead|drums:{port,channel,enabled}}}"""
    try:
        return jsonify(_get_dreamer().start(request.get_json() or {}))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/dreamer/stop", methods=["POST"])
def dreamer_stop():
    try:
        _get_dreamer().stop()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/dreamer/status")
def dreamer_status():
    try:
        return jsonify(_get_dreamer().status())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/dreamer/schedule", methods=["GET", "POST"])
def dreamer_schedule():
    """Time-of-day auto-play: GET the day-arc, POST to set blocks / enable. Default OFF."""
    try:
        d = _get_dreamer()
        if request.method == "POST":
            return jsonify(d.set_schedule(request.get_json() or {}))
        return jsonify(d.get_schedule())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/dreamer/test_notes", methods=["POST"])
def dreamer_test_notes():
    """Fire C2..C6 chromatically through a bank's port for octave calibration.
    body: {port: str, channel: int (1-16)}"""
    try:
        return jsonify(_get_dreamer().test_notes(request.get_json() or {}))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/patterns/progressions")
def list_progressions():
    """List saved chord progression patterns from the library."""
    pat_dir = BANK_ROOT / "patterns" / "progressions"
    if not pat_dir.exists():
        return jsonify([])
    patterns = []
    for f in sorted(pat_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            patterns.append(data)
        except Exception:
            pass
    return jsonify(patterns)


@app.route("/api/generate-progression", methods=["POST"])
def generate_progression():
    """Generate a fresh chord progression from genre + key (algorithmic, not from library)."""
    params = request.get_json() or {}
    genre = params.get("genre", "house")
    key = params.get("key", "Cm")
    seed = params.get("seed")
    py = str(VENV_PY) if VENV_PY.exists() else sys.executable
    script = TOOLS_DIR / "patterns" / "generate-progression.py"
    cmd = [py, str(script), "--genre", genre, "--key", key]
    if seed is not None:
        cmd += ["--seed", str(seed)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return jsonify({"ok": False, "error": result.stderr[-500:]}), 500
        data = json.loads(result.stdout)
        return jsonify({"ok": True, **data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/patterns/drums")
def list_drum_patterns():
    """List saved drum patterns from patterns/drums/."""
    pat_dir = BANK_ROOT / "patterns" / "drums"
    if not pat_dir.exists():
        return jsonify([])
    patterns = []
    for f in sorted(pat_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            patterns.append(data)
        except Exception:
            pass
    return jsonify(patterns)


@app.route("/api/capture-stop", methods=["POST"])
def capture_stop():
    """Kill the running capture subprocess + send MIDI panic to silence stuck notes.
    Also halts any running queue / audition so we don't just spawn the next capture."""
    global current_capture_proc
    # Stop all automation FIRST (set the flags before killing the proc) so the
    # queue worker sees the stop signal instead of marching on to the next patch.
    queue_stop_flag.set()
    audition_stop_flag.set()
    killed = False
    if current_capture_proc and current_capture_proc.poll() is None:
        try:
            current_capture_proc.terminate()  # SIGTERM
            try:
                current_capture_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                current_capture_proc.kill()
            killed = True
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    # Always send MIDI panic afterward — clears any stuck notes
    params = request.get_json() or {}
    midi_port_name = params.get("midi_port", "Moog Grandmother")
    try:
        import mido
        ports = mido.get_output_names()
        matches = [p for p in ports if midi_port_name.lower() in p.lower()]
        if matches:
            port = mido.open_output(matches[0])
            try:
                for ch in range(16):
                    port.send(mido.Message('control_change', control=123, value=0, channel=ch))
                    port.send(mido.Message('control_change', control=120, value=0, channel=ch))
            finally:
                port.close()
    except Exception:
        pass
    return jsonify({"ok": True, "killed_process": killed, "midi_panic_sent": True})


# ============================================================================
# CAPTURE QUEUE — "Bang it out" mode
# ----------------------------------------------------------------------------
# Hit one button, walk away, the bench auto-captures every iconic patch in
# sequence. For each patch: fire PC → wait 1s → run capture-synth.py → log.
# Resume-friendly (default skips already-captured patches).
# ============================================================================

queue_state = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "instrument": None,
    "total": 0,
    "current_idx": 0,        # 1-indexed for display ("3 of 12")
    "current_patch": None,   # name string of patch being processed
    "completed": [],         # list of {patch, wavs, elapsed_sec}
    "failed": [],            # list of {patch, error}
    "skipped": [],           # list of patches skipped because already captured
    "log": [],               # human-readable log lines for the UI to display
}
queue_thread: threading.Thread | None = None
queue_stop_flag = threading.Event()


def _queue_log(msg: str):
    """Append a log line + print to stderr. Keep last 200 lines."""
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    queue_state["log"].append(line)
    if len(queue_state["log"]) > 200:
        queue_state["log"] = queue_state["log"][-200:]
    print(f">> QUEUE: {line}", file=sys.stderr, flush=True)


@app.route("/api/capture-queue/start", methods=["POST"])
def capture_queue_start():
    """Start an auto-capture queue for an instrument's iconic patches.

    Body: {
      instrument: "jp8000",
      mode: "todo" | "all" | "selected",   # default "todo" — skips already-captured
      patches: ["supersaw-1", "mega-saw"],  # only used when mode="selected"
      chain: "raw",
      settings: { midi_port, audio_device, input_channels, note_range, step, ... }
    }
    """
    global queue_thread
    if queue_state["running"]:
        return jsonify({"error": "queue already running. Stop first."}), 409

    params = request.get_json() or {}
    slug = params.get("instrument") or ""
    mode = params.get("mode", "todo")
    chain = params.get("chain", "raw")
    settings = params.get("settings", {})
    selected = params.get("patches", [])

    manifest_path = INSTRUMENTS_DIR / slug / "manifest.json"
    if not manifest_path.exists():
        return jsonify({"error": f"instrument '{slug}' not found"}), 404
    manifest = json.loads(manifest_path.read_text())
    all_patches = manifest.get("patches", [])

    # Build the queue. For each patch, check existing captures (resume support).
    queue = []
    for p in all_patches:
        if mode == "selected" and p["name"] not in selected:
            continue
        # Check if this patch's chain has WAVs already. "todo" mode skips patches that
        # have any WAVs (treats both "captured" and "pending-with-some-wavs" as done).
        # Empty chain dirs count as todo — they'll be (re)captured.
        chain_dir = INSTRUMENTS_DIR / slug / "patches" / p["name"] / chain
        existing_wavs = list(chain_dir.glob("*.wav")) if chain_dir.exists() else []
        if mode == "todo" and existing_wavs:
            # Skip — already has captures
            queue_state["skipped"].append(p["name"])
            continue
        queue.append(p)

    if not queue:
        return jsonify({"ok": True, "message": "nothing to capture (all done in this mode)",
                        "skipped": queue_state["skipped"]})

    # Honor synth quirks — JP-8000 needs PATCH mode + Perform Ctrl OFF, no bank select etc.
    quirks = manifest.get("midi_quirks", {})

    # Reset state for new run
    queue_state["running"] = True
    queue_state["started_at"] = datetime.now().isoformat(timespec="seconds")
    queue_state["finished_at"] = None
    queue_state["instrument"] = slug
    queue_state["total"] = len(queue)
    queue_state["current_idx"] = 0
    queue_state["current_patch"] = None
    queue_state["completed"] = []
    queue_state["failed"] = []
    queue_state["log"] = []
    queue_stop_flag.clear()

    _queue_log(f"queue started — {slug} · mode={mode} · chain={chain} · {len(queue)} patches")
    if quirks.get("required_synth_setup"):
        for setup_line in quirks["required_synth_setup"]:
            _queue_log(f"  reminder: {setup_line}")

    def worker():
        import subprocess as _sp
        import mido as _mido
        import time as _time
        import sounddevice as _sd
        import numpy as _np
        global current_capture_proc

        # PRE-FLIGHT: fire a test note + measure peak. If silent, ABORT before captures.
        _queue_log("pre-flight: firing test note + measuring audio peak...")
        try:
            audio_dev_name = settings.get("audio_device", "TX-6")
            input_channels_str = settings.get("input_channels", "1,2")
            input_channels = [int(c.strip()) for c in input_channels_str.split(",")]
            # Resolve audio device index
            devs = _sd.query_devices()
            in_devs = [(i, d) for i, d in enumerate(devs) if d["max_input_channels"] > 0 and audio_dev_name.lower() in d["name"].lower()]
            if not in_devs:
                _queue_log(f"✗ pre-flight: audio device '{audio_dev_name}' not found — aborting queue")
                queue_state["running"] = False
                queue_state["finished_at"] = datetime.now().isoformat(timespec="seconds")
                return
            dev_idx = in_devs[0][0]
            # Find a test patch with a PC + fire it, then test note + record
            test_patch = queue[0]
            test_pc = test_patch.get("program_change")
            midi_port_name = settings.get("midi_port", "mio")
            ports = _mido.get_output_names()
            mp_matches = [p for p in ports if midi_port_name.lower() in p.lower()]
            if not mp_matches:
                _queue_log(f"✗ pre-flight: MIDI port '{midi_port_name}' not found — aborting")
                queue_state["running"] = False
                queue_state["finished_at"] = datetime.now().isoformat(timespec="seconds")
                return
            port = _mido.open_output(mp_matches[0])
            try:
                if test_pc is not None:
                    port.send(_mido.Message("program_change", program=int(test_pc), channel=0))
                    _time.sleep(0.8)
                # Start a 2-sec recording, fire note, sustain, stop
                rec = _sd.rec(int(48000 * 2.0), samplerate=48000,
                              channels=len(input_channels), device=dev_idx,
                              mapping=input_channels, dtype="float32", blocking=False)
                _time.sleep(0.15)
                port.send(_mido.Message("note_on", note=60, velocity=100, channel=0))
                _time.sleep(1.2)
                port.send(_mido.Message("note_off", note=60, velocity=0, channel=0))
                _sd.wait()
                peak = float(_np.abs(rec).max()) if rec is not None else 0.0
            finally:
                port.close()
            peak_db = 20.0 * _np.log10(peak) if peak > 0 else None
            _queue_log(f"pre-flight: peak = {peak*100:.2f}% ({'—' if peak_db is None else f'{peak_db:.1f} dBFS'})")
            if peak < 0.005:  # below ~-46 dBFS = effectively silent
                _queue_log(f"✗ AUDIO IS SILENT — aborting queue. Captures would all be empty.")
                _queue_log(f"   check: TX-6 input channels, synth volume knob, audio cable, monitor the '🎚 Audio level' meter on the multisample form")
                queue_state["failed"].append({
                    "patch": "pre-flight",
                    "error": f"audio peak {peak*100:.3f}% — signal not reaching the recorder. Captures would all be silent WAVs.",
                })
                queue_state["running"] = False
                queue_state["finished_at"] = datetime.now().isoformat(timespec="seconds")
                return
            _queue_log(f"✓ pre-flight passed — audio is alive, proceeding to capture {len(queue)} patches")
        except Exception as _e:
            _queue_log(f"⚠ pre-flight check errored ({_e}), continuing anyway (use Check Level button to verify manually)")

        for idx, patch in enumerate(queue, start=1):
            if queue_stop_flag.is_set():
                _queue_log(f"stopped by user at idx {idx}/{len(queue)}")
                break

            queue_state["current_idx"] = idx
            queue_state["current_patch"] = patch["name"]
            patch_start = _time.time()
            _queue_log(f"[{idx}/{len(queue)}] {patch['name']} — starting")

            # STEP 1: Fire Program Change to load the patch on the synth (if linked)
            pc = patch.get("program_change")
            if pc is not None:
                midi_port_name = settings.get("midi_port") or manifest.get("capture_settings", {}).get("midi_port") or "mio"
                try:
                    ports = _mido.get_output_names()
                    matches = [p for p in ports if midi_port_name.lower() in p.lower()]
                    if matches:
                        port = _mido.open_output(matches[0])
                        try:
                            # Bank Select only if the synth's quirks allow it
                            if quirks.get("send_bank_select", False):
                                msb = patch.get("bank_msb")
                                lsb = patch.get("bank_lsb")
                                if msb is not None:
                                    port.send(_mido.Message("control_change", control=0, value=int(msb), channel=0))
                                if lsb is not None:
                                    port.send(_mido.Message("control_change", control=32, value=int(lsb), channel=0))
                            port.send(_mido.Message("program_change", program=int(pc), channel=0))
                            _queue_log(f"  → sent PC={pc} to {matches[0]}")
                        finally:
                            port.close()
                        _time.sleep(1.0)  # let the synth settle on the new patch
                except Exception as _e:
                    _queue_log(f"  ⚠ PC fire failed: {_e}  (continuing anyway)")
            else:
                _queue_log(f"  no PC linked — assuming patch is already dialed in manually")

            # STEP 2: Run the multisample capture (capture-synth.py)
            cmd = [
                str(TOOLS_DIR / "capture" / ".venv" / "bin" / "python"),
                str(TOOLS_DIR / "capture" / "capture-synth.py"),
                "--instrument", slug,
                "--patch", patch["name"],
                "--chain", chain,
                "--midi-port", settings.get("midi_port", "mio"),
                "--midi-channel", str(settings.get("midi_channel", 1)),
                "--audio-device", settings.get("audio_device", "TX-6"),
                "--input-channels", settings.get("input_channels", "1,2"),
                "--note-range", settings.get("note_range", "a2-a5"),
                "--step", str(settings.get("step", 3)),
                "--velocities", str(settings.get("velocities", "100")),
                "--round-robins", str(settings.get("round_robins", 1)),
                "--sustain-sec", str(settings.get("sustain_sec", 4)),
                "--tail-sec", str(settings.get("tail_sec", 2.5)),
                "--sample-rate", "48000",
                "--bit-depth", "24",
                "-y",
            ]
            try:
                current_capture_proc = _sp.Popen(cmd, stdout=_sp.PIPE, stderr=_sp.PIPE, text=True)
                stdout, stderr = current_capture_proc.communicate(timeout=600)
                rc = current_capture_proc.returncode
                current_capture_proc = None

                if rc != 0:
                    _queue_log(f"  ✗ {patch['name']} failed (exit {rc}): {stderr[-200:]}")
                    queue_state["failed"].append({
                        "patch": patch["name"],
                        "error": stderr[-500:] or f"exit {rc}",
                    })
                    continue

                # Count WAVs to confirm success
                chain_dir = INSTRUMENTS_DIR / slug / "patches" / patch["name"] / chain
                wavs = list(chain_dir.glob("*.wav")) if chain_dir.exists() else []
                elapsed = _time.time() - patch_start
                _queue_log(f"  ✓ {patch['name']} — {len(wavs)} WAVs · {elapsed:.1f}s")
                queue_state["completed"].append({
                    "patch": patch["name"],
                    "wavs": len(wavs),
                    "elapsed_sec": round(elapsed, 1),
                })
            except _sp.TimeoutExpired:
                if current_capture_proc:
                    current_capture_proc.kill()
                    current_capture_proc = None
                _queue_log(f"  ✗ {patch['name']} timed out (>10 min)")
                queue_state["failed"].append({"patch": patch["name"], "error": "timeout"})
            except Exception as _e:
                _queue_log(f"  ✗ {patch['name']} exception: {_e}")
                queue_state["failed"].append({"patch": patch["name"], "error": str(_e)})

        # Done
        queue_state["running"] = False
        queue_state["current_patch"] = None
        queue_state["finished_at"] = datetime.now().isoformat(timespec="seconds")
        total_elapsed = sum(c.get("elapsed_sec", 0) for c in queue_state["completed"])
        total_wavs = sum(c.get("wavs", 0) for c in queue_state["completed"])
        _queue_log(f"DONE — {len(queue_state['completed'])} captured, {len(queue_state['failed'])} failed, {len(queue_state['skipped'])} skipped · {total_wavs} WAVs · {total_elapsed:.0f}s total")

    queue_thread = threading.Thread(target=worker, daemon=True)
    queue_thread.start()
    return jsonify({
        "ok": True,
        "queued": [p["name"] for p in queue],
        "skipped": queue_state["skipped"],
        "total": len(queue),
    })


@app.route("/api/capture-queue/status")
def capture_queue_status():
    """Snapshot of queue state — UI polls this every ~1s for live progress."""
    return jsonify(dict(queue_state))


@app.route("/api/capture-queue/stop", methods=["POST"])
def capture_queue_stop():
    """Abort the queue gracefully — finishes current capture-synth.py, then exits."""
    global current_capture_proc
    queue_stop_flag.set()
    # Also kill the running capture-synth.py so we don't have to wait for it to finish
    if current_capture_proc and current_capture_proc.poll() is None:
        try:
            current_capture_proc.terminate()
            current_capture_proc.wait(timeout=2)
        except Exception:
            try: current_capture_proc.kill()
            except Exception: pass
    return jsonify({"ok": True, "stopping": True})


_note_ports = {}  # real_port_name -> open mido output, cached so keypress latency stays low

@app.route("/api/note", methods=["POST"])
def play_note():
    """Live computer-keyboard player. Body: { midi_port, channel=1, notes:[60,64,67], on, velocity=100 }.
    Sends note_on (on=true) / note_off (on=false) for each note. The output port is cached OPEN
    across calls so playing from the laptop keys feels responsive; /api/midi/panic clears stuck notes."""
    import mido
    p = request.get_json() or {}
    port_name = p.get("midi_port")
    if not port_name:
        return jsonify({"error": "midi_port required"}), 400
    notes = p.get("notes")
    if notes is None and "note" in p:
        notes = [p["note"]]
    if not notes:
        return jsonify({"error": "notes required"}), 400
    ch = int(p.get("channel", 1)) - 1
    on = bool(p.get("on", True))
    vel = int(p.get("velocity", 100))
    real = None
    try:
        matches = [x for x in mido.get_output_names() if port_name.lower() in x.lower()]
        if not matches:
            return jsonify({"error": f"no MIDI port matches '{port_name}'"}), 404
        real = matches[0]
        port = _note_ports.get(real)
        if port is None:
            port = mido.open_output(real)
            _note_ports[real] = port
        kind = "note_on" if on else "note_off"
        for n in notes:
            n = int(n)
            if 0 <= n <= 127:
                port.send(mido.Message(kind, note=n, velocity=(vel if on else 0), channel=ch))
        return jsonify({"ok": True, "port": real, "notes": notes, "on": on})
    except Exception as e:
        if real and real in _note_ports:          # drop a stale handle so the next call re-opens
            try: _note_ports.pop(real).close()
            except Exception: pass
        return jsonify({"error": str(e)}), 500


@app.route("/api/midi/panic", methods=["POST"])
def midi_panic():
    """PANIC — the big red button. Halts EVERYTHING: capture queue, audition
    loop, and the in-flight capture subprocess, then sends All Notes Off + All
    Sound Off on all 16 channels. Hitting this during a whole-pack run stops it."""
    global current_capture_proc
    # 1) Signal every automation loop to stop
    queue_stop_flag.set()
    audition_stop_flag.set()
    # 2) Kill the in-flight capture subprocess so the queue worker returns now
    #    (otherwise it stays blocked on communicate() until the capture finishes)
    if current_capture_proc and current_capture_proc.poll() is None:
        try:
            current_capture_proc.terminate()
            try:
                current_capture_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                current_capture_proc.kill()
        except Exception:
            pass
        current_capture_proc = None
    params = request.get_json() or {}
    midi_port_name = params.get("midi_port", "Moog Grandmother")
    try:
        import mido
        ports = mido.get_output_names()
        matches = [p for p in ports if midi_port_name.lower() in p.lower()]
        if not matches:
            return jsonify({"ok": False, "error": f"no MIDI port matches '{midi_port_name}'", "available": ports}), 404
        port = mido.open_output(matches[0])
        try:
            for ch in range(16):
                port.send(mido.Message('control_change', control=123, value=0, channel=ch))
                port.send(mido.Message('control_change', control=120, value=0, channel=ch))
        finally:
            port.close()
        return jsonify({"ok": True, "port": matches[0]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/build-pack", methods=["POST"])
def build_pack():
    """Run gmpack for the given instrument."""
    params = request.get_json() or {}
    instrument = params.get("instrument", "grandmother")
    vol = params.get("vol", 1)
    py = str(VENV_PY) if VENV_PY.exists() else sys.executable
    script = TOOLS_DIR / "pack" / "build-pack.py"
    if not script.exists():
        return jsonify({"ok": False, "error": "build-pack.py not found"}), 500
    try:
        result = subprocess.run(
            [py, str(script), "--instrument", instrument, "--vol", str(vol)],
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode != 0:
            return jsonify({"ok": False, "stderr": result.stderr[-2000:], "stdout": result.stdout[-2000:]}), 500
        return jsonify({"ok": True, "stdout_tail": result.stdout[-2000:]})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "build timed out"}), 500


@app.route("/api/captures/clear", methods=["POST"])
def clear_captures_log():
    """Wipe the session capture log. Does NOT delete WAVs on disk — those stay
    in the instruments/ tree (use the per-row delete or the manual file move
    if you want them gone for real). Just clears this panel."""
    global capture_log
    cleared_count = len(capture_log)
    capture_log = []
    _save_log()
    return jsonify({"ok": True, "cleared": cleared_count})


@app.route("/api/captures")
def list_captures():
    """Return the session capture log (last 30, newest first)."""
    return jsonify(capture_log)


# --- persistent MIDI clock streamer (always-on tempo reference) ---
clock_stop_flag = threading.Event()
clock_reset_flag = threading.Event()  # set this to send Stop→Start (Ableton-style "lock to 1")
clock_thread: threading.Thread | None = None
clock_state: dict = {"running": False, "bpm": 120, "port": "", "ports": []}


def _clock_worker(port_names: list[str], bpm: float):
    """Persistent MIDI clock worker — broadcasts to MULTIPLE ports at once.

    Every active synth/drum machine gets the same 24-PPQN clock + Start/Stop, so
    the Moog's arp, the KO's sequencer, and any tempo-synced FX all lock to ONE
    master tempo + downbeat. This is what makes the whole studio play in sync.

    Reads bpm from clock_state['bpm'] live so the slider changes tempo without
    tearing down the thread.
    """
    import mido as _mido
    import time as _time
    available = _mido.get_output_names()
    # Resolve each requested name to a real port + open it
    opened = []   # list of (real_name, port)
    for want in port_names:
        match = next((p for p in available if want.lower() in p.lower()), None)
        if match and match not in [n for n, _ in opened]:
            try:
                opened.append((match, _mido.open_output(match)))
            except Exception as e:
                print(f"clock: couldn't open {match}: {e}", file=sys.stderr)
    if not opened:
        clock_state["running"] = False
        return
    clock_state["ports"] = [n for n, _ in opened]

    clock_msg = _mido.Message('clock')
    start_msg = _mido.Message('start')
    stop_msg = _mido.Message('stop')

    def broadcast(msg):
        for _name, p in opened:
            try: p.send(msg)
            except Exception: pass

    try:
        broadcast(start_msg)
        t_last = _time.time()
        while not clock_stop_flag.is_set():
            # Ableton-style transport reset: Stop → brief pause → Start on all ports
            if clock_reset_flag.is_set():
                broadcast(stop_msg)
                _time.sleep(0.03)
                broadcast(start_msg)
                t_last = _time.time()
                clock_reset_flag.clear()
                continue
            current_bpm = clock_state.get("bpm", bpm)
            interval = 60.0 / current_bpm / 24.0
            t_next = t_last + interval
            # Hybrid sleep+spin for sub-ms jitter (measured on macOS — see git history)
            while True:
                remaining = t_next - _time.time()
                if remaining <= 0:
                    break
                if clock_stop_flag.is_set() or clock_reset_flag.is_set():
                    break
                if remaining > 0.005:
                    _time.sleep(remaining - 0.003)
            if clock_stop_flag.is_set():
                break
            if clock_reset_flag.is_set():
                continue
            broadcast(clock_msg)
            t_last = t_next
    finally:
        try: broadcast(stop_msg)
        except Exception: pass
        for _name, p in opened:
            try: p.close()
            except Exception: pass
        clock_state["running"] = False
        clock_state["ports"] = []


@app.route("/api/clock/start", methods=["POST"])
def clock_start():
    """Start or restart the persistent MIDI clock thread.

    Accepts EITHER:
      - midi_port:  "Moog Grandmother"        (single — backward compat)
      - midi_ports: ["mio","Moog Grandmother","EP-133"]  (broadcast to all)

    Broadcasting to all active ports keeps the whole studio in sync.
    """
    global clock_thread
    params = request.get_json() or {}
    bpm = float(params.get("bpm", 120))
    # Build the port list — prefer explicit list, fall back to single
    port_names = params.get("midi_ports")
    if not port_names:
        single = params.get("midi_port", "Moog Grandmother")
        port_names = [single] if single else []
    # de-dupe preserving order
    seen = set(); port_names = [p for p in port_names if not (p in seen or seen.add(p))]

    port_key = ",".join(sorted(port_names))

    # No-op if nothing changed
    if (clock_state["running"]
            and clock_state["bpm"] == bpm
            and clock_state.get("port") == port_key):
        return jsonify({"ok": True, "already_running": True, **clock_state})

    # Tear down existing thread before starting a new one
    if clock_state["running"]:
        clock_stop_flag.set()
        if clock_thread and clock_thread.is_alive():
            clock_thread.join(timeout=0.5)

    clock_stop_flag.clear()
    clock_state["running"] = True
    clock_state["bpm"] = bpm
    clock_state["port"] = port_key
    clock_state["ports"] = port_names
    clock_thread = threading.Thread(target=_clock_worker, args=(port_names, bpm), daemon=True)
    clock_thread.start()
    return jsonify({"ok": True, **clock_state})


@app.route("/api/clock/stop", methods=["POST"])
def clock_stop():
    clock_stop_flag.set()
    clock_state["running"] = False
    return jsonify({"ok": True})


@app.route("/api/clock/bpm", methods=["POST"])
def clock_bpm():
    """Live-update the BPM without restarting the clock thread.

    Used by the dashboard's BPM slider/scrub — change BPM smoothly in real time
    without re-triggering MIDI Start (which would re-lock the arp to position 1).
    The worker reads clock_state['bpm'] on each tick.
    """
    params = request.get_json() or {}
    bpm = float(params.get("bpm", 120))
    if not clock_state["running"]:
        # Not running — just remember the value for next start
        clock_state["bpm"] = bpm
        return jsonify({"ok": True, "running": False, **clock_state})
    clock_state["bpm"] = bpm
    return jsonify({"ok": True, **clock_state})


@app.route("/api/clock/reset", methods=["POST"])
def clock_reset():
    """Send MIDI Stop→Start to snap downstream gear back to position 1.

    This is the Ableton 'stop/start the transport' behavior — synths with an
    internal arp/sequencer that follows MIDI clock will jump to the downbeat.
    Doesn't tear down the clock thread; just nudges it via clock_reset_flag.
    """
    if not clock_state["running"]:
        return jsonify({"ok": False, "error": "clock not running — start it first"}), 400
    clock_reset_flag.set()
    return jsonify({"ok": True, "reset": True, **clock_state})


@app.route("/api/clock/status")
def clock_status():
    return jsonify(clock_state)


# --- system audio output switcher ---

def _find_audio_switcher() -> str:
    """Find the audio switcher binary. Prefers our own bundled Swift CLI
    (`tools/dashboard/audio-switcher`), falls back to switchaudio-osx if installed
    via brew. Our Swift binary needs no install — ships with the repo."""
    import shutil
    bench_switcher = Path(__file__).resolve().parent / "audio-switcher"
    if bench_switcher.exists() and bench_switcher.is_file():
        return str(bench_switcher)
    return shutil.which("SwitchAudioSource") or shutil.which("switchaudio-osx") or ""


@app.route("/api/audio-output/list")
def audio_output_list():
    """List all macOS output devices + the current default.

    Uses our Swift audio-switcher binary for live + accurate state. Falls back
    to sounddevice if the binary's missing.
    """
    import subprocess
    devices = []
    current_name = None
    switcher = _find_audio_switcher()

    if switcher:
        # Use our Swift binary — gives us live + accurate names + current default
        try:
            r = subprocess.run([switcher, "list"], capture_output=True, text=True, timeout=3)
            if r.returncode == 0:
                for line in r.stdout.strip().split("\n"):
                    parts = line.split("|")
                    if len(parts) >= 3:
                        is_default = parts[2].strip() == "default"
                        devices.append({
                            "index": int(parts[0]),  # Core Audio device ID
                            "name": parts[1],
                            "channels": 2,  # not used by UI
                            "is_current": is_default,
                        })
                        if is_default:
                            current_name = parts[1]
        except Exception:
            pass

    # Fallback to sounddevice if our binary isn't present
    if not devices:
        import sounddevice as sd
        try:
            sd._terminate(); sd._initialize()
        except Exception:
            pass
        try:
            current_idx = sd.default.device[1]
        except Exception:
            current_idx = None
        for i, d in enumerate(sd.query_devices()):
            if d["max_output_channels"] > 0:
                devices.append({
                    "index": i,
                    "name": d["name"],
                    "channels": d["max_output_channels"],
                    "is_current": (i == current_idx),
                })

    return jsonify({
        "devices": devices,
        "current_name": current_name,
        "switcher_available": bool(switcher),
        "switcher_path": switcher or None,
        "install_hint": None if switcher else "compile audio-switcher.swift (see tools/dashboard/) or brew install switchaudio-osx",
    })


@app.route("/api/audio-output/set", methods=["POST"])
def audio_output_set():
    """Switch macOS default output. Uses our Swift binary OR switchaudio-osx."""
    import subprocess
    switcher = _find_audio_switcher()
    if not switcher:
        return jsonify({
            "ok": False,
            "error": "no audio switcher available (Swift binary not compiled, switchaudio-osx not installed)",
        }), 400
    params = request.get_json() or {}
    name = params.get("name")
    if not name:
        return jsonify({"ok": False, "error": "missing device name"}), 400
    try:
        # Our Swift binary takes "set NAME"; switchaudio-osx takes "-s NAME"
        if "audio-switcher" in switcher and "switchaudio-osx" not in switcher:
            cmd = [switcher, "set", name]
        else:
            cmd = [switcher, "-s", name]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return jsonify({"ok": False, "error": r.stderr.strip() or "switch failed"}), 500
        return jsonify({"ok": True, "switched_to": name})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/audio-output/open-settings", methods=["POST"])
def audio_output_open_settings():
    """Fallback when switchaudio-osx isn't installed — open macOS Sound preferences
    directly to the Output tab so the user can switch manually with one click."""
    import subprocess
    try:
        # x-apple.systempreferences:com.apple.preference.sound?Output opens the
        # Sound preferences pane directly to the Output tab on modern macOS.
        subprocess.run(
            ["open", "x-apple.systempreferences:com.apple.preference.sound?Output"],
            capture_output=True, timeout=3
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# --- monitor (live audio passthrough TX-6 → system output) ---

def _monitor_worker(input_device_name: str, channels_str: str):
    """Stream audio from input_device into the system default output, low-latency.

    Auto-mutes whenever a capture is running (current_capture_proc is set) so the
    live monitor never bleeds back into the recording.
    """
    import sounddevice as sd
    import numpy as np
    try:
        # Resolve input device by name → index
        devices = sd.query_devices()
        in_idx = None
        for i, d in enumerate(devices):
            if input_device_name.lower() in d["name"].lower() and d["max_input_channels"] > 0:
                in_idx = i
                break
        if in_idx is None:
            monitor_state["last_error"] = f"input device not found: {input_device_name}"
            monitor_state["running"] = False
            return
        # Parse channels — e.g. "1,2" means stereo pair, "11,12" means TX-6 channels 11+12
        try:
            channels = [int(c.strip()) for c in channels_str.split(",")]
        except Exception:
            channels = [1, 2]
        n_input = len(channels)
        input_info = sd.query_devices(in_idx)
        sr = int(input_info["default_samplerate"]) or 48000

        # Output uses system default device (whatever the Mac is set to — W+, speakers, etc.)
        # We always send stereo out. If input is mono, duplicate to both channels.
        def callback(indata, outdata, frames, time, status):
            if status:
                # Don't crash on overruns; just log to state
                monitor_state["last_error"] = str(status)
            # NO mute during capture — you monitor while you record, like always. The monitor
            # plays the synth to your SPEAKERS; the capture records the synth off the USB INPUT.
            # Separate paths, no bleed. Muting your ears during the take was pointless.
            monitor_state["muted_by_capture"] = False
            # Map input channels → stereo output
            if n_input == 1:
                outdata[:, 0] = indata[:, 0]
                outdata[:, 1] = indata[:, 0]
            elif n_input == 2:
                outdata[:, 0] = indata[:, 0]
                outdata[:, 1] = indata[:, 1]
            else:
                # >2 channels: mix to stereo (first half → L, second half → R)
                mid = n_input // 2
                outdata[:, 0] = np.mean(indata[:, :mid], axis=1)
                outdata[:, 1] = np.mean(indata[:, mid:], axis=1)

        # Use selectors for input mapping so we read the specific TX-6 channels requested
        with sd.Stream(
            device=(in_idx, None),  # None = system default output
            channels=(n_input, 2),
            samplerate=sr,
            dtype="float32",
            blocksize=256,  # low latency (~5ms at 48k)
            callback=callback,
            extra_settings=None,
        ) as stream:
            # Re-map input channels via extra_settings if needed — actually for the
            # selected channels we need a different approach: open InputStream with
            # mapping. Falling back to default channel range for now (first N channels).
            monitor_state["running"] = True
            monitor_state["last_error"] = None
            while not monitor_stop_flag.is_set():
                monitor_stop_flag.wait(0.1)
    except Exception as e:
        monitor_state["last_error"] = str(e)
    finally:
        monitor_state["running"] = False
        monitor_state["muted_by_capture"] = False


@app.route("/api/monitor/start", methods=["POST"])
def monitor_start():
    """Start passthrough monitoring from input device to system default output."""
    global monitor_thread
    if monitor_state["running"]:
        return jsonify({"ok": True, "already_running": True, **monitor_state})
    params = request.get_json() or {}
    input_device = params.get("audio_device", "TX-6")
    channels = params.get("input_channels", "1,2")
    monitor_state["input_device"] = input_device
    monitor_state["input_channels"] = channels
    monitor_stop_flag.clear()
    monitor_thread = threading.Thread(
        target=_monitor_worker, args=(input_device, channels), daemon=True
    )
    monitor_thread.start()
    # Give the worker a moment to fail or start
    import time as _t; _t.sleep(0.3)
    return jsonify({"ok": True, **monitor_state})


@app.route("/api/monitor/stop", methods=["POST"])
def monitor_stop():
    """Stop passthrough monitoring."""
    monitor_stop_flag.set()
    monitor_state["running"] = False
    return jsonify({"ok": True})


@app.route("/api/monitor/status")
def monitor_status():
    return jsonify(monitor_state)


# --- live audition (loop MIDI to synth, no recording) ---
audition_stop_flag = threading.Event()
audition_thread: threading.Thread | None = None
# Keepalive — if the dashboard tab closes without firing /api/audition-stop, the
# loop_worker will auto-stop after AUDITION_KEEPALIVE_TIMEOUT seconds of no pings.
# Prevents "closed Safari and the synth is still playing forever" stuck state.
import time as _time_mod
audition_last_ping: float = 0.0
AUDITION_KEEPALIVE_TIMEOUT = 60.0  # seconds — frontend pings every 15s, we allow 4 misses

# --- MIDI INPUT LISTENER (the bench LISTENS to the synth) ---
# Foundation for: catching PCs when Nick scrolls a synth's front panel,
# CC capture (knob twists), incoming-note recording, MIDI clock detection.
# A single in-port at a time. Background thread holds the port; events pushed
# to a thread-safe deque. Frontend polls /api/midi-listen/poll to drain.
from collections import deque
midi_listen_lock = threading.Lock()
midi_listen_port = None          # active rtmidi.MidiIn instance (or None)
midi_listen_port_name = None     # human-readable port name we're listening to
midi_listen_events = deque(maxlen=200)  # recent events; UI drains via poll


@app.route("/api/audition-ping", methods=["POST"])
def audition_ping():
    """Frontend pings this while audition is running; missing pings auto-stop the loop."""
    global audition_last_ping
    audition_last_ping = _time_mod.time()
    return jsonify({"ok": True})


@app.route("/api/audition-start", methods=["POST"])
def audition_start():
    """Loop a chord progression through MIDI — no recording. Fires to EVERY synth in `parts` (so Preview
    plays the Moog too, not just the JP), exactly like Collect: a part named bass/sub plays the root an
    octave down; every other part plays the full chord. Falls back to a single `midi_port` if no parts
    are given. Stop via /api/audition-stop."""
    global audition_thread
    if audition_thread and audition_thread.is_alive():
        return jsonify({"error": "audition already running. Stop first."}), 409
    params = request.get_json() or {}
    chords_str = params.get("progression", "Cm Ab Eb Bb")
    bpm = float(params.get("bpm", 120))
    bars_per_chord = float(params.get("bars_per_chord", 2))
    send_mode = params.get("send_mode", "chord")
    midi_port_name = params.get("midi_port", "Moog Grandmother")
    midi_channel = int(params.get("midi_channel", 1)) - 1
    velocity = int(params.get("velocity", 80))
    # Optional multi-synth parts (mirrors Collect). Each: {name, port, channel}. channel "all" → every
    # channel (handy when a synth's receive channel is unknown, like the Grandmother on its own port).
    import re as _re_parts
    parts_spec = []
    for p in (params.get("parts") or []):
        port_name = (p.get("port") or "").strip()
        if not port_name:
            continue
        chv = str(p.get("channel", "1")).strip().lower()
        channels = list(range(16)) if chv == "all" else [max(0, int(chv) - 1)]
        role = "bass" if _re_parts.search(r"bass|sub", (p.get("name") or ""), _re_parts.I) else "chords"
        parts_spec.append({"port": port_name, "channels": channels, "role": role})

    def loop_worker():
        import mido
        import time as _time
        import re as _re
        # parse chords inline (small helper)
        ROOTS = {'c':0,'c#':1,'cs':1,'db':1,'d':2,'d#':3,'ds':3,'eb':3,'e':4,'f':5,'f#':6,'fs':6,'gb':6,'g':7,'g#':8,'gs':8,'ab':8,'a':9,'a#':10,'as':10,'bb':10,'b':11}
        CHORD_TYPES = {'':[0,4,7],'maj':[0,4,7],'M':[0,4,7],'maj7':[0,4,7,11],'M7':[0,4,7,11],'maj9':[0,4,7,11,14],'m':[0,3,7],'min':[0,3,7],'minor':[0,3,7],'m7':[0,3,7,10],'min7':[0,3,7,10],'m9':[0,3,7,10,14],'m7b5':[0,3,6,10],'7':[0,4,7,10],'dim':[0,3,6],'dim7':[0,3,6,9],'aug':[0,4,8],'sus2':[0,2,7],'sus4':[0,5,7]}
        def chord_midi(name, oct_=3):
            m = _re.match(r'^([A-Ga-g])([#b]?)\s*(.*)$', name.strip())
            if not m: return []
            letter, acc, suff = m.groups()
            base = ROOTS.get(letter.lower(), 0)
            if acc == '#': base += 1
            elif acc == 'b': base -= 1
            base = base % 12
            ints = CHORD_TYPES.get(suff.strip(), [0,4,7])
            root = base + (oct_+1)*12
            return [root + i for i in ints]
        chords = [chord_midi(c) for c in chords_str.split()]

        avail = mido.get_output_names()
        opened = {}   # real device name -> open port (each unique device opened once)
        def get_port(name):
            ms = [pp for pp in avail if name.lower() in pp.lower()]
            if not ms:
                return None
            real = ms[0]
            if real not in opened:
                opened[real] = mido.open_output(real)
            return opened[real]

        # Build voices — one per (port, channel). Multi-synth from parts; else the single-port fallback.
        voices = []   # each: {'port': <mido port>, 'channel': int, 'role': 'bass'|'chords'}
        if parts_spec:
            for ps in parts_spec:
                pt = get_port(ps["port"])
                if pt is None:
                    continue
                for ch in ps["channels"]:
                    voices.append({"port": pt, "channel": ch, "role": ps["role"]})
        else:
            pt = get_port(midi_port_name)
            if pt is not None:
                voices.append({"port": pt, "channel": midi_channel, "role": "chords"})
        if not voices:
            return

        sec_per_bar = (60.0 / bpm) * 4
        sec_per_chord = bars_per_chord * sec_per_bar
        total_loop_sec = sec_per_chord * len(chords)

        def sleep_until(target_t: float) -> bool:
            """Interruptible sleep until target_t. Returns True if stop flag set."""
            while True:
                remaining = target_t - _time.time()
                if remaining <= 0:
                    return False
                if audition_stop_flag.wait(min(remaining, 0.05)):
                    return True

        def notes_for(chord, role):
            if not chord:
                return []
            if role == "bass":
                return [chord[0] - 12]
            return [chord[0]] if send_mode == "root" else chord

        # Arp only makes sense for a single synth; multi-synth previews sustain (matches Collect).
        arp_single = (len(voices) == 1 and send_mode == "arp")

        try:
            # ABSOLUTE time anchor — every chord lands at a deterministic offset from
            # t_loop_start, so MIDI send overhead doesn't accumulate as drift.
            # This is what makes the audition loop-to-click sync stay tight over many passes.
            t_loop_start = _time.time()
            pass_count = 0
            while not audition_stop_flag.is_set():
                held = {}   # voice index -> [notes currently on]
                pass_start_t = t_loop_start + pass_count * total_loop_sec
                for i, chord in enumerate(chords):
                    if audition_stop_flag.is_set(): break
                    if not chord: continue
                    target_t = pass_start_t + i * sec_per_chord
                    # wait until this chord's start moment
                    if sleep_until(target_t): break
                    if arp_single:
                        v = voices[0]
                        for n in held.get(0, []):
                            v["port"].send(mido.Message('note_off', note=n, velocity=0, channel=v["channel"]))
                        held[0] = []
                        arp_interval = sec_per_bar / 16  # 1/16 note
                        n_arp = max(1, int(round(sec_per_chord / arp_interval)))
                        # Arp notes anchored to target_t so they land on each 16th-note grid position.
                        for k in range(n_arp):
                            if audition_stop_flag.is_set(): break
                            t_arp_on = target_t + k * arp_interval
                            t_arp_off = target_t + k * arp_interval + arp_interval * 0.8
                            if sleep_until(t_arp_on): break
                            note = chord[k % len(chord)]
                            v["port"].send(mido.Message('note_on', note=note, velocity=velocity, channel=v["channel"]))
                            if sleep_until(t_arp_off): break
                            v["port"].send(mido.Message('note_off', note=note, velocity=0, channel=v["channel"]))
                    else:
                        # Sustained chord/root across every voice — each synth on its own port + channel.
                        for vi, v in enumerate(voices):
                            play = notes_for(chord, v["role"])
                            for n in held.get(vi, []):
                                v["port"].send(mido.Message('note_off', note=n, velocity=0, channel=v["channel"]))
                            for n in play:
                                v["port"].send(mido.Message('note_on', note=n, velocity=velocity, channel=v["channel"]))
                            held[vi] = play
                # end of one pass — release held notes, wait until exact loop boundary
                # before starting the next pass (this prevents the cleanup overhead from
                # pushing pass 2's downbeat past the click's bar 1)
                for vi, v in enumerate(voices):
                    for n in held.get(vi, []):
                        v["port"].send(mido.Message('note_off', note=n, velocity=0, channel=v["channel"]))
                # Keepalive check: if the frontend stopped pinging (e.g. tab closed),
                # bail out so we don't loop forever after the user walked away.
                if _time_mod.time() - audition_last_ping > AUDITION_KEEPALIVE_TIMEOUT:
                    print(f"audition: keepalive timeout ({AUDITION_KEEPALIVE_TIMEOUT}s), auto-stopping")
                    break
                pass_end_t = t_loop_start + (pass_count + 1) * total_loop_sec
                if sleep_until(pass_end_t): break
                pass_count += 1
        finally:
            # cleanup — all-notes-off on every channel of every opened device, then close
            for pt in opened.values():
                for ch in range(16):
                    try:
                        pt.send(mido.Message('control_change', control=123, value=0, channel=ch))
                    except Exception:
                        pass
                pt.close()

    global audition_last_ping
    audition_last_ping = _time_mod.time()  # fresh keepalive timer on start
    audition_stop_flag.clear()
    audition_thread = threading.Thread(target=loop_worker, daemon=True)
    audition_thread.start()
    return jsonify({"ok": True, "looping": chords_str, "bpm": bpm, "synths": len(parts_spec) or 1})


@app.route("/api/audition-stop", methods=["POST"])
def audition_stop():
    """Stop the live audition loop."""
    audition_stop_flag.set()
    return jsonify({"ok": True})


@app.route("/api/audition-compose", methods=["POST"])
def audition_compose():
    """Audition a composed session — fires N .mid files simultaneously through MIDI.

    Each track can route to its OWN MIDI port (v1 multi-synth). If a track omits
    midi_port, it falls back to the top-level midi_port. The server opens one
    output connection per unique destination port and routes events accordingly.

    Body: {
      tracks: [
        {"midi_path": "patterns/midi/progressions/X.mid", "channel": 1,  "midi_port": "mio"},
        {"midi_path": "patterns/midi/bass/X.mid",         "channel": 2,  "midi_port": "TR-8S"},
        {"midi_path": "patterns/midi/drums/X.mid",        "channel": 10, "midi_port": "TR-8S"},
      ],
      bpm: 120,
      midi_port: "mio"            # fallback for tracks without their own port
    }

    All tracks loop together; loop length = the longest track. Stop via /api/audition-stop.
    """
    global audition_thread, audition_last_ping
    if audition_thread and audition_thread.is_alive():
        return jsonify({"error": "audition already running. Stop first."}), 409
    params = request.get_json() or {}
    tracks_in = params.get("tracks", [])
    bpm = float(params.get("bpm", 120))
    default_port_name = params.get("midi_port") or "Moog Grandmother"
    velocity_scale = float(params.get("velocity_scale", 1.0))

    # Resolve each track's .mid file + load events.
    # `port_name` per track is what the user requested (may be substring like "mio");
    # we resolve to real Core MIDI port names inside the worker.
    tracks = []
    for t in tracks_in:
        midi_path_rel = t.get("midi_path")
        channel = int(t.get("channel", 1)) - 1  # 0-indexed for mido
        track_port = t.get("midi_port") or default_port_name
        if not midi_path_rel:
            continue
        full_path = BANK_ROOT / midi_path_rel
        if not full_path.exists():
            continue
        try:
            import mido
            mid = mido.MidiFile(str(full_path))
            events = []  # (absolute_tick, type, note, vel)
            abs_tick = 0
            for msg in mid.tracks[0]:
                abs_tick += msg.time
                if msg.type in ("note_on", "note_off"):
                    events.append((abs_tick, msg.type, msg.note, msg.velocity))
            tracks.append({
                "channel": channel,
                "events": events,
                "total_ticks": abs_tick,
                "ppqn": mid.ticks_per_beat,
                "label": midi_path_rel,
                "port_name": track_port,
            })
        except Exception as _e:
            print(f"audition-compose: couldn't load {midi_path_rel}: {_e}", file=sys.stderr)

    if not tracks:
        return jsonify({"error": "no valid tracks to play"}), 400

    # All tracks loop together for the duration of the longest track (in beats)
    max_beats = max(t["total_ticks"] / t["ppqn"] for t in tracks)
    loop_sec = (60.0 / bpm) * max_beats

    # Resolve every unique requested port name to a real Core MIDI port.
    # We do this OUTSIDE the worker so we can fail fast in the response.
    import mido as _mido_resolve
    available = _mido_resolve.get_output_names()
    resolved_ports = {}  # requested_name -> real port name (or None if unresolved)
    for tr in tracks:
        req = tr["port_name"]
        if req in resolved_ports:
            continue
        matches = [p for p in available if req.lower() in p.lower()]
        resolved_ports[req] = matches[0] if matches else None

    unresolved = [n for n, real in resolved_ports.items() if real is None]
    if unresolved:
        return jsonify({
            "error": f"no MIDI port matches: {', '.join(unresolved)}",
            "available": available,
        }), 404

    def compose_worker():
        import mido as _mido
        import time as _time

        # Open one output per UNIQUE real port (multiple tracks → same real port reuse one conn)
        open_ports = {}  # real_name -> mido output
        for req, real in resolved_ports.items():
            if real not in open_ports:
                try:
                    open_ports[real] = _mido.open_output(real)
                except Exception as _e:
                    print(f"audition-compose: couldn't open {real}: {_e}", file=sys.stderr)
                    return

        # Map each track to its destination port object
        for tr in tracks:
            tr["_port"] = open_ports[resolved_ports[tr["port_name"]]]

        # Pre-compute event timing in absolute seconds (one loop pass).
        # Tag every event with its destination port so the playback loop knows where to send.
        def build_event_list(track):
            sec_per_tick = (60.0 / bpm) / track["ppqn"]
            out = []
            for abs_tick, mtype, note, vel in track["events"]:
                t_sec = abs_tick * sec_per_tick
                scaled_vel = min(127, max(0, int(round(vel * velocity_scale)))) if mtype == "note_on" else 0
                out.append((t_sec, mtype, note, scaled_vel, track["channel"], track["_port"]))
            return out

        all_events = []
        for tr in tracks:
            all_events.extend(build_event_list(tr))
        all_events.sort(key=lambda e: e[0])

        try:
            t_loop_start = _time.time()
            pass_count = 0
            held = set()  # (port_id, channel, note) currently sounding — released between loops
            while not audition_stop_flag.is_set():
                pass_start_t = t_loop_start + pass_count * loop_sec
                for sec_offset, mtype, note, vel, ch, dest_port in all_events:
                    if audition_stop_flag.is_set():
                        break
                    target_t = pass_start_t + sec_offset
                    while True:
                        remaining = target_t - _time.time()
                        if remaining <= 0:
                            break
                        if audition_stop_flag.wait(min(remaining, 0.05)):
                            break
                    if audition_stop_flag.is_set():
                        break
                    port_id = id(dest_port)
                    if mtype == "note_on" and vel > 0:
                        dest_port.send(_mido.Message("note_on", note=note, velocity=vel, channel=ch))
                        held.add((port_id, ch, note))
                    else:
                        dest_port.send(_mido.Message("note_off", note=note, velocity=0, channel=ch))
                        held.discard((port_id, ch, note))
                # Release anything still held between passes (safety) — but route to correct port
                for port_id, ch, note in list(held):
                    for p in open_ports.values():
                        if id(p) == port_id:
                            p.send(_mido.Message("note_off", note=note, velocity=0, channel=ch))
                            break
                held.clear()
                if _time_mod.time() - audition_last_ping > AUDITION_KEEPALIVE_TIMEOUT:
                    print(f"audition-compose: keepalive timeout, stopping", file=sys.stderr)
                    break
                pass_count += 1
        finally:
            # Hard cleanup — All Notes Off (CC123) on every (port, channel) we touched
            touched = {}  # port_obj -> set of channels
            for tr in tracks:
                touched.setdefault(tr["_port"], set()).add(tr["channel"])
            for p, channels in touched.items():
                for ch in channels:
                    try:
                        p.send(_mido.Message("control_change", control=123, value=0, channel=ch))
                    except Exception:
                        pass
                try:
                    p.close()
                except Exception:
                    pass

    audition_last_ping = _time_mod.time()
    audition_stop_flag.clear()
    audition_thread = threading.Thread(target=compose_worker, daemon=True)
    audition_thread.start()

    # AUTO-SYNC — broadcast MIDI clock to every port in this audition so the Moog arp,
    # the KO sequencer, and any tempo-synced FX all lock to the same master tempo.
    # Without this, each device's internal sequencer drifts on its own clock.
    ports_used = sorted(set(v for v in resolved_ports.values() if v))
    clock_started = False
    if params.get("sync_clock", True) and ports_used:
        try:
            global clock_thread
            if clock_state["running"]:
                clock_stop_flag.set()
                if clock_thread and clock_thread.is_alive():
                    clock_thread.join(timeout=0.5)
            clock_stop_flag.clear()
            clock_state["running"] = True
            clock_state["bpm"] = bpm
            clock_state["port"] = ",".join(sorted(ports_used))
            clock_state["ports"] = ports_used
            clock_thread = threading.Thread(target=_clock_worker, args=(ports_used, bpm), daemon=True)
            clock_thread.start()
            clock_started = True
        except Exception as _e:
            print(f"audition-compose: couldn't start sync clock: {_e}", file=sys.stderr)

    return jsonify({
        "ok": True,
        "tracks": [
            {"channel": t["channel"] + 1, "path": t["label"],
             "port_requested": t["port_name"], "port_resolved": resolved_ports[t["port_name"]]}
            for t in tracks
        ],
        "bpm": bpm,
        "loop_sec": round(loop_sec, 2),
        "midi_port": default_port_name,
        "ports_used": ports_used,
        "clock_broadcasting": clock_started,
        "clock_ports": ports_used if clock_started else [],
    })


@app.route("/api/scan-all-devices", methods=["POST"])
def scan_all_devices():
    """Fire one MIDI note + sample every audio input device to find where signal is."""
    if not capture_lock.acquire(blocking=False):
        return jsonify({"error": "capture in progress"}), 409
    try:
        params = request.get_json() or {}
        midi_port_name = params.get("midi_port", "Moog Grandmother")
        note = params.get("note", 45)  # A2

        import mido
        import sounddevice as sd
        import numpy as np
        import threading
        import time

        # find MIDI port
        ports = mido.get_output_names()
        midi_matches = [p for p in ports if midi_port_name.lower() in p.lower()]
        if not midi_matches:
            return jsonify({"ok": False, "error": f"no MIDI port: {midi_port_name}"}), 404

        # get all input devices
        all_devs = sd.query_devices()
        input_devs = [(i, d) for i, d in enumerate(all_devs) if d["max_input_channels"] > 0]

        # results
        results = []

        midi_port = mido.open_output(midi_matches[0])
        try:
            for dev_idx, dev in input_devs:
                # record 1.5s on each device
                ch_count = min(dev["max_input_channels"], 12)
                sr = int(dev.get("default_samplerate") or 48000)
                duration = 1.5
                try:
                    rec = sd.rec(int(duration * sr), samplerate=sr, channels=ch_count, device=dev_idx,
                                 mapping=list(range(1, ch_count + 1)), dtype='float32', blocking=False)
                    time.sleep(0.05)
                    midi_port.send(mido.Message('note_on', note=note, velocity=110, channel=0))
                    time.sleep(0.7)
                    midi_port.send(mido.Message('note_off', note=note, velocity=0, channel=0))
                    sd.wait()
                    # per-channel peak
                    peaks = []
                    for c in range(ch_count):
                        peak = float(np.max(np.abs(rec[:, c])))
                        db = 20 * np.log10(max(1e-10, peak))
                        peaks.append({"ch": c + 1, "db": db if db > -100 else None})
                    hot = [p for p in peaks if p["db"] is not None and p["db"] > -40]
                    results.append({
                        "device": dev["name"],
                        "index": dev_idx,
                        "channels": ch_count,
                        "sample_rate": sr,
                        "hot_channels": [p["ch"] for p in hot],
                        "all_peaks": peaks,
                        "max_peak_db": max((p["db"] for p in peaks if p["db"] is not None), default=None),
                    })
                except Exception as e:
                    results.append({"device": dev["name"], "index": dev_idx, "error": str(e)})
        finally:
            midi_port.send(mido.Message('control_change', control=123, value=0, channel=0))
            midi_port.close()

        # Find best match — device with highest max_peak_db
        best = None
        for r in results:
            if r.get("hot_channels"):
                if not best or (r.get("max_peak_db") or -100) > (best.get("max_peak_db") or -100):
                    best = r
        return jsonify({"ok": True, "results": results, "best_device": best})
    finally:
        capture_lock.release()


@app.route("/api/test-midi", methods=["POST"])
def test_midi():
    """Send a single MIDI note to verify MIDI path is working — no audio recording."""
    params = request.get_json() or {}
    midi_port_name = params.get("midi_port", "Moog Grandmother")
    note = params.get("note", 60)  # MIDI C4
    velocity = params.get("velocity", 100)
    duration = params.get("duration", 0.5)
    midi_channel = params.get("midi_channel", 1) - 1
    try:
        import mido
        ports = mido.get_output_names()
        matches = [p for p in ports if midi_port_name.lower() in p.lower()]
        if not matches:
            return jsonify({"ok": False, "error": f"no MIDI port matches '{midi_port_name}'", "available": ports}), 404
        port = mido.open_output(matches[0])
        try:
            port.send(mido.Message('note_on', note=note, velocity=velocity, channel=midi_channel))
            import time
            time.sleep(duration)
            port.send(mido.Message('note_off', note=note, velocity=0, channel=midi_channel))
        finally:
            port.close()
        return jsonify({"ok": True, "port": matches[0], "note": note, "duration_sec": duration})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/scan-channels", methods=["POST"])
def scan_channels():
    """Fire one MIDI note + record ALL channels of the audio device for 5 sec.
    Returns which channels have hot signal. Helps find where the synth audio is routed."""
    if not capture_lock.acquire(blocking=False):
        return jsonify({"error": "capture in progress"}), 409
    try:
        params = request.get_json() or {}
        py = str(VENV_PY) if VENV_PY.exists() else sys.executable
        cmd = [
            py, str(TOOLS_DIR / "capture" / "scan-channels.py"),
            "--device", params.get("audio_device", "TX-6"),
            "--midi-port", params.get("midi_port", "Moog Grandmother"),
            "--midi-channel", str(params.get("midi_channel", 1)),
            "--note", params.get("note", "a2"),
            "--velocity", str(params.get("velocity", 100)),
            "--duration", str(params.get("duration", 5)),
            "--hold", str(params.get("hold", 3)),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        # parse per-channel peaks from stdout
        per_channel = []
        active = []
        for line in result.stdout.split("\n"):
            m = re.match(r"\s*ch\s*(\d+):\s*([\-\+\d.\sinf]+)\s*dBFS", line)
            if m:
                ch = int(m.group(1))
                val = m.group(2).strip()
                if "inf" in val:
                    db = float("-inf")
                else:
                    try: db = float(val)
                    except: db = float("-inf")
                per_channel.append({"channel": ch, "db": db if db != float("-inf") else None})
                if db > -40 and db != float("-inf"):
                    active.append(ch)

        recommendation = None
        if len(active) >= 2:
            # stereo pair detected
            recommendation = ",".join(str(c) for c in active[:2])
        elif len(active) == 1:
            recommendation = str(active[0])

        return jsonify({
            "ok": result.returncode == 0,
            "per_channel": per_channel,
            "active_channels": active,
            "recommendation": recommendation,
            "stdout_tail": result.stdout[-500:],
        })
    finally:
        capture_lock.release()


@app.route("/api/meter", methods=["POST"])
def meter():
    """Record a quick audio check on the current input. Saves a temp WAV +
    returns peak dB + WAV URL so the frontend can draw the Serato waveform
    of what was just captured. Visual confidence at-a-glance."""
    if not capture_lock.acquire(blocking=False):
        return jsonify({"error": "capture in progress"}), 409
    try:
        import sounddevice as _sd
        import numpy as _np
        import soundfile as _sf

        params = request.get_json() or {}
        audio_dev_name = params.get("audio_device", "TX-6")
        ch_str = params.get("input_channels", "1,2")
        duration = float(params.get("duration", 3.0))
        try:
            channels = [int(c.strip()) for c in ch_str.split(",")]
        except Exception:
            return jsonify({"ok": False, "error": f"bad input_channels: {ch_str}"}), 400

        # Resolve device index
        devs = _sd.query_devices()
        matches = [(i, d) for i, d in enumerate(devs)
                   if d["max_input_channels"] > 0 and audio_dev_name.lower() in d["name"].lower()]
        if not matches:
            return jsonify({"ok": False, "error": f"no audio device matches '{audio_dev_name}'"}), 404
        dev_idx = matches[0][0]

        # Force a fresh sounddevice — sometimes Core Audio caches stale port info
        try:
            _sd._terminate(); _sd._initialize()
        except Exception:
            pass

        # Record
        sr = 48000
        rec = _sd.rec(int(duration * sr), samplerate=sr, channels=len(channels),
                      device=dev_idx, mapping=channels, dtype="float32", blocking=True)
        peak = float(_np.abs(rec).max()) if rec is not None and rec.size else 0.0
        peak_db = float(20.0 * _np.log10(peak)) if peak > 0 else None

        # Save the WAV so the UI can draw its waveform
        preview_dir = BANK_ROOT / "_meter_previews"
        preview_dir.mkdir(exist_ok=True)
        preview_path = preview_dir / "latest.wav"
        _sf.write(str(preview_path), rec, sr, subtype="PCM_24")

        rel = preview_path.relative_to(BANK_ROOT)
        return jsonify({
            "ok": True,
            "peak_db": peak_db,
            "peak_pct": round(peak * 100, 2),
            "wav_url": f"/audio/{rel}",
            "channels": channels,
            "device": matches[0][1]["name"],
            "duration_sec": duration,
        })
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e), "trace": traceback.format_exc()[-500:]}), 500
    finally:
        capture_lock.release()


@app.route("/api/zip-pack", methods=["POST"])
def zip_pack():
    """Zip the built pack folder for Gumroad upload."""
    params = request.get_json() or {}
    instrument = params.get("instrument", "grandmother")
    vol = params.get("vol", 1)
    releases_dir = BANK_ROOT / "releases"
    pack_name = f"spacepit-{instrument}-vol{vol}"
    pack_dir = releases_dir / pack_name
    zip_path = releases_dir / f"{pack_name}.zip"
    if not pack_dir.exists():
        return jsonify({"ok": False, "error": f"pack not built: {pack_dir}"}), 404
    if zip_path.exists():
        zip_path.unlink()
    try:
        result = subprocess.run(
            ["zip", "-r", str(zip_path), pack_name, "-q"],
            cwd=str(releases_dir), capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            return jsonify({"ok": False, "stderr": result.stderr}), 500
        size_mb = zip_path.stat().st_size / (1024 * 1024)
        return jsonify({"ok": True, "zip_path": str(zip_path.relative_to(BANK_ROOT)), "size_mb": round(size_mb, 1)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/open-folder", methods=["POST"])
def open_folder():
    """Open a folder in Finder. Body: { kind: 'pack' | 'source' | 'patches' | 'loops' | 'photos' }."""
    params = request.get_json() or {}
    kind = params.get("kind", "source")
    instrument = params.get("instrument", "grandmother")
    paths = {
        "pack":     BANK_ROOT / "releases" / f"spacepit-{instrument}-vol1",
        "source":   INSTRUMENTS_DIR / instrument,
        "patches":  INSTRUMENTS_DIR / instrument / "patches",
        "loops":    INSTRUMENTS_DIR / instrument / "loops" / "raw",
        "photos":   INSTRUMENTS_DIR / instrument / "photos",
        "ableton-ready": BANK_ROOT / "releases" / f"spacepit-{instrument}-vol1" / "audio" / "ableton-ready",
    }
    path = paths.get(kind)
    if not path or not path.exists():
        return jsonify({"ok": False, "error": f"path not found: {path}"}), 404
    subprocess.Popen(["open", str(path)])
    return jsonify({"ok": True, "opened": str(path)})


@app.route("/api/instruments", methods=["POST"])
def create_instrument():
    """Create a new instrument folder + manifest. Body: { slug, name, category, tier }."""
    params = request.get_json() or {}
    slug = (params.get("slug") or "").strip().lower()
    if not slug or not re.match(r"^[a-z0-9-]+$", slug):
        return jsonify({"ok": False, "error": "slug must be lowercase letters/numbers/hyphens only"}), 400
    if (INSTRUMENTS_DIR / slug).exists():
        return jsonify({"ok": False, "error": f"instrument '{slug}' already exists"}), 409
    name = params.get("name") or slug.replace("-", " ").title()
    category = params.get("category") or "synth"
    tier = params.get("tier") or "standard"

    instr_dir = INSTRUMENTS_DIR / slug
    instr_dir.mkdir(parents=True)
    for sub in ["patches", "loops/raw", "loops/processed", "sweeps/raw", "exports", "photos"]:
        (instr_dir / sub).mkdir(parents=True, exist_ok=True)

    manifest = {
        "slug": slug,
        "name": name,
        "category": category,
        "tier": tier,
        "sampling_dates": [],
        "chains_captured": ["raw"],
        "patches": [],
        "one_shots_dir": None,
        "loops": [],
        "exports": {
            "ableton_move": None, "mpc": None, "op_z": None,
            "ep_133": None, "sfz": None, "decent_sampler": None,
        },
        "preview_samples": [],
        "site": {"gear_slug_in_sanity": None, "published_at": None, "free_preview_count": 5},
        "video": {"process_video_url": None, "shot_list": []},
        "store": {"free_tier_count": 5, "paid_kit_url": None, "price_usd": None},
    }
    (instr_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    return jsonify({"ok": True, "slug": slug, "path": str(instr_dir.relative_to(BANK_ROOT))})


@app.route("/api/audit-pack", methods=["POST"])
def audit_pack():
    """Run a full QC audit on the built pack."""
    params = request.get_json() or {}
    instrument = params.get("instrument", "grandmother")
    pack_dir = BANK_ROOT / "releases" / f"spacepit-{instrument}-vol1"
    if not pack_dir.exists():
        return jsonify({"ok": False, "error": f"pack not built: {pack_dir}"}), 404
    py = str(VENV_PY) if VENV_PY.exists() else sys.executable
    script = TOOLS_DIR / "pack" / "audit-pack.py"
    try:
        result = subprocess.run(
            [py, str(script), "--pack", str(pack_dir), "--json"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            return jsonify({"ok": False, "stderr": result.stderr}), 500
        report = json.loads(result.stdout)
        return jsonify(report)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/audio/<path:filepath>")
def serve_audio(filepath):
    abs_path = BANK_ROOT / filepath
    # security: only serve from BANK_ROOT
    try:
        abs_path.resolve().relative_to(BANK_ROOT.resolve())
    except ValueError:
        abort(403)
    if not abs_path.exists():
        abort(404)
    return send_from_directory(abs_path.parent, abs_path.name)


if __name__ == "__main__":
    import os as _os
    import socket as _socket
    import subprocess as _subproc
    port = 8001
    # BANK_DASHBOARD_HOST env var: set to "0.0.0.0" to expose on LAN (default localhost-only)
    host = _os.environ.get("BANK_DASHBOARD_HOST", "127.0.0.1")
    # Resolve LAN IP (primary route) for the welcome banner
    lan_ip = None
    try:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        lan_ip = s.getsockname()[0]
        s.close()
    except Exception:
        lan_ip = None
    # Also try to surface a Tailscale IP for "hit it from anywhere" usage.
    # Tailscale uses CGNAT range 100.64.0.0/10 — scan interfaces for any IP in that range.
    # Works whether Tailscale was installed via CLI or App Store (which has no CLI).
    tailscale_ip = None
    try:
        r = _subproc.run(["ifconfig"], capture_output=True, text=True, timeout=2)
        import re as _re
        for line in r.stdout.splitlines():
            m = _re.search(r"inet (100\.(\d+)\.\d+\.\d+)", line)
            if m:
                second_octet = int(m.group(2))
                # CGNAT range is 100.64.x.x through 100.127.x.x
                if 64 <= second_octet <= 127:
                    tailscale_ip = m.group(1)
                    break
    except Exception:
        pass
    print(f"=== spacepit capture dashboard ===")
    print(f"  bank:   {BANK_ROOT}")
    print(f"  local:  http://localhost:{port}/")
    if host == "0.0.0.0":
        if lan_ip:
            print(f"  LAN:    http://{lan_ip}:{port}/  ← phone/iPad on same Wi-Fi")
        if tailscale_ip and tailscale_ip != lan_ip:
            print(f"  remote: http://{tailscale_ip}:{port}/  ← Tailscale (from anywhere)")
    elif host == "127.0.0.1":
        print(f"  (localhost-only — run via 'bankshare' or set BANK_DASHBOARD_HOST=0.0.0.0 to expose)")
    print()
    app.run(host=host, port=port, debug=False)
