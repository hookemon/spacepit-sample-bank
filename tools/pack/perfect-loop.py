#!/usr/bin/env python3
"""Turn a recording into a PERFECT loop: start dead on the first transient, then trim to EXACTLY
N bars at the given BPM (pure math) — so it drops onto the DAW grid with no dead air and loops.

You tell it the BPM + bars (you know them — you recorded at that tempo). It finds the downbeat
and does the arithmetic: samples = bars * (60/bpm) * beats_per_bar * sample_rate.

  python perfect-loop.py --wav take.wav --bpm 120 --bars 4

Tip: leave a beat or two of tail past the last bar when you record — that gives a seamless
sample-continuous wrap (the loop's start is blended from the audio just past the loop point).
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import soundfile as sf


def find_transient(mono: np.ndarray, sr: int, thresh_db: float = -40.0) -> int:
    """First sample above the gate (relative to peak) = the downbeat. Snapped back to the nearest
    zero-crossing within ~3ms so the loop starts clean, with no leading silence and no edge click."""
    a = np.abs(mono)
    pk = float(a.max()) or 1.0
    thr = pk * (10.0 ** (thresh_db / 20.0))
    idx = np.where(a > thr)[0]
    if len(idx) == 0:
        return 0
    onset = int(idx[0])
    lo = max(1, onset - int(0.003 * sr))
    for j in range(onset, lo, -1):
        if (mono[j - 1] <= 0 < mono[j]) or (mono[j - 1] >= 0 > mono[j]):
            return j
    return onset


def make_loop(audio: np.ndarray, sr: int, bpm: float, bars: int, meter: int = 4, xfade_ms: float = 14.0):
    """Returns (loop, onset, loop_len). Transient-aligned, EXACTLY bars long, seam-wrapped."""
    mono = audio.mean(axis=1) if audio.ndim > 1 else audio
    onset = find_transient(mono, sr)
    loop_len = int(round(bars * (60.0 / bpm) * meter * sr))   # the whole point: exact bar math
    end = onset + loop_len
    if end > len(audio):
        short = (end - len(audio)) / sr
        raise ValueError(f"recording is {short:.2f}s too short — need {loop_len/sr:.3f}s of audio "
                         f"from the transient. Record a little more.")
    loop = audio[onset:end].astype(np.float64).copy()
    # SEAMLESS WRAP — done on the TAIL, never the head, so the downbeat transient at loop[0] stays
    # PRISTINE. (The old code blended the head with the audio just PAST the loop point, which replaced
    # the first ~14ms of the downbeat with the last chord's decay — the loop no longer "started on the
    # transient," it started on the tail. That was the muddy start.) Instead we fade the loop's last X
    # samples toward the audio just BEFORE the onset (the pre-roll), so the final sample lands where the
    # downbeat naturally begins: loop[-1] → loop[0] is continuous exactly like the take's onset-1 → onset.
    # Pre-roll is ~silence (the sound started from silence), so the tail fades out into the downbeat the
    # same way the take began — no seam click, attack 100% intact.
    X = int(min(round(xfade_ms / 1000 * sr), loop_len // 8))
    wrapped = False
    if X > 8:
        ch = loop.shape[1] if loop.ndim > 1 else 1
        avail = min(X, onset)
        pre = audio[onset - avail:onset].astype(np.float64) if avail > 0 else np.zeros((0, ch))
        if pre.ndim == 1:
            pre = pre[:, None]
        if avail < X:                                   # not enough lead-in → pad with silence
            pre = np.vstack([np.zeros((X - avail, ch)), pre])
        fade = np.linspace(0.0, 1.0, X)[:, None]        # 0 at start of the tail region → 1 at the very end
        loop[-X:] = loop[-X:] * (1.0 - fade) + pre * fade
        wrapped = True
    pk = float(np.max(np.abs(loop)))
    if pk > 0.999:
        loop *= 0.999 / pk
    return loop.astype(np.float32), onset, loop_len, wrapped


def _has_acid_chunk(raw: bytearray) -> bool:
    """Scan the RIFF chunk list for an existing 'acid' chunk (so we never double-add)."""
    import struct
    pos = 12
    while pos + 8 <= len(raw):
        if raw[pos:pos + 4] == b"acid":
            return True
        sz = struct.unpack("<I", raw[pos + 4:pos + 8])[0]
        pos += 8 + sz + (sz & 1)        # chunks are word-aligned (odd sizes get a pad byte)
    return False


def write_acid_chunk(path, tempo: float, beats: int, meter_num: int = 4, meter_denom: int = 4) -> bool:
    """Embed an ACID chunk so Ableton (and most DAWs) AUTO-WARP the loop to `tempo` on import — drop it
    in and it's already on grid, no manual Warp. Marks the file as a tempo-locked loop of `beats` beats
    in meter_num/meter_denom. Non-destructive (audio untouched), and a safe no-op if a DAW ignores it
    (it just falls back to manual warp). Returns True if the chunk was added."""
    import struct
    from pathlib import Path as _P
    p = _P(path)
    raw = bytearray(p.read_bytes())
    if len(raw) < 12 or raw[0:4] != b"RIFF" or raw[8:12] != b"WAVE":
        return False
    if _has_acid_chunk(raw):
        return False
    flags = 0x00                        # NOT one-shot → the DAW treats it as a stretchable tempo loop
    body = struct.pack("<IHHfIHHf", flags, 60, 0x8000, 0.0,
                       int(beats), int(meter_denom), int(meter_num), float(tempo))
    acid = b"acid" + struct.pack("<I", len(body)) + body   # 8-byte header + 24-byte body
    if len(raw) % 2:                    # word-align before appending a chunk
        raw += b"\x00"
    raw += acid
    raw[4:8] = struct.pack("<I", len(raw) - 8)             # RIFF size now covers the acid chunk
    p.write_bytes(raw)
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wav", required=True, help="the recording to loop")
    ap.add_argument("--bpm", type=float, required=True, help="the tempo you recorded at")
    ap.add_argument("--bars", type=int, default=4)
    ap.add_argument("--meter", type=int, default=4, help="beats per bar (4 = 4/4)")
    ap.add_argument("--xfade-ms", type=float, default=14.0, help="seam wrap length (needs tail)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    wav = Path(args.wav).expanduser()
    if not wav.exists():
        print(f"✗ not found: {wav}"); sys.exit(1)
    audio, sr = sf.read(str(wav), always_2d=True)
    try:
        loop, onset, loop_len, wrapped = make_loop(audio, sr, args.bpm, args.bars, args.meter, args.xfade_ms)
    except ValueError as e:
        print(f"✗ {e}"); sys.exit(2)

    out = Path(args.out).expanduser() if args.out else wav.with_name(
        f"{wav.stem}_loop_{int(args.bpm)}bpm_{args.bars}bar.wav")
    sf.write(str(out), loop, sr, subtype=sf.info(str(wav)).subtype)
    acidized = write_acid_chunk(out, args.bpm, args.bars * args.meter)
    print(f"✓ {out.name}")
    print(f"  transient: trimmed {onset/sr*1000:.0f}ms of lead-in — loop starts ON the downbeat")
    print(f"  length:    exactly {args.bars} bars @ {args.bpm}bpm = {loop_len/sr:.3f}s = {loop_len} samples")
    print(f"  wrap:      {'seamless (blended from tail)' if wrapped else 'no tail — bar-exact but record a beat extra for a seamless seam'}")
    print(f"  warp:      {('ACID tempo embedded — Ableton auto-warps to ' + format(args.bpm, 'g') + ' BPM on import') if acidized else 'no tempo tag'}")


if __name__ == "__main__":
    main()
