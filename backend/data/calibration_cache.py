"""Stream 2, step 1 — pull the calibration inputs ONCE and cache them to disk.

The overconfidence fix has to be measured against ALL the resolved history, and we'll iterate
on it many times. Re-pulling forecasts/obs/prices each time would (a) be slow and (b) hammer
the Open-Meteo quota the LIVE bot trades on. So we pull every raw input exactly once here and
dump it; overconfidence_eval.py then re-prices offline against this cache as many times as we
like, quota-free. The forecast MEANS don't change when we change the confidence logic — only
the pricing transformation does — so a single pull suffices.

Cached per resolved keeper event: the buckets (low/high/won/yes_token), the bias-corrected
blend forecast mean, the station's hourly obs (to rebuild observed-so-far at any hour), the
live ensemble spread (next-day base σ), the tz, and the raw CLOB price history per YES token
(to read the market price at any station-local hour).

Run:  PYTHONPATH=. venv/bin/python -m backend.data.calibration_cache --events 160
"""
import argparse
import asyncio
import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import httpx

from backend.config import settings
from backend.data.weather import CITY_CONFIG, METEOSTAT_STATION
from backend.data.calibration_backfill import (fetch_resolved_events, extract_event,
                                               fetch_blend_means)
from backend.data.calibration_intraday import (fetch_hourly_obs, fetch_ensemble_std,
                                               _fetch_day_history, HEADERS)

CACHE_FILE = Path(__file__).with_name("calibration_cache.json")
KEEPERS = ["chicago", "nyc", "hong_kong", "denver", "tokyo"]


async def build(cities, models, n_events: int):
    async with httpx.AsyncClient(timeout=60.0, headers=HEADERS) as client:
        events = await fetch_resolved_events(client)
        raw, dates_by_city = [], defaultdict(list)
        for e in events:
            ex = extract_event(e)
            if not ex:
                continue
            city, metric, tdate, buckets = ex
            if city not in cities or tdate >= date.today():
                continue
            raw.append((city, metric, tdate.isoformat(), buckets))
            dates_by_city[city].append(tdate)
        print(f"{len(raw)} resolved in-scope events across {len(dates_by_city)} cities")

        # forecasts + obs + tz + ensemble spread (the Open-Meteo / Meteostat pulls — ONCE)
        means, obs, tzs, stds = {}, {}, {}, {}
        for city, dts in dates_by_city.items():
            cfg = CITY_CONFIG.get(city)
            if not cfg:
                continue
            lo, hi = min(dts) - timedelta(days=1), max(dts) + timedelta(days=1)
            means[city] = await fetch_blend_means(client, cfg, models, lo, hi)
            tzs[city] = cfg.get("tz") or "UTC"
            stds[city] = await fetch_ensemble_std(client, cfg, models)
            st = METEOSTAT_STATION.get(city)
            if st:
                od = await fetch_hourly_obs(client, st, lo, hi, tzs[city], cfg.get("unit", "F"))
                # serialise date->iso, hour int stays as str keys
                obs[city] = {d.isoformat(): {str(h): t for h, t in hours.items()}
                             for d, hours in od.items()}
            else:
                obs[city] = {}
        print(f"forecasts+obs+spread pulled for {len(means)} cities  "
              f"(spreads: " + " ".join(f"{c}:{(stds[c].get('high') or 0):.1f}/{(stds[c].get('low') or 0):.1f}"
                                       for c in stds) + ")")

    # sample events for market histories (cap the CLOB calls), keep ALL their buckets
    raw.sort(key=lambda r: r[2])
    stride = max(1, len(raw) // n_events)
    sampled = raw[::stride][:n_events]
    tokens = sorted({b["yes_token"] for _, _, _, bks in sampled for b in bks if b["yes_token"]})
    tok_date = {b["yes_token"]: d for _, _, d, bks in sampled for b in bks if b["yes_token"]}
    print(f"fetching CLOB price history for {len(tokens)} tokens ({len(sampled)} sampled events)...")
    sem = asyncio.Semaphore(12)
    async with httpx.AsyncClient(timeout=30.0, headers=HEADERS) as client:
        hists = await asyncio.gather(*[_fetch_day_history(client, t, tok_date[t], sem) for t in tokens])
    histories = {t: h for t, h in zip(tokens, hists) if h}
    print(f"got {len(histories)} non-empty histories")

    out = {
        "cities": cities, "models": models,
        "events": [{"city": c, "metric": m, "date": d, "buckets": bks} for c, m, d, bks in sampled],
        "means": means, "obs": obs, "tzs": tzs, "stds": stds, "histories": histories,
        "intraday_min_f": settings.WEATHER_INTRADAY_SIGMA_MIN_F,
        "blend_inflation": settings.WEATHER_BLEND_SIGMA_INFLATION,
    }
    CACHE_FILE.write_text(json.dumps(out))
    nb = sum(len(e["buckets"]) for e in out["events"])
    print(f"\nwrote {CACHE_FILE.name}: {len(out['events'])} events / {nb} buckets / "
          f"{len(histories)} token histories  ({CACHE_FILE.stat().st_size//1024} KB)")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cities", type=str, default=",".join(KEEPERS))
    ap.add_argument("--models", type=str, default=None)
    ap.add_argument("--events", type=int, default=160, help="events to fetch market history for")
    args = ap.parse_args()
    cities = [c.strip() for c in args.cities.split(",")]
    models = ([m.strip() for m in args.models.split(",")] if args.models
              else [m.strip() for m in settings.WEATHER_BLEND_MODELS.split(",") if m.strip()])
    asyncio.run(build(cities, models, args.events))


if __name__ == "__main__":
    main()
