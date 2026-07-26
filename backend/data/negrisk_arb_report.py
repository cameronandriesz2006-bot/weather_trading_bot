"""Phase-B1 analysis: how much negRisk arb is actually capturable, and how fast must we be?

Reads the JSONL that ``negrisk_arb_scan.py`` appends each sweep and answers the two questions
that decide Plan B — neither of which a single snapshot can answer:

  1. IS IT CAPTURABLE?  A board quoting a $0.50 edge for six straight hours is $0.50 of total
     profit, not $0.50 per sweep. So we group consecutive positive sweeps on the same board
     into WINDOWS and count each window once. That is the honest daily number.

  2. DOES SPEED PAY?  For each window we know how long it survived. If we react L seconds after
     a window opens we capture only windows that outlive L. Sweeping that L gives a
     profit-vs-latency curve — which is precisely the "would a faster VPS pay for itself"
     question, answered with measurements instead of intuition.

A window is closed when a board goes a full sweep with no positive executable edge. Because
sweeps are discrete, a window seen once is reported with the sweep interval as its duration —
an upper bound on something that may have been far more fleeting, so treat single-sweep
windows as the optimistic case, never the expected one.

Run:  PYTHONPATH=. venv/bin/python -m backend.data.negrisk_arb_report
"""
import argparse
import json
from collections import defaultdict
from datetime import datetime

LOG_PATH = "logs/negrisk_arb.jsonl"


def load(path, executable_only=True, direction=None):
    """Return (board_rows, sweep_timestamps).

    The scanner logs a ``sweep`` row every pass and a ``board`` row only on profit, so the
    sweep rows are what let us say "no edge existed at 14:03" instead of merely "we have no
    record of 14:03". Older logs predate the type tag; those rows are all boards.
    """
    rows, sweeps = [], []
    with open(path) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("type") == "sweep":
                sweeps.append(r["ts"])
                continue
            best, bdir = 0.0, None
            for d in ("buy", "short"):
                if direction and d != direction:
                    continue
                b = r.get(d)
                if not b:
                    continue
                if executable_only and not b.get("executable"):
                    continue
                if b.get("profit", 0) > best:
                    best, bdir = b["profit"], d
            r["_best"] = best
            r["_dir"] = bdir
            r["_t"] = datetime.fromisoformat(r["ts"])
            rows.append(r)
    rows.sort(key=lambda r: r["_t"])
    return rows, sorted(set(sweeps))


def build_windows(rows, sweep_ts=None, gap_tolerance=1):
    """Group consecutive positive sweeps per board into windows.

    ``gap_tolerance`` sweeps of no-edge are allowed inside a window before it is closed, so a
    single missed/again-quoted sweep doesn't shatter one real window into three.
    """
    sweeps = sorted(set(sweep_ts) | {r["ts"] for r in rows}) if sweep_ts \
        else sorted({r["ts"] for r in rows})
    idx = {ts: i for i, ts in enumerate(sweeps)}
    byboard = defaultdict(list)
    for r in rows:
        byboard[r["slug"]].append(r)

    windows = []
    for slug, rs in byboard.items():
        cur = None
        for r in sorted(rs, key=lambda x: x["_t"]):
            i = idx[r["ts"]]
            if r["_best"] <= 0:
                continue
            if cur and i - cur["last_i"] <= gap_tolerance + 1:
                cur["last_i"] = i
                cur["end"] = r["_t"]
                cur["samples"] += 1
                if r["_best"] > cur["peak"]:
                    # Take peak AND its capital from the SAME sweep. Tracking them with
                    # independent max() produced a per-window ROC that was never observed —
                    # the best profit divided by the worst capital, from different instants.
                    cur["peak"] = r["_best"]
                    cur["cost"] = r.get(r["_dir"], {}).get("cost", 0)
            else:
                if cur:
                    windows.append(cur)
                cur = {"slug": slug, "dir": r["_dir"], "start": r["_t"], "end": r["_t"],
                       "last_i": i, "peak": r["_best"], "samples": 1,
                       # `first` is what a trader who reacted on the opening sweep would
                       # actually have got. The peak is what the old report credited, which
                       # nobody can capture — you cannot see the future of a window.
                       "first": r["_best"], "first_i": i,
                       "cost": r.get(r["_dir"], {}).get("cost", 0)}
        if cur:
            windows.append(cur)
    return windows, len(sweeps)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--path", default=LOG_PATH)
    ap.add_argument("--interval", type=float, default=0,
                    help="sweep interval seconds; 0 = MEASURE it from the log's own sweep "
                         "timestamps. The old default of 2.0 was the requested cadence, not the "
                         "achieved one — the loop actually held ~3.1-3.7s because a sweep takes "
                         "longer than its own interval, which misstated every window duration "
                         "and the low-latency end of the curve.")
    ap.add_argument("--include-sub-min", action="store_true",
                    help="also count boards below the 5-share minimum order size")
    args = ap.parse_args()

    rows, sweep_ts = load(args.path, executable_only=not args.include_sub_min)
    if not args.interval:
        ts = sorted(datetime.fromisoformat(t) for t in sweep_ts)
        d = sorted((ts[i + 1] - ts[i]).total_seconds() for i in range(len(ts) - 1))
        args.interval = d[len(d) // 2] if d else 2.0
        print(f"measured sweep interval: {args.interval:.2f}s "
              f"(min {d[0]:.2f} / max {d[-1]:.2f})" if d else "")
    if not rows:
        print(f"{len(sweep_ts)} sweeps logged, NO profitable board ever seen.")
        return
    windows, n_sweeps = build_windows(rows, sweep_ts)
    # Span the whole watch period, not just first->last hit: a $/hour rate divided by the
    # gap between two hits is a divide-by-tiny artefact, not a finding.
    all_t = [datetime.fromisoformat(t) for t in sweep_ts] + [r["_t"] for r in rows]
    span_h = (max(all_t) - min(all_t)).total_seconds() / 3600.0
    print(f"log span {span_h:.2f}h over {n_sweeps} sweeps, {len(rows)} board-observations")
    if span_h < 0.5:
        print("  !! under 30 min of data — every $/day figure below is extrapolation noise.")
    print(f"boards ever showing an executable edge: "
          f"{len({w['slug'] for w in windows})}")
    print(f"distinct opportunity windows: {len(windows)}")
    if not windows:
        print("\nNO executable windows. Plan B is a NO on this data.")
        return

    # ---- the four corrections, applied in order, each shown so the attrition is visible ----
    # 1. LEFT CENSORING. A window already live in the very first sweep is standing STOCK that
    #    existed before we started watching, not flow that arrived during the observation.
    #    Counting it puts a fixed number over a growing denominator, so $/day decays as 1/t —
    #    the original report fell from $6,720/day at 2min to $1,909/day at 27min purely from
    #    this. Also drop right-censored windows still open at the log's end, for symmetry.
    last_i = n_sweeps - 1
    born = [w for w in windows if w.get("first_i", 0) > 0]
    settled = [w for w in born if w["last_i"] < last_i]
    total_peak = sum(w["peak"] for w in windows)
    print(f"\n--- corrections (each line is the previous line with one more fix) ---")
    print(f"  0. as originally reported (peak-credit, all windows)   ${total_peak:>8.2f}"
          f"  ~ ${total_peak/span_h*24 if span_h else 0:>9.2f}/day")
    b = sum(w["peak"] for w in born)
    print(f"  1. + drop stock already live at the first sweep        ${b:>8.2f}"
          f"  ~ ${b/span_h*24 if span_h else 0:>9.2f}/day")
    s = sum(w["peak"] for w in settled)
    print(f"  2. + drop windows still open at the log's end          ${s:>8.2f}"
          f"  ~ ${s/span_h*24 if span_h else 0:>9.2f}/day")
    # 3. ARRIVAL CREDIT. A trader reacting to a window captures what is on the book when they
    #    arrive, not the window's later peak. Peak-crediting overstated this by 1.69x measured.
    a = sum(w["first"] for w in settled)
    print(f"  3. + credit at ARRIVAL value, not window peak          ${a:>8.2f}"
          f"  ~ ${a/span_h*24 if span_h else 0:>9.2f}/day")
    # 4. ONE EXECUTION PER BOARD. The same board blinking in and out opens a new window each
    #    time and was re-credited at each new peak. Each re-execution needs FRESH capital, so
    #    they are not additive on any real balance sheet.
    byslug = {}
    for w in settled:
        byslug[w["slug"]] = max(byslug.get(w["slug"], 0.0), w["first"])
    o = sum(byslug.values())
    print(f"  4. + count each BOARD once, not each blink             ${o:>8.2f}"
          f"  ~ ${o/span_h*24 if span_h else 0:>9.2f}/day   <== headline")

    total, per_h = o, (o / span_h if span_h > 0 else 0)
    print(f"\ncorrected capturable profit: ${total:.2f}  =>  ${per_h:.2f}/hour ~ ${per_h*24:.2f}/day")
    print(f"  windows: {len(windows)} raw -> {len(born)} born in-window -> {len(settled)} "
          f"fully observed -> {len(byslug)} distinct boards")

    # PORTFOLIO capital, not per-board. "Peak capital for a single board" was the wrong
    # statistic: what caps the business is how much money the whole book will absorb at one
    # instant, and every tranche stays locked until that board settles.
    bysweep = defaultdict(float)
    for r in rows:
        if r["_best"] > 0:
            bysweep[r["ts"]] += r.get(r["_dir"], {}).get("cost", 0)
    if bysweep:
        caps = sorted(bysweep.values())
        print(f"  simultaneous capital across all open opportunities: "
              f"median ${caps[len(caps)//2]:,.0f}  peak ${caps[-1]:,.0f}")
    print(f"  peak capital for a single board: ${max((w['cost'] for w in windows), default=0):,.2f}")
    fees = sum(r.get(r["_dir"], {}).get("fee", 0) for r in rows if r["_best"] > 0)
    gross = sum(r.get(r["_dir"], {}).get("gross", 0) for r in rows if r["_best"] > 0)
    if gross:
        print(f"  (net-positive observations paid ${fees:.2f} of fee on ${gross:.2f} gross)")
    windows = settled or windows

    print(f"\n--- window durations (how long an edge survives) ---")
    for w in windows:
        w["dur_s"] = max((w["end"] - w["start"]).total_seconds(), args.interval)
    durs = sorted(w["dur_s"] for w in windows)
    def pct(p):
        return durs[min(int(len(durs) * p), len(durs) - 1)]
    print(f"  median {pct(0.5)/60:.1f} min | p25 {pct(0.25)/60:.1f} | p75 {pct(0.75)/60:.1f} "
          f"| max {durs[-1]/60:.1f} min")
    one = sum(1 for d in durs if d <= args.interval)
    print(f"  single-sweep (<= {args.interval}s, may be far briefer): {one}/{len(durs)}")

    print(f"\n--- PROFIT vs REACTION LATENCY (the 'is a faster box worth it' curve) ---")
    print(f"  {'latency':>10} {'windows':>8} {'$ captured':>11} {'$/day':>9}  {'% of max':>8}")
    # Credit ARRIVAL value, and collapse to one execution per board, at every latency — so the
    # curve is the same quantity as the headline, just gated on surviving L seconds. The old
    # curve summed window PEAKS, which both overstated capture (1.69x) and made the shape lie
    # about the value of speed: the overstatement shrank as L grew, flattering low latency.
    base = None
    for lat in (0, 30, 60, 120, 300, 600, 1800, 3600):
        got = [w for w in windows if w["dur_s"] > lat]
        per_board = {}
        for w in got:
            per_board[w["slug"]] = max(per_board.get(w["slug"], 0.0), w["first"])
        s = sum(per_board.values())
        if base is None:
            base = s
        d = s / span_h * 24 if span_h > 0 else 0
        print(f"  {lat:>8}s {len(per_board):>8} {s:>11.2f} {d:>9.2f}  "
              f"{(s/base*100 if base else 0):>7.1f}%")

    print(f"\n--- top windows ---")
    for w in sorted(windows, key=lambda x: -x["peak"])[:12]:
        print(f"  ${w['peak']:>6.2f} on ${w['cost']:>8.2f} ({w['peak']/w['cost']*100 if w['cost'] else 0:>5.2f}% ROC) "
              f"{w['dur_s']/60:>6.1f}min {w['dir']:>5}  {w['slug'][:48]}")


if __name__ == "__main__":
    main()
