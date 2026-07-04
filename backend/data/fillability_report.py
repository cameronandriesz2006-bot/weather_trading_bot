"""Fillability report — turn the fillability_probe.jsonl time series into the one
number the Edge-2 go/no-go needs: *how often is a contested same-day book actually
inside our gates?*

The backtest placed 206 trades at a flat 2c spread; it never modeled the real book.
The probe (backend/core/weather_signals._log_fillability_probe) records the real-CLOB
spread + liquidity + every regime-scoped gate outcome on the un-railed same-day
residue, at every scan. This script summarizes that:

  (A) how many contested same-day bucket-scans we've logged, by city x action-hour Ha
  (B) BOOK fillability  — spread_ok AND liq_ok AND vol_ok (is there a real book to hit?)
  (C) FULL pass         — all gates incl. edge/gap/freshness (would we actually trade?)
  (D) binding constraint — among non-passing rows, which gate failed, how often
  (E) per contested bucket-day rollup — was each ever fillable / ever tradeable, at which Ha

Usage:
  python -m backend.data.fillability_report [--since 2026-07-04] [--city denver]
                                            [--ha 16] [--window 16 17]
"""
import argparse
import json
import os
import statistics
from collections import Counter, defaultdict

LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "logs", "fillability_probe.jsonl")

# gates that constitute "is there a real book to hit" vs the full tradeable set
BOOK_GATES = ["spread_ok", "liq_ok", "vol_ok"]
ALL_GATES = ["edge_ok", "entry_ok", "spread_ok", "liq_ok", "vol_ok", "gap_ok", "extreme_ok"]


def _load(args):
    rows = []
    if not os.path.exists(LOG):
        return rows
    with open(LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if args.since and r.get("t", "") < args.since:
                continue
            if args.city and r.get("city") != args.city:
                continue
            if args.ha is not None and r.get("ha") != args.ha:
                continue
            if args.window and not (args.window[0] <= r.get("ha", -1) <= args.window[1]):
                continue
            rows.append(r)
    return rows


def _pct(n, d):
    return f"{n/d:>4.0%}" if d else "  --"


def _med(vals):
    return statistics.median(vals) if vals else float("nan")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="ISO timestamp lower bound, e.g. 2026-07-04")
    ap.add_argument("--city")
    ap.add_argument("--ha", type=int, help="only this action-hour")
    ap.add_argument("--window", nargs=2, type=int, metavar=("LO", "HI"),
                    help="action-hour range, e.g. --window 16 17 (the edge lives here)")
    args = ap.parse_args()

    rows = _load(args)
    print(f"log: {LOG}")
    filt = []
    if args.since: filt.append(f"since={args.since}")
    if args.city: filt.append(f"city={args.city}")
    if args.ha is not None: filt.append(f"ha={args.ha}")
    if args.window: filt.append(f"window={args.window[0]}-{args.window[1]}h")
    print(f"filters: {', '.join(filt) or 'none'}")
    print(f"contested same-day bucket-scans: {len(rows)}\n")
    if not rows:
        print("No rows yet — the probe writes only during in-window scans with an "
              "un-railed same-day book. Check back after a trading window.")
        return

    # ---- (A) coverage: rows by city x Ha -------------------------------------
    print("=" * 74)
    print("(A) COVERAGE — contested same-day bucket-scans by city x action-hour")
    print("=" * 74)
    by_city_ha = defaultdict(int)
    cities = sorted({r["city"] for r in rows})
    has = sorted({r["ha"] for r in rows})
    for r in rows:
        by_city_ha[(r["city"], r["ha"])] += 1
    hdr = "  " + f"{'city':10}" + "".join(f"{('H'+str(h)):>6}" for h in has) + f"{'tot':>7}"
    print(hdr)
    for c in cities:
        line = "  " + f"{c:10}" + "".join(f"{by_city_ha.get((c, h), 0):>6}" for h in has)
        print(line + f"{sum(by_city_ha.get((c, h), 0) for h in has):>7}")

    # ---- (B)/(C) fillability by Ha -------------------------------------------
    print("\n" + "=" * 74)
    print("(B/C) FILLABILITY by action-hour  (book = spread+liq+vol; pass = ALL gates)")
    print("=" * 74)
    print(f"  {'Ha':>3}  {'n':>4}  {'book-ok':>8}  {'would-trade':>11}  "
          f"{'med spread':>10}  {'med liq':>8}")
    for h in has:
        sub = [r for r in rows if r["ha"] == h]
        nbook = sum(1 for r in sub if all(r.get(g) for g in BOOK_GATES))
        npass = sum(1 for r in sub if r.get("passes"))
        print(f"  {h:>3}  {len(sub):>4}  {_pct(nbook, len(sub))} {' ':>2}  "
              f"{_pct(npass, len(sub))} {' ':>5}  "
              f"{_med([r['rel_spread'] for r in sub]):>9.0%}  "
              f"${_med([r['liquidity'] for r in sub]):>7.0f}")

    # ---- (D) binding constraint ----------------------------------------------
    print("\n" + "=" * 74)
    print("(D) BINDING CONSTRAINT — among rows that did NOT pass, which gate failed")
    print("    (a row can fail several; each failing gate is counted once)")
    print("=" * 74)
    fails = Counter()
    nonpass = [r for r in rows if not r.get("passes")]
    for r in nonpass:
        for g in ALL_GATES:
            if not r.get(g):
                fails[g] += 1
    if not nonpass:
        print("  (every logged row passed)")
    for g, n in fails.most_common():
        print(f"  {g:12} {n:>5}  ({_pct(n, len(nonpass))} of {len(nonpass)} non-passing)")

    # ---- (E) per contested bucket-day rollup ---------------------------------
    print("\n" + "=" * 74)
    print("(E) PER CONTESTED BUCKET-DAY — was it EVER fillable / EVER tradeable")
    print("=" * 74)
    days = defaultdict(list)
    for r in rows:
        days[(r["date"], r["city"], r["metric"], r["bucket"])].append(r)
    print(f"  {'date':10} {'city':9} {'bucket':14} {'scans':>5}  {'minspread':>9}  "
          f"{'book@Ha':>8}  {'trade@Ha':>9}")
    for key in sorted(days):
        d, c, met, bk = key
        rs = days[key]
        minspread = min(r["rel_spread"] for r in rs)
        book_ha = sorted({r["ha"] for r in rs if all(r.get(g) for g in BOOK_GATES)})
        pass_ha = sorted({r["ha"] for r in rs if r.get("passes")})
        book_s = ",".join(f"H{h}" for h in book_ha) or "never"
        pass_s = ",".join(f"H{h}" for h in pass_ha) or "never"
        print(f"  {d:10} {c:9} {bk[:14]:14} {len(rs):>5}  {minspread:>8.0%}  "
              f"{book_s:>8}  {pass_s:>9}")

    # ---- headline ------------------------------------------------------------
    nbook = sum(1 for r in rows if all(r.get(g) for g in BOOK_GATES))
    npass = sum(1 for r in rows if r.get("passes"))
    print("\n" + "=" * 74)
    print(f"HEADLINE: of {len(rows)} contested same-day bucket-scans, "
          f"{_pct(nbook, len(rows)).strip()} had a real book inside the gates, "
          f"{_pct(npass, len(rows)).strip()} would have traded.")
    print("=" * 74)


if __name__ == "__main__":
    main()
