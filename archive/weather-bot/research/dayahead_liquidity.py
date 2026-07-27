"""Stream 1 — can we ACTUALLY place day-ahead bets, and at what size?

"Lean into next-day" looks best vs the market (calibration Test 3) precisely because a
day out the market is a SOFTER opponent — few traders have weighed in. The flip side of
that same coin is THIN BOOKS: the edge and the illiquidity are the same fact. So before
we lean into next-day we have to know whether the edge is harvestable or evaporates the
moment we try to fill real size.

This probe answers it from data we DO have access to:
  (A) LIVE BOOKS (the precise, current measurement). For every open weather bucket in our
      keeper cities, pull the real CLOB ask ladder and WALK it (the same code the live bot
      uses to fill) to find how much USDC we could deploy before the VWAP slips more than a
      given amount past the mid. Bucketed by lead time (same-day / next-day / further), so we
      see the day-ahead slate specifically.
  (B) HISTORICAL TRADES (corroboration, separate module dayahead_activity.py) — how much
      actually traded in the 24h-before-settlement window on resolved markets.

It is quota-SAFE: only Polymarket APIs (Gamma + CLOB), zero Open-Meteo calls, so it can't
starve the live bot's forecast budget.

Caveats it respects:
  - One pull is a snapshot of TODAY's slate (a handful of idiosyncratic markets). It's a
    directional read; a robust number wants sampling over several days (re-run + accumulate).
  - It measures CAPACITY (how much size fits), NOT edge (whether there's an edge there).
  - Executed depth is live-only; this measures the book as it stands now, which is the right
    regime for a forward go-live decision.

Run:  PYTHONPATH=. venv/bin/python -m backend.data.dayahead_liquidity
      ... --cities chicago,nyc,hong_kong,denver,tokyo   --slips 0.005,0.01,0.02
"""
import argparse
import asyncio
import sqlite3
from collections import defaultdict
from datetime import date
from typing import Dict, List, Optional, Tuple

import httpx

from backend.config import settings
from backend.data.orderbook import fetch_books, LiveBook
from backend.data.weather import station_local_now
from backend.data.weather_markets import fetch_polymarket_weather_markets, WeatherMarket

# The cities the calibration work flagged as keep/keepers (Chicago/NYC proven, HK best live
# evidence, Denver/Tokyo parity). Everything else in WEATHER_CITIES is shown too but tagged.
KEEPERS = {"chicago", "nyc", "hong_kong", "denver", "tokyo"}


def _live_bankroll() -> float:
    try:
        c = sqlite3.connect(settings.DATABASE_URL.replace("sqlite:///", "").replace("./", ""))
        row = c.execute("SELECT bankroll FROM bot_state").fetchone()
        c.close()
        if row and row[0]:
            return float(row[0])
    except Exception:
        pass
    return settings.INITIAL_BANKROLL


def max_deployable(asks: List[Tuple[float, float]], mid: float, slip_cap: float) -> float:
    """Max USDC we can spend walking ``asks`` (cheapest-first) while the running VWAP stays
    within ``slip_cap`` (price units) of ``mid``. The walk-the-book answer to 'how much fits
    before slippage bites', not a depth heuristic. VWAP rises monotonically as we go deeper,
    so we take whole levels while the average stays under the cap, then a single partial fill
    at the boundary (capped by that level's available size)."""
    if not asks or mid <= 0:
        return 0.0
    limit = mid + slip_cap
    spent = 0.0
    contracts = 0.0
    for price, size in asks:
        if price <= limit:
            # prev avg <= limit and price <= limit -> whole level keeps avg <= limit
            spent += price * size
            contracts += size
            continue
        headroom = limit * contracts - spent     # "limit-units" of slack we can still spend
        if headroom <= 0:
            break
        q_unbounded = headroom / (price - limit)  # contracts that bring avg exactly to limit
        if q_unbounded >= size:
            # even the whole (pricier) level keeps avg <= limit; take it and continue
            spent += price * size
            contracts += size
            continue
        spent += q_unbounded * price              # partial fill to the boundary, then stop
        break
    return spent


def _lead_days(m: WeatherMarket) -> int:
    """Days from the station-local TODAY to the market's settlement day (0 = in progress)."""
    return (m.target_date - station_local_now(m.city_key).date()).days


def _lead_label(d: int) -> str:
    return {0: "same-day(0)", 1: "next-day(1)", 2: "+2d"}.get(d, f"+{d}d" if d >= 2 else f"{d}d")


async def probe(cities: List[str], slips: List[float]) -> None:
    bankroll = _live_bankroll()
    max_bet = settings.KELLY_MAX_TRADE_FRACTION * bankroll
    print(f"bankroll=${bankroll:,.0f}  max single bet (2.5%)=${max_bet:,.0f}  "
          f"book-fraction cap={settings.WEATHER_MAX_BOOK_FRACTION:.0%}  "
          f"gates: liq>=${settings.WEATHER_MIN_LIQUIDITY:.0f} vol>=${settings.WEATHER_MIN_VOLUME:.0f} "
          f"rel_spread<={settings.WEATHER_MAX_REL_SPREAD:.0%}")
    print(f"cities={cities}\nslippage caps (price units) = {slips}\n")

    markets = await fetch_polymarket_weather_markets(cities)
    if not markets:
        print("no open weather markets returned (Gamma) — nothing to probe")
        return

    # one batched book fetch for every YES+NO token across the whole slate
    tokens = []
    for m in markets:
        tokens += [t for t in (m.token_id_yes, m.token_id_no) if t]
    async with httpx.AsyncClient(timeout=30.0) as client:
        books: Dict[str, LiveBook] = await fetch_books(tokens, client)
    print(f"{len(markets)} open buckets across {len(set((m.city_key,m.target_date,m.metric) for m in markets))} "
          f"events; fetched live books for {len(books)}/{len(tokens)} tokens\n")

    headline = slips[len(slips) // 2]  # middle cap as the headline (default 0.01 = 1c)
    rows = []
    for m in markets:
        yb = books.get(m.token_id_yes or "")
        if yb is None or yb.top.best_ask is None:
            continue
        mid = yb.top.mid
        deploy = {s: max_deployable(yb.asks, mid, s) for s in slips}
        depth = sum(p * sz for p, sz in yb.asks)
        passes = (m.liquidity >= settings.WEATHER_MIN_LIQUIDITY
                  and m.volume >= settings.WEATHER_MIN_VOLUME
                  and (yb.top.best_ask - (yb.top.best_bid or yb.top.best_ask)) / max(mid, 1e-6)
                  <= settings.WEATHER_MAX_REL_SPREAD)
        rows.append({
            "city": m.city_key, "lead": _lead_days(m), "metric": m.metric,
            "date": m.target_date.isoformat(), "bucket": m.bucket_label, "mid": mid,
            "best_ask": yb.top.best_ask, "depth": depth, "deploy": deploy,
            "liq": m.liquidity, "vol": m.volume, "passes_gates": passes,
            "keeper": m.city_key in KEEPERS,
        })

    if not rows:
        print("no buckets with a readable YES book")
        return

    # ---- per-lead summary (the headline: is the day-ahead slate fillable?) -------------
    print("=" * 92)
    print(f"DEPLOYABLE CAPITAL by lead time  (YES side; deploy@{headline:.3g} = max $ before VWAP "
          f"slips {headline:.3g} past mid)")
    print("=" * 92)
    print(f"  {'lead':12} {'buckets':>7} {'passgate':>8} {'med deploy':>11} {'tot deploy':>11} "
          f"{'med depth':>10} {'fills $'+f'{max_bet:.0f}?':>9}")
    by_lead = defaultdict(list)
    for r in rows:
        by_lead[r["lead"]].append(r)
    for lead in sorted(by_lead):
        sub = by_lead[lead]
        dep = sorted(r["deploy"][headline] for r in sub)
        med = dep[len(dep) // 2]
        tot = sum(d for d in dep)
        depth_med = sorted(r["depth"] for r in sub)[len(sub) // 2]
        npass = sum(r["passes_gates"] for r in sub)
        fills = sum(1 for r in sub if r["deploy"][headline] >= max_bet)
        print(f"  {_lead_label(lead):12} {len(sub):>7} {npass:>8} ${med:>10,.0f} ${tot:>10,.0f} "
              f"${depth_med:>9,.0f} {fills:>4}/{len(sub):<4}")

    # ---- keepers, next-day only: the slate we'd actually trade ------------------------
    nd_keepers = [r for r in rows if r["lead"] == 1 and r["keeper"]]
    print("\n" + "=" * 92)
    print(f"NEXT-DAY, KEEPER CITIES ONLY — the day-ahead book we'd actually lean into "
          f"(n={len(nd_keepers)})")
    print("=" * 92)
    if nd_keepers:
        slipcols = "  ".join(f"dep@{s:.3g}".rjust(9) for s in slips)
        print(f"  {'city':10} {'bkt':>9} {'mid':>5} {'depth':>8} {slipcols}  {'gates':>5}")
        for r in sorted(nd_keepers, key=lambda r: (r["city"], -r["deploy"][headline])):
            deps = "  ".join(f"${r['deploy'][s]:>7,.0f}" for s in slips)
            print(f"  {r['city']:10} {r['bucket'][:9]:>9} {r['mid']:>5.2f} ${r['depth']:>6,.0f} "
                  f"{deps}  {'ok' if r['passes_gates'] else 'FAIL':>5}")
        dep = sorted(r["deploy"][headline] for r in nd_keepers)
        print(f"\n  next-day keeper deployable@{headline:.3g}: "
              f"min ${dep[0]:,.0f} / median ${dep[len(dep)//2]:,.0f} / max ${dep[-1]:,.0f}  "
              f"(vs ${max_bet:,.0f} intended max bet)")
        fits = sum(1 for d in dep if d >= max_bet)
        print(f"  buckets where the full ${max_bet:,.0f} bet fits at <= {headline:.3g} slippage: "
              f"{fits}/{len(dep)}")
    else:
        print("  (no next-day keeper buckets open right now — re-run; the slate rolls daily)")

    # ---- per-city x lead matrix (median deployable@headline) --------------------------
    print("\n" + "=" * 92)
    print(f"median deployable@{headline:.3g} — city x lead  ($; '-' = none open)")
    print("=" * 92)
    leads = sorted(by_lead)
    print(f"  {'city':12} " + "  ".join(_lead_label(l).rjust(12) for l in leads) + "   keeper")
    by_city = defaultdict(lambda: defaultdict(list))
    for r in rows:
        by_city[r["city"]][r["lead"]].append(r["deploy"][headline])
    for city in sorted(by_city, key=lambda c: (c not in KEEPERS, c)):
        cells = []
        for l in leads:
            vals = sorted(by_city[city].get(l, []))
            cells.append(f"${vals[len(vals)//2]:>10,.0f}" if vals else f"{'-':>11}")
        print(f"  {city:12} " + "  ".join(cells) + f"   {'Y' if city in KEEPERS else ''}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cities", type=str, default=None, help="comma list (default = active WEATHER_CITIES)")
    ap.add_argument("--slips", type=str, default="0.005,0.01,0.02",
                    help="VWAP-vs-mid slippage caps in price units (default 0.005,0.01,0.02)")
    args = ap.parse_args()
    cities = ([c.strip() for c in args.cities.split(",")] if args.cities
              else [c.strip() for c in settings.WEATHER_CITIES.split(",") if c.strip()])
    slips = sorted(float(s) for s in args.slips.split(","))
    asyncio.run(probe(cities, slips))


if __name__ == "__main__":
    main()
