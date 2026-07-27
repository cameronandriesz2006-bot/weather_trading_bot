"""Cross-check the observation source against what bets actually settle on.

The intraday backtest (intraday_backtest.py) is built from Meteostat station
observations. This verifies those observations agree with the GROUND TRUTH of what
Polymarket markets actually resolved to — so we know the climb curve is calibrated
to the same thermometer bets pay out on (and we catch any city on an outright wrong
station). For every RESOLVED past daily-temperature market we take the bucket that
won (= the settled high/low) and ask: does Meteostat's observed daily max/min for
that station+date fall in that bucket's rounding interval?

Run:  python -m backend.data.source_crosscheck
"""
import asyncio
import json
from collections import defaultdict
from datetime import date
from typing import Dict, List, Optional, Tuple

import httpx

from backend.data.weather import CITY_CONFIG, METEOSTAT_STATION
from backend.data.weather_markets import parse_event_slug, parse_bucket_label

GAMMA = "https://gamma-api.polymarket.com/events"
METEOSTAT_DAILY = "https://d.meteostat.net/app/proxy/stations/daily"
HEADERS = {"User-Agent": "Mozilla/5.0"}


def _c_to_native(c, unit):
    return (c * 9.0 / 5.0 + 32.0) if (unit == "F" and c is not None) else c


def _in_bucket(value: float, low: Optional[float], high: Optional[float]) -> Tuple[bool, float]:
    """Is value inside the bucket's rounding interval [low-0.5, high+0.5)? Also
    return how far OUTSIDE it is (0 if inside) — a near-miss vs a real mismatch."""
    lo = (low - 0.5) if low is not None else float("-inf")
    hi = (high + 0.5) if high is not None else float("inf")
    if lo <= value < hi:
        return True, 0.0
    gap = (lo - value) if value < lo else (value - (hi - 1e-9))
    return False, abs(gap)


async def _fetch_resolved_events(client: httpx.AsyncClient) -> List[dict]:
    out = []
    for off in range(0, 2000, 100):
        r = await client.get(GAMMA, params={"closed": "true", "limit": 100,
                                            "offset": off, "tag_slug": "daily-temperature"})
        r.raise_for_status()
        page = r.json()
        if not page:
            break
        out.extend(page)
        if len(page) < 100:
            break
    return out


def _winning_bucket(event: dict) -> Optional[Tuple[Optional[float], Optional[float]]]:
    """The bucket whose YES resolved to ~1 = the settled value's bucket."""
    for m in event.get("markets", []):
        op = m.get("outcomePrices")
        if isinstance(op, str):
            try:
                op = json.loads(op)
            except Exception:
                continue
        if op and len(op) >= 2 and float(op[0]) > 0.95:
            return parse_bucket_label(m.get("groupItemTitle") or "")
    return None


async def _fetch_obs_range(client, station, start, end) -> Dict[str, dict]:
    r = await client.get(METEOSTAT_DAILY, params={"station": station,
                          "start": start.isoformat(), "end": end.isoformat()}, headers=HEADERS)
    r.raise_for_status()
    return {x["date"][:10]: x for x in r.json().get("data", []) if x.get("date")}


async def main():
    async with httpx.AsyncClient(timeout=40.0, headers=HEADERS) as client:
        events = await _fetch_resolved_events(client)

        # records: (city, metric, date, (low,high))
        records = []
        for e in events:
            pe = parse_event_slug(e.get("slug", ""))
            if not pe:
                continue
            city, metric, tdate = pe
            if tdate >= date.today() or city not in METEOSTAT_STATION:
                continue
            wb = _winning_bucket(e)
            if wb is None:
                continue
            records.append((city, metric, tdate, wb))

        # group dates per station, fetch each station's obs range once
        by_city_dates = defaultdict(list)
        for city, metric, tdate, wb in records:
            by_city_dates[city].append(tdate)
        obs_cache = {}
        for city, dates in by_city_dates.items():
            station = METEOSTAT_STATION.get(city)
            if not station:
                continue
            obs_cache[city] = await _fetch_obs_range(client, station, min(dates), max(dates))

    # compare
    stats = defaultdict(lambda: {"agree": 0, "total": 0, "gaps": []})
    for city, metric, tdate, (low, high) in records:
        if not METEOSTAT_STATION.get(city):
            continue
        row = obs_cache.get(city, {}).get(tdate.isoformat())
        if not row:
            continue
        unit = CITY_CONFIG[city].get("unit", "F")
        val = _c_to_native(row.get("tmax") if metric == "high" else row.get("tmin"), unit)
        if val is None:
            continue
        ok, gap = _in_bucket(val, low, high)
        s = stats[city]
        s["total"] += 1
        if ok:
            s["agree"] += 1
        else:
            s["gaps"].append(gap)

    print(f"\nSource cross-check — Meteostat obs vs Polymarket settled bucket")
    print(f"{'city':12} {'unit':4} {'agree':>7} {'rate':>6} {'median miss (when off)':>24}")
    overall_a = overall_t = 0
    for city in CITY_CONFIG:
        s = stats.get(city)
        if not s or s["total"] == 0:
            print(f"{city:12} {CITY_CONFIG[city].get('unit','F'):4} {'-':>7}   (no resolved data / no station)")
            continue
        rate = s["agree"] / s["total"]
        gaps = sorted(s["gaps"])
        med = gaps[len(gaps)//2] if gaps else 0.0
        u = CITY_CONFIG[city].get("unit", "F")
        flag = "  <-- station likely WRONG" if rate < 0.6 else ("  (ok)" if rate >= 0.8 else "  (marginal)")
        print(f"{city:12} {u:4} {s['agree']:>3}/{s['total']:<3} {rate:>5.0%} {med:>20.1f}{u}{flag}")
        overall_a += s["agree"]; overall_t += s["total"]
    if overall_t:
        print(f"\nOVERALL: {overall_a}/{overall_t} = {overall_a/overall_t:.0%} of settled outcomes matched the observed value")


if __name__ == "__main__":
    asyncio.run(main())
