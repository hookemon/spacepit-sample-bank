#!/usr/bin/env python3
"""Measure real-world MIDI clock jitter.

Streams MIDI clock to a port at a target BPM for N seconds and records the
delta between successive ticks. Reports the actual jitter the synth sees.

Run while no other clock is streaming. Doesn't connect to the dashboard's
clock thread — runs its own isolated test.

Usage:
    python measure-clock-jitter.py --bpm 120 --duration 30 --port "Moog Grandmother"
    python measure-clock-jitter.py --bpm 88 --duration 60  # quick check

What "good" looks like:
    - mean tick interval matches the target (60/bpm/24) to within 0.1ms
    - max jitter under 2-3ms (you won't hear that against a synth's analog VCA)
    - 95th percentile under 1ms

What "bad" looks like:
    - max jitter > 10ms (audible swing/sloppy feel)
    - drift between measured mean and target (clock is slowly wrong)
    - stddev > 1ms (loose, jittery feel under tight programming)
"""
import argparse
import statistics
import sys
import time

import mido


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bpm", type=float, default=120.0, help="target BPM")
    ap.add_argument("--duration", type=float, default=30.0, help="how long to measure (sec)")
    ap.add_argument("--port", default="Moog Grandmother", help="MIDI port name (substring match)")
    ap.add_argument("--no-send", action="store_true", help="run the timing loop but don't actually send MIDI (pure CPU jitter test)")
    args = ap.parse_args()

    interval = 60.0 / args.bpm / 24.0
    expected_ticks = int(args.duration / interval)
    print(f"MIDI clock jitter test")
    print(f"  bpm:               {args.bpm}")
    print(f"  target interval:   {interval*1000:.4f} ms ({1/interval:.2f} ticks/sec)")
    print(f"  duration:          {args.duration} sec ({expected_ticks} ticks expected)")
    print(f"  port:              {args.port if not args.no_send else '(no-send mode)'}")
    print()

    # open port
    port = None
    if not args.no_send:
        ports = mido.get_output_names()
        matches = [p for p in ports if args.port.lower() in p.lower()]
        if not matches:
            print(f"ERROR: no MIDI port matching '{args.port}'")
            print(f"available: {ports}")
            sys.exit(1)
        port = mido.open_output(matches[0])
        port.send(mido.Message('start'))

    clock_msg = mido.Message('clock')
    deltas = []  # tick-to-tick wall-clock deltas in seconds
    t_last = None
    t_start = time.time()
    t_anchor = t_start
    i = 0
    print("running… (press Ctrl-C to stop early)", flush=True)
    try:
        while time.time() - t_start < args.duration:
            t_next = t_anchor + i * interval
            # Hybrid sleep+spin (matches server.py _clock_worker for like-for-like measurement)
            while True:
                remaining = t_next - time.time()
                if remaining <= 0:
                    break
                if remaining > 0.005:
                    time.sleep(remaining - 0.003)  # leave 3ms for spin buffer
                # else: tight spin for the last 5ms (Python sleep overshoots up to 5ms on macOS)
            if not args.no_send:
                port.send(clock_msg)
            actual_t = time.time()
            if t_last is not None:
                deltas.append(actual_t - t_last)
            t_last = actual_t
            i += 1
    except KeyboardInterrupt:
        print("interrupted")
    finally:
        if port is not None:
            port.send(mido.Message('stop'))
            port.close()

    if not deltas:
        print("no data — ran too short")
        sys.exit(1)

    deltas_ms = [d * 1000 for d in deltas]
    target_ms = interval * 1000

    mean = statistics.mean(deltas_ms)
    median = statistics.median(deltas_ms)
    stdev = statistics.stdev(deltas_ms) if len(deltas_ms) > 1 else 0.0
    max_d = max(deltas_ms)
    min_d = min(deltas_ms)
    sorted_d = sorted(deltas_ms)
    p95 = sorted_d[int(len(sorted_d) * 0.95)]
    p99 = sorted_d[int(len(sorted_d) * 0.99)]

    # how does mean compare to target
    drift_ms = mean - target_ms
    drift_pct = (drift_ms / target_ms) * 100

    print()
    print(f"=== RESULTS ===")
    print(f"  ticks sent:        {len(deltas) + 1}")
    print(f"  target interval:   {target_ms:.4f} ms")
    print(f"  measured mean:     {mean:.4f} ms  ({'+' if drift_ms >= 0 else ''}{drift_ms:.4f} ms drift, {drift_pct:+.3f}%)")
    print(f"  measured median:   {median:.4f} ms")
    print(f"  std dev:           {stdev:.4f} ms")
    print(f"  min / max:         {min_d:.4f} / {max_d:.4f} ms")
    print(f"  95th percentile:   {p95:.4f} ms  (almost all ticks are tighter than this)")
    print(f"  99th percentile:   {p99:.4f} ms")
    print()
    # Verdict based on what actually matters musically:
    #   - drift_pct (does the clock stay on grid over the long run?)
    #   - stdev (typical jitter — what 95% of ticks feel like)
    #   - p99 - target (worst-case spikes that aren't outliers)
    # We deliberately don't use max because macOS scheduler hiccups produce isolated
    # outliers that don't reflect playback feel.
    p99_excess = p99 - target_ms
    if stdev < 1.0 and p99_excess < 3 and abs(drift_pct) < 0.05:
        print("VERDICT: ✓ ROCK SOLID — loops will lock to click cleanly (typical jitter < 1ms)")
    elif stdev < 2.0 and p99_excess < 6:
        print("VERDICT: ◐ GOOD — minor jitter, won't be audibly noticeable on analog gear")
    else:
        print("VERDICT: ⚠ JITTERY — may hear timing slop on tight programming")
    # Quick reading guide
    print()
    print("Reading the numbers:")
    print(f"  · drift {drift_pct:+.4f}% means the clock {'speeds up' if drift_pct > 0 else 'slows down'}"
          f" by ~{abs(drift_pct)*60*60:.1f} sec per hour of playback")
    if stdev < 1.0:
        print("  · stdev under 1ms = tight feel, synth arps lock cleanly")
    elif stdev < 2.0:
        print("  · stdev under 2ms = analog synths won't care; tight sequencers might")
    else:
        print("  · stdev over 2ms = audible looseness")


if __name__ == "__main__":
    main()
