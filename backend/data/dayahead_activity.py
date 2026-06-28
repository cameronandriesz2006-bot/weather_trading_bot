"""Stream 1B — how much actually TRADED in the day-before window, historically.

Companion to dayahead_liquidity.py (which measures LIVE book depth). The live book gives the
precise current depth but only a snapshot of today's slate; this uses the retained TRADE
history (Polymarket Data API /trades, which keeps executed fills with size+price+timestamp)
to ask the complementary question on a BIG sample of RESOLVED keeper markets:

  of all the money that ever traded on a bucket, how much changed hands in the
  24-48h-before-settlement window — i.e. the 'place the bet the day before' window?

This is ACTIVITY (what did trade), a lower bound on harvestable size, not DEPTH (what could
have been filled) — depth is live-only. But if almost nothing traded day-ahead historically,
that corroborates the live-book finding that day-ahead size is tiny. Quota-safe (Polymarket
only; no Open-Meteo).

Run:  PYTHONPATH=. venv/bin/python -m backend.data.dayahead_activity --events 20
"""
import argparse
import asyncio
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional

import httpx

from backend.config import settings
from backend.data.weather import CITY_CONFIG
from backend.data.calibration_backfill import fetch_resolved_events, extract_event

TRADES_URL = "https://data-api.polymarket.com/trades"
HEADERS = {"User-Agent": "Mozilla/5.0"}
KEEPERS = {"chicago", "nyc", "hong_kong", "denver", "tokyo"}


def _settlement_ts(city: str, tdate: date) -> int:
    """Unix ts of the END of the target local day (≈ when the daily extreme is final)."""
    tz = ZoneInfo(CITY_CONFIG.get(city, {}).get("tz", "UTC"))
    end_local = datetime(tdate.year, tdate.month, tdate.day, tzinfo=tz) + timedelta(days=1)
    return int(end_local.timestamp())


async def _all_trades(client, condition_id: str, sem) -> List[dict]:
    out: List[dict] = []
    async with sem:
        for off in range(0, 3000, 500):
            try:
                r = await client.get(TRADES_URL, params={"market": condition_id,
                                                          "limit": 500, "offset": off})
                if r.status_code >= 400:
                    break
                page = r.json() or []
            except Exception:
                break
            out.extend(page)
            if len(page) < 500:
                break
    return out


async def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--events", type=int, default=20, help="resolved keeper events to sample")
    ap.add_argument("--cities", type=str, default=",".join(sorted(KEEPERS)))
    args = ap.parse_args()
    cities = [c.strip() for c in args.cities.split(",")]

    async with httpx.AsyncClient(timeout=40.0, headers=HEADERS) as client:
        events = await fetch_resolved_events(client)
        # collect resolved in-scope events with their bucket conditionIds
        scoped = []
        for e in events:
            ex = extract_event(e)
            if not ex:
                continue
            city, metric, tdate, buckets = ex
            if city not in cities or tdate >= date.today():
                continue
            conds = [m.get("conditionId") for m in e.get("markets", []) if m.get("conditionId")]
            if conds:
                scoped.append((city, metric, tdate, conds))
        # sample evenly across the date range
        scoped.sort(key=lambda x: x[2])
        stride = max(1, len(scoped) // args.events)
        sample = scoped[::stride][:args.events]
        print(f"{len(scoped)} resolved keeper events available; sampling {len(sample)}\n")

        sem = asyncio.Semaphore(8)
        # window $ traded, per event, split by hours-before-settlement
        per_city = defaultdict(lambda: {"lifetime": 0.0, "w_24_48": 0.0, "w_0_24": 0.0,
                                        "w_48p": 0.0, "n_ev": 0, "nd_buckets": []})
        for city, metric, tdate, conds in sample:
            settle = _settlement_ts(city, tdate)
            tasks = [_all_trades(client, c, sem) for c in conds]
            all_bucket_trades = await asyncio.gather(*tasks)
            pc = per_city[city]
            pc["n_ev"] += 1
            for trades in all_bucket_trades:
                nd = 0.0
                for t in trades:
                    try:
                        notional = float(t["size"]) * float(t["price"])
                        ts = int(t["timestamp"])
                    except (KeyError, ValueError, TypeError):
                        continue
                    pc["lifetime"] += notional
                    h_before = (settle - ts) / 3600.0
                    if h_before < 0:
                        continue  # after settlement (resolution trades)
                    if h_before <= 24:
                        pc["w_0_24"] += notional
                    elif h_before <= 48:
                        pc["w_24_48"] += notional
                        nd += notional
                    else:
                        pc["w_48p"] += notional
                pc["nd_buckets"].append(nd)

    print("=" * 88)
    print("HISTORICAL $ TRADED by window  (24-48h = the 'place it the day before' window)")
    print("=" * 88)
    print(f"  {'city':10} {'events':>6} {'lifetime$':>11} {'>48h':>9} {'24-48h':>9} {'0-24h':>9} "
          f"{'24-48h med/bkt':>14}")
    tot = defaultdict(float)
    nd_all = []
    for city in sorted(per_city, key=lambda c: (c not in KEEPERS, c)):
        pc = per_city[city]
        for k in ("lifetime", "w_48p", "w_24_48", "w_0_24"):
            tot[k] += pc[k]
        ndb = sorted(pc["nd_buckets"])
        nd_all += pc["nd_buckets"]
        med = ndb[len(ndb) // 2] if ndb else 0.0
        print(f"  {city:10} {pc['n_ev']:>6} ${pc['lifetime']:>10,.0f} ${pc['w_48p']:>8,.0f} "
              f"${pc['w_24_48']:>8,.0f} ${pc['w_0_24']:>8,.0f} ${med:>12,.0f}")
    print("  " + "-" * 84)
    print(f"  {'TOTAL':10} {'':>6} ${tot['lifetime']:>10,.0f} ${tot['w_48p']:>8,.0f} "
          f"${tot['w_24_48']:>8,.0f} ${tot['w_0_24']:>8,.0f}")
    if tot["lifetime"] > 0:
        print(f"\n  share of all volume that traded in the 24-48h day-before window: "
              f"{100*tot['w_24_48']/tot['lifetime']:.1f}%")
        print(f"  share in the final 0-24h (same-day): {100*tot['w_0_24']/tot['lifetime']:.1f}%")
    if nd_all:
        nd_all.sort()
        print(f"  per-bucket day-before $ traded (n={len(nd_all)} buckets): "
              f"median ${nd_all[len(nd_all)//2]:,.0f}  /  p90 ${nd_all[int(len(nd_all)*0.9)]:,.0f}  "
              f"/  max ${nd_all[-1]:,.0f}")


if __name__ == "__main__":
    asyncio.run(main())
