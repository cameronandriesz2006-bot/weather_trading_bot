"""Replay the negRisk arb scanner log as a P&L: "what if we took only the profitable trades?"

The scanner (`negrisk_arb_scan.py`) writes a `board` row ONLY when a board is net-of-fee
profitable at real book depth. So every row here is already a trade we would have been willing
to take. This tool turns that stream of *observations* into a stream of *executions* and prices
the result, including the capital you would have needed on hand.

The judgment calls that drive the headline, all exposed as flags so an auditor can move them:

  --episode-gap   Consecutive profitable sweeps on one board are ONE standing mispricing, not
                  N of them. Rows are grouped into episodes; a gap longer than this starts a
                  new one. Credit is never taken at the peak — you cannot trade a price you
                  only see in hindsight.
  --credit-row    Which row of an episode the fill is priced at. The 2026-07-27 audit found
                  that crediting the FIRST row (arrival) is zero-latency fiction: the scanner
                  stamps ts AFTER the ~1s book fetch, so the arrival prices are already stale
                  at their own timestamp and no order can reach the book before the next
                  ~2s sweep. Default is therefore 2 — a trade only counts if its price
                  SURVIVED to the following sweep, and it pays that surviving (usually worse)
                  price; shorter-lived episodes are voided as missed. This one flag is a
                  52% haircut on the log audited 2026-07-27. `--credit-row 1 --gas 0`
                  reproduces the original (pre-audit) headline.
  --warmup        Mispricing already standing when the scanner booted is a one-time STOCK, not
                  flow. Episodes starting within this many seconds of t0 are dropped.
  --gas           Per-position on-chain cost, default 0.024 (audit-measured ~$0.019 convert +
                  ~$0.005 redeem). Matters enormously: the median trade nets ~$0.055.

Reproducibility: pass --until so a growing log still yields the same numbers.

    python -m backend.data.negrisk_arb_pnl --until 2026-07-27T05:07:00+00:00
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import re
import statistics
from typing import Dict, List, Optional

LOG = "logs/negrisk_arb.jsonl"
SIDES = ("buy", "short")
MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}


def _ts(s: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(s)


def load(path: str, until: Optional[str]) -> tuple:
    cutoff = _ts(until) if until else None
    boards, sweeps = [], []
    for line in open(path):
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if cutoff and _ts(d["ts"]) > cutoff:
            break
        (boards if d.get("type") == "board" else sweeps).append(d)
    return boards, [s for s in sweeps if s.get("type") == "sweep"]


def resolution_time(slug: str, entry: datetime.datetime) -> datetime.datetime:
    """When capital comes back if we must hold to settlement.

    APPROXIMATION: the market date parsed out of the slug, +26h UTC (covers every station
    timezone's local-day end plus Polymarket's resolution lag). Only affects the
    hold-to-resolution capital column, never the P&L.
    """
    m = re.search(r"on-([a-z]+)-(\d+)-(\d{4})", slug)
    if not m:
        return entry + datetime.timedelta(hours=24)
    day = datetime.datetime(int(m.group(3)), MONTHS[m.group(1)], int(m.group(2)),
                            tzinfo=datetime.timezone.utc)
    return max(day + datetime.timedelta(hours=26), entry + datetime.timedelta(minutes=10))


def executions(boards: List[dict], t0: datetime.datetime, gap_s: float,
               warmup_s: float, gas: float, credit_row: int = 2) -> List[dict]:
    """One execution per standing mispricing.

    An episode shorter than ``credit_row`` rows is a price that vanished before any real order
    could have landed — it is dropped as MISSED, not credited at $0 (no position, no gas).
    Episodes long enough are priced at row ``credit_row``, i.e. at what was still on the book
    one sweep after we first saw it, which is systematically less than arrival on the trades
    that matter (fast competition eats the big ones between sweeps).
    """
    out = []
    for side in SIDES:
        by_slug = collections.defaultdict(list)
        for r in boards:
            if side in r:
                by_slug[r["slug"]].append(r)
        for slug, rows in by_slug.items():
            rows.sort(key=lambda r: r["ts"])
            episodes, cur, last = [], [], None
            for r in rows:
                t = _ts(r["ts"])
                if last and (t - last).total_seconds() > gap_s:
                    episodes.append(cur)
                    cur = []
                cur.append(r)
                last = t
            if cur:
                episodes.append(cur)
            for ep in episodes:
                t_seen = _ts(ep[0]["ts"])
                if (t_seen - t0).total_seconds() <= warmup_s:
                    continue                      # left-censored stock, not flow
                if len(ep) < credit_row:
                    continue                      # gone before an order could land: missed
                fill = ep[credit_row - 1]
                t = _ts(fill["ts"])
                d = fill[side]
                out.append({
                    "ts": t, "slug": slug, "side": side, "cost": d["cost"],
                    "gross_profit": d["profit"], "profit": d["profit"] - gas,
                    "roc": d["roc"], "held": len(ep),
                    "free": resolution_time(slug, t),
                })
    out.sort(key=lambda x: x["ts"])
    return out


def simulate(trades: List[dict], bankroll: float, mode: str) -> tuple:
    """Greedy: take every profitable trade you can afford. Returns (n_taken, net_$)."""
    cash, locked, taken = float(bankroll), [], []
    for x in trades:
        matured = [f for f in locked if f[0] <= x["ts"]]
        cash += sum(f[1] for f in matured)
        locked = [f for f in locked if f[0] > x["ts"]]
        if cash >= x["cost"]:
            cash -= x["cost"]
            taken.append(x)
            until = x["free"] if mode == "hold" else x["ts"] + datetime.timedelta(minutes=10)
            locked.append((until, x["cost"] + x["profit"]))
    return len(taken), sum(y["profit"] for y in taken)


def peak_capital(trades: List[dict], mode: str) -> float:
    events = []
    for x in trades:
        free = x["free"] if mode == "hold" else x["ts"] + datetime.timedelta(minutes=10)
        events += [(x["ts"], x["cost"]), (free, -x["cost"])]
    events.sort()
    cur = peak = 0.0
    for _, delta in events:
        cur += delta
        peak = max(peak, cur)
    return peak


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=LOG)
    ap.add_argument("--until", default=None, help="ISO ts; ignore rows after it (reproducibility)")
    ap.add_argument("--episode-gap", type=float, default=300.0)
    ap.add_argument("--credit-row", type=int, default=2,
                    help="price the fill at this row of each episode; episodes shorter than "
                         "this are voided as missed. 1 = the pre-audit zero-latency number.")
    ap.add_argument("--warmup", type=float, default=120.0)
    ap.add_argument("--gas", type=float, default=0.024, help="per-position on-chain cost, $")
    args = ap.parse_args()

    boards, sweeps = load(args.log, args.until)
    if not sweeps:
        print("no sweeps in log")
        return
    t0, t1 = _ts(sweeps[0]["ts"]), _ts(sweeps[-1]["ts"])
    hours = (t1 - t0).total_seconds() / 3600.0
    per_day = lambda x: x / hours * 24.0

    print(f"log      {args.log}")
    print(f"window   {t0:%Y-%m-%d %H:%M} -> {t1:%Y-%m-%d %H:%M} UTC  ({hours:.2f}h)")
    print(f"rows     {len(sweeps)} sweeps, {len(boards)} profitable-board rows")
    print(f"params   episode-gap={args.episode_gap}s credit-row={args.credit_row} "
          f"warmup={args.warmup}s gas=${args.gas}/position")

    for side in SIDES:
        rs = [r for r in boards if side in r]
        bad = sum(1 for r in rs if r[side]["profit"] <= 0)
        noex = sum(1 for r in rs if not r[side].get("executable"))
        print(f"  {side:>5}: {len(rs)} rows, {bad} non-positive, {noex} below the 5-share minimum")

    trades = executions(boards, t0, args.episode_gap, args.warmup, args.gas, args.credit_row)
    if not trades:
        print("\nno executions")
        return
    net = sum(x["profit"] for x in trades)
    label = "ARRIVAL (zero-latency)" if args.credit_row == 1 else f"row {args.credit_row} (survived a sweep)"
    print(f"\n=== {len(trades)} executions, one per mispricing, credited at {label} ===")
    for side in SIDES:
        ts = [x for x in trades if x["side"] == side]
        if ts:
            s = sum(x["profit"] for x in ts)
            print(f"  {side:>5}: {len(ts):>4} trades  net ${s:8.2f}  = ${per_day(s):7.2f}/day")
    print(f"  {'TOTAL':>5}: {len(trades):>4} trades  net ${net:8.2f}  = ${per_day(net):7.2f}/day"
          f"   ({per_day(len(trades)):.0f} trades/day)")

    p = sorted(x["profit"] for x in trades)
    c = sorted(x["cost"] for x in trades)
    print(f"\n  per-trade profit  median ${statistics.median(p):.3f}  mean ${statistics.mean(p):.3f}"
          f"  max ${p[-1]:.2f}")
    print(f"  per-trade capital median ${statistics.median(c):.2f}  max ${c[-1]:.2f}")
    print(f"  CONCENTRATION  ex-top-1 ${per_day(sum(p[:-1])):.2f}/day"
          f"   ex-top-5 ${per_day(sum(p[:-5])):.2f}/day"
          f"   (top trade = {p[-1] / net * 100:.0f}% of all P&L)")

    print(f"\n  peak simultaneous capital: hold-to-resolution ${peak_capital(trades,'hold'):,.0f}"
          f" | merge-in-10-min ${peak_capital(trades,'merge'):,.0f}")

    print(f"\n=== WHAT BANKROLL BUYS (greedy: take every affordable profitable trade) ===")
    print(f"{'bankroll':>9} | {'HOLD to resolution':^27} | {'MERGE back in 10 min':^27}")
    print(f"{'':>9} | {'trades':>7}{'$/day':>10}{'%/day':>10} | {'trades':>7}{'$/day':>10}{'%/day':>10}")
    print("-" * 71)
    for B in (100, 250, 500, 1000, 2500, 5000, 10000):
        row = f"{'$' + format(B, ','):>9} |"
        for mode in ("hold", "merge"):
            n, s = simulate(trades, B, mode)
            row += f" {n:>7}{per_day(s):>10.2f}{per_day(s) / B * 100:>9.2f}% |"
        print(row)


if __name__ == "__main__":
    main()
