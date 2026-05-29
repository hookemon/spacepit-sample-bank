"""the studio dreamer — generative band that plays your physical studio over MIDI.

You assign each PLAYER (character) to a synth + MIDI channel:
  chords → JP-8000 ch1, bass → Moog ch1, drums → EP-133 ch10, lead → ...
then pick a VIBE (meditation / hiphop / party) and hit play. The dreamer generates
endless music in key and fires it to the gear. Probabilistic, so it never repeats exactly.

This is the v0 of the north star ("when you go to bed type shit"). Next: free-text vibes,
time-of-day scheduling (morning meditation → daytime → party), more characters.
"""
import random
import threading
import time

try:
    import mido
except Exception:
    mido = None

NOTE_IDX = {"C": 0, "C#": 1, "DB": 1, "D": 2, "D#": 3, "EB": 3, "E": 4, "F": 5,
            "F#": 6, "GB": 6, "G": 7, "G#": 8, "AB": 8, "A": 9, "A#": 10, "BB": 10, "B": 11}
SCALES = {"major": [0, 2, 4, 5, 7, 9, 11], "minor": [0, 2, 3, 5, 7, 8, 10],
          "dorian": [0, 2, 3, 5, 7, 9, 10]}
CHORDS = {"maj": [0, 4, 7], "min": [0, 3, 7], "maj7": [0, 4, 7, 11], "m7": [0, 3, 7, 10],
          "m9": [0, 3, 7, 10, 14], "maj9": [0, 4, 7, 11, 14], "7": [0, 4, 7, 10],
          "m7b5": [0, 3, 6, 10], "add9": [0, 4, 7, 14], "sus4": [0, 5, 7]}

# Each VIBE = the band's mode. prog entries are (scale-degree 0-6, chord quality).
VIBES = {
    "meditation": {
        "tempo": 56, "scale": "major", "chord_beats": 8, "vel": 60,
        "prog": [(0, "maj9"), (5, "m9"), (3, "maj9"), (4, "sus4")],
        "chord_oct": 4, "bass_oct": 2, "bass_feel": "hold",
        "lead_density": 0.5, "lead_oct": 5, "drums": None, "swing": 0.0,
    },
    "hiphop": {   # boom-bap — lazy 5-chord jazzy loop, fat bass, dusty drums
        "tempo": 86, "scale": "dorian", "chord_beats": 4, "vel": 80,
        "prog": [(0, "m9"), (3, "m7"), (5, "maj7"), (1, "m7b5"), (4, "7")],
        "chord_oct": 4, "bass_oct": 2, "bass_feel": "root8",
        "lead_density": 0.15, "lead_oct": 5, "drums": "boombap", "swing": 0.16,
    },
    "party": {    # four-on-the-floor rave
        "tempo": 128, "scale": "minor", "chord_beats": 2, "vel": 92,
        "prog": [(0, "min"), (5, "maj"), (3, "maj"), (4, "maj")],
        "chord_oct": 4, "bass_oct": 2, "bass_feel": "root8",
        "lead_density": 0.0, "lead_oct": 5, "drums": "four", "swing": 0.0,
    },
    "lofi": {     # dusty study beat — slower, softer cousin of hiphop
        "tempo": 72, "scale": "dorian", "chord_beats": 4, "vel": 68,
        "prog": [(0, "m9"), (3, "maj7"), (5, "m7"), (1, "7")],
        "chord_oct": 4, "bass_oct": 2, "bass_feel": "root8",
        "lead_density": 0.1, "lead_oct": 5, "drums": "boombap", "swing": 0.2,
    },
    "ambient": {  # weightless — even more spacious than meditation
        "tempo": 48, "scale": "major", "chord_beats": 8, "vel": 52,
        "prog": [(0, "maj9"), (3, "maj9"), (5, "m9"), (0, "maj9")],
        "chord_oct": 4, "bass_oct": 2, "bass_feel": "hold",
        "lead_density": 0.35, "lead_oct": 5, "drums": None, "swing": 0.0,
    },
    "house": {    # deep house groove
        "tempo": 122, "scale": "minor", "chord_beats": 2, "vel": 86,
        "prog": [(0, "m7"), (5, "maj7"), (3, "maj7"), (4, "7")],
        "chord_oct": 4, "bass_oct": 2, "bass_feel": "root8",
        "lead_density": 0.0, "lead_oct": 5, "drums": "four", "swing": 0.0,
    },
    "trap": {     # dark, half-time feel, 808 bass, rolling hats
        "tempo": 140, "scale": "minor", "chord_beats": 4, "vel": 90,
        "prog": [(0, "min"), (0, "min"), (5, "maj"), (3, "maj")],
        "chord_oct": 3, "bass_oct": 1, "bass_feel": "root8",
        "lead_density": 0.05, "lead_oct": 5, "drums": "trap", "swing": 0.0,
    },
}
DRUM = {"kick": 36, "snare": 38, "hat": 42, "ohat": 46}

# free-text vibe → vibe key. Type a word, the dreamer figures out the mood.
VIBE_WORDS = {
    "meditation": ["meditation", "meditate", "calm", "zen", "peace", "peaceful", "sleep", "relax", "spa", "breathe", "still"],
    "ambient": ["ambient", "drift", "space", "spacey", "float", "floating", "dream", "dreamy", "cinematic", "pad", "weightless", "wash"],
    "lofi": ["lofi", "lo-fi", "study", "chill", "chilled", "dusty", "jazzy", "sleepy", "cozy", "rainy", "mellow"],
    "hiphop": ["hiphop", "hip-hop", "hip hop", "boombap", "boom-bap", "boom bap", "rap", "beat", "head-nod", "headnod", "90s", "soulful"],
    "house": ["house", "deep house", "disco", "groove", "groovy", "dance", "garage", "soulful house"],
    "party": ["party", "rave", "banger", "hype", "energy", "festival", "club", "euphoric", "anthem", "go off"],
    "trap": ["trap", "808", "drill", "dark", "hard", "modern", "menace", "moody", "night"],
}


def resolve_vibe(text):
    """Map free text to a vibe key. Exact key wins; else score by keyword hits; else hiphop."""
    if not text:
        return "hiphop"
    t = str(text).strip().lower()
    if t in VIBES:
        return t
    best, best_score = None, 0
    for vibe, words in VIBE_WORDS.items():
        score = sum(1 for w in words if w in t)
        if score > best_score:
            best, best_score = vibe, score
    return best or "hiphop"

_thread = None
_stop = threading.Event()
_ports = {}
_state = {"playing": False, "vibe": None, "key": None, "chord": None}


def _get_port(name):
    if name not in _ports:
        _ports[name] = mido.open_output(name)
    return _ports[name]


def _all_off():
    for p in _ports.values():
        try:
            for ch in range(16):
                p.send(mido.Message("control_change", control=123, value=0, channel=ch))
        except Exception:
            pass


def _close_ports():
    _all_off()
    for p in list(_ports.values()):
        try:
            p.close()
        except Exception:
            pass
    _ports.clear()


def status():
    return dict(_state)


# ---------------- time-of-day scheduler (scaffold) ----------------
# "morning meditation → picks up in the day → party at night." Default OFF so it never
# fires on its own. When enabled, a watcher checks the clock and switches the vibe to the
# active block, reusing the saved players (roles) + key. v0 scaffold — wire the day-arc.
_sched = {
    "enabled": False,
    "key": "C",
    "roles": None,            # last-used player assignment; set when enabled
    "blocks": [               # hour (0-23) → vibe; active block = latest hour <= now
        {"hour": 6, "vibe": "meditation"},
        {"hour": 8, "vibe": "lofi"},
        {"hour": 12, "vibe": "hiphop"},
        {"hour": 17, "vibe": "house"},
        {"hour": 21, "vibe": "party"},
        {"hour": 23, "vibe": "ambient"},
    ],
}
_sched_thread = None
_sched_stop = threading.Event()
_sched_last = None


def _active_block(hour):
    blocks = sorted(_sched["blocks"], key=lambda b: b["hour"])
    pick = blocks[-1]                       # wrap: before the first block = last night's block
    for b in blocks:
        if b["hour"] <= hour:
            pick = b
    return pick


def get_schedule():
    return dict(_sched)


def set_schedule(cfg):
    """Update the schedule. cfg may include blocks, key, roles, enabled."""
    global _sched_thread, _sched_last
    if "blocks" in cfg and cfg["blocks"]:
        _sched["blocks"] = cfg["blocks"]
    if "key" in cfg:
        _sched["key"] = cfg["key"]
    if "roles" in cfg and cfg["roles"]:
        _sched["roles"] = cfg["roles"]
    if "enabled" in cfg:
        _sched["enabled"] = bool(cfg["enabled"])

    if _sched["enabled"]:
        _sched_stop.clear()
        _sched_last = None
        if _sched_thread is None or not _sched_thread.is_alive():
            _sched_thread = threading.Thread(target=_sched_run, daemon=True)
            _sched_thread.start()
    else:
        _sched_stop.set()
    return get_schedule()


def _sched_run():
    """When enabled, switch the dreamer's vibe to match the time-of-day block."""
    global _sched_last
    while not _sched_stop.is_set() and _sched["enabled"]:
        hour = time.localtime().tm_hour
        block = _active_block(hour)
        if block["vibe"] != _sched_last and _sched["roles"]:
            _sched_last = block["vibe"]
            start({"vibe": block["vibe"], "key": _sched["key"], "roles": _sched["roles"]})
        # check again in ~30s (cheap; clock only moves so fast)
        for _ in range(30):
            if _sched_stop.is_set():
                break
            time.sleep(1)


def stop(disable_schedule=True):
    _stop.set()
    global _thread
    if _thread:
        _thread.join(timeout=2)
        _thread = None
    _close_ports()
    _state.update(playing=False, chord=None)
    if disable_schedule:                  # a manual STOP also halts the auto-scheduler
        _sched["enabled"] = False
        _sched_stop.set()


def start(cfg):
    """cfg = {vibe, key, tempo?, roles:{chords|bass|lead|drums: {port, channel, enabled}}}"""
    if mido is None:
        return {"error": "mido not available"}
    stop(disable_schedule=False)          # switching vibes must not kill the auto-scheduler
    _stop.clear()
    global _thread
    _thread = threading.Thread(target=_run, args=(cfg,), daemon=True)
    _thread.start()
    _state.update(playing=True, vibe=cfg.get("vibe"), key=cfg.get("key"))
    return {"ok": True}


def _role(cfg, name):
    r = (cfg.get("roles") or {}).get(name) or {}
    if not r.get("enabled") or not r.get("port"):
        return None
    return {"port": r["port"], "ch": int(r.get("channel", 1)) - 1}


def _send_on(role, note, vel):
    try:
        _get_port(role["port"]).send(mido.Message("note_on", note=int(note), velocity=int(vel), channel=role["ch"]))
    except Exception:
        pass


def _send_off(role, note):
    try:
        _get_port(role["port"]).send(mido.Message("note_off", note=int(note), velocity=0, channel=role["ch"]))
    except Exception:
        pass


def _run(cfg):
    vibe_key = resolve_vibe(cfg.get("vibe"))
    _state["vibe"] = vibe_key
    v = VIBES[vibe_key]
    tempo = float(cfg.get("tempo") or v["tempo"])
    scale = SCALES[v["scale"]]
    root = NOTE_IDX.get(str(cfg.get("key", "C")).upper(), 0)
    vel = v["vel"]
    beat = 60.0 / tempo
    step_dur = beat / 4.0                      # 16th grid
    spc = v["chord_beats"] * 4                 # steps per chord

    chords = _role(cfg, "chords")
    bass = _role(cfg, "bass")
    lead = _role(cfg, "lead")
    drums = _role(cfg, "drums")

    def deg_root(deg):
        return root + scale[deg % 7] + 12 * (1 + (deg // 7))

    active_chord, active_bass, active_lead = [], [], []
    pending_off = []   # (off_time, role, note)

    pi = 0
    while not _stop.is_set():
        deg, qual = v["prog"][pi % len(v["prog"])]
        pi += 1
        ch_root = deg_root(deg)
        _state["chord"] = f"{cfg.get('key','C')} {v['scale']} · deg {deg+1} {qual}"

        # new chord — swap held notes
        if chords:
            for n in active_chord:
                _send_off(chords, n)
            active_chord = [ch_root + 12 * (v["chord_oct"] - 4) + iv for iv in CHORDS.get(qual, [0, 4, 7])]
            for n in active_chord:
                _send_on(chords, n, vel)
        if bass and v["bass_feel"] == "hold":
            for n in active_bass:
                _send_off(bass, n)
            active_bass = [ch_root + 12 * (v["bass_oct"] - 4)]
            for n in active_bass:
                _send_on(bass, n, vel + 6)

        # one sparse lead phrase per chord (probabilistic)
        lead_step = random.randint(0, spc - 1) if (lead and random.random() < v["lead_density"]) else -1

        for step in range(spc):
            if _stop.is_set():
                break
            now = time.time()
            # process scheduled note-offs
            for off in [p for p in pending_off if p[0] <= now]:
                _send_off(off[1], off[2])
            pending_off = [p for p in pending_off if p[0] > now]

            beat_pos = step % 4
            on_beat = (beat_pos == 0)
            on_8th = (step % 2 == 0)

            if bass and v["bass_feel"] == "root8" and on_8th:
                for n in active_bass:
                    _send_off(bass, n)
                active_bass = [ch_root + 12 * (v["bass_oct"] - 4)]
                _send_on(bass, active_bass[0], vel + 8)
                pending_off.append((now + step_dur * 1.6, bass, active_bass[0]))

            if drums and v["drums"]:
                hits = []
                bar_step = step % 16
                if v["drums"] == "boombap":
                    if bar_step in (0, 10):
                        hits.append("kick")
                    if bar_step in (4, 12):
                        hits.append("snare")
                    hits.append("hat")               # running 16th hats (swung by step timing)
                elif v["drums"] == "four":
                    if beat_pos == 0:
                        hits.append("kick")
                    if bar_step in (4, 12):
                        hits.append("snare")
                    hits.append("hat")               # running 16th hats
                    if bar_step % 4 == 2:
                        hits.append("ohat")           # open-hat lift on the offbeat
                elif v["drums"] == "trap":
                    if bar_step in (0, 7, 10):       # syncopated 808 kicks
                        hits.append("kick")
                    if bar_step == 8:                # half-time backbeat (beat 3)
                        hits.append("snare")
                    hits.append("hat")               # 16th hats
                    if random.random() < 0.12:       # occasional roll
                        hits.append("hat")
                for h in hits:
                    dn = DRUM[h]
                    _send_on(drums, dn, vel + (10 if h == "kick" else 0))
                    pending_off.append((now + 0.04, drums, dn))

            if step == lead_step and lead:
                ln = root + random.choice(scale) + 12 * (v["lead_oct"] - 4)
                for n in active_lead:
                    _send_off(lead, n)
                active_lead = [ln]
                _send_on(lead, ln, vel - 6)
                pending_off.append((now + beat * 2, lead, ln))

            # swing the offbeat 16ths
            sw = step_dur * (1 + v["swing"]) if step % 2 == 1 else step_dur * (1 - v["swing"])
            time.sleep(max(0.0, sw))

    # cleanup held notes
    for role, notes in ((chords, active_chord), (bass, active_bass), (lead, active_lead)):
        if role:
            for n in notes:
                _send_off(role, n)
