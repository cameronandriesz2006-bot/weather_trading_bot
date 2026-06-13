"""Backfill per-station forecast bias from Open-Meteo historical archives.

We measure the systematic offset of our live model (GFS) against actual (ERA5
reanalysis) daily high/low at each settlement station, so the forecast can
subtract it before pricing buckets. Raw GFS has repeatable per-station offsets
(e.g. it ran ~2F cold on NYC overnight lows in early June 2026) that the market
has already priced in; uncorrected, those offsets masquerade as trading edge.

This needs NO trading simulation and NO waiting: both halves of each
(forecast, actual) pair already exist in history.
  - forecast half: historical-forecast-api (archived GFS forecasts)
  - actual half:   archive-api (ERA5 reanalysis)

Run:  python -m backend.data.bias_backfill            (default 60-day window)
      python -m backend.data.bias_backfill --days 90
Writes backend/data/station_bias.json, which weather.py reads at forecast time.
"""
import argparse
import asyncio
import json
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import httpx

from backend.data.weather import CITY_CONFIG

HIST_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
BIAS_FILE = Path(__file__).with_name("station_bias.json")

DEFAULT_WINDOW_DAYS = 60
# ERA5 archive lags real time; end the window a few days back so "actual" exists.
ARCHIVE_LAG_DAYS = 3


async def _fetch_daily(
    client: httpx.AsyncClient, url: str, lat: float, lon: float,
    start: date, end: date, model: Optional[str] = None,
) -> Dict[str, Dict[str, Optional[float]]]:
    """Fetch daily max/min (F) keyed by ISO date. Local-day aggregation (matches
    the live forecast and market settlement)."""
    params = {
        "latitude": lat, "longitude": lon,
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "daily": "temperature_2m_max,temperature_2m_min",
        "temperature_unit": "fahrenheit", "timezone": "auto",
    }
    if model:
        params["models"] = model
    r = await client.get(url, params=params)
    r.raise_for_status()
    daily = r.json().get("daily", {})
    times = daily.get("time", []) or []
    highs = daily.get("temperature_2m_max", []) or []
    lows = daily.get("temperature_2m_min", []) or []
    out: Dict[str, Dict[str, Optional[float]]] = {}
    for i, t in enumerate(times):
        out[t] = {
            "high": highs[i] if i < len(highs) else None,
            "low": lows[i] if i < len(lows) else None,
        }
    return out


def _errors(forecast: dict, actual: dict, metric: str) -> List[float]:
    """forecast - actual for every date present (and non-null) in both."""
    errs = []
    for d, fv in forecast.items():
        av = actual.get(d)
        if not av:
            continue
        f, a = fv.get(metric), av.get(metric)
        if f is None or a is None:
            continue
        errs.append(float(f) - float(a))
    return errs


async def compute_all(days: int = DEFAULT_WINDOW_DAYS) -> dict:
    """Compute bias = mean(forecast - actual) per station and metric."""
    end = date.today() - timedelta(days=ARCHIVE_LAG_DAYS)
    start = end - timedelta(days=days)
    stations: Dict[str, dict] = {}

    async with httpx.AsyncClient(timeout=30.0) as client:
        for city_key, cfg in CITY_CONFIG.items():
            lat, lon = cfg["lat"], cfg["lon"]
            forecast = await _fetch_daily(client, HIST_FORECAST_URL, lat, lon, start, end, model="gfs_seamless")
            actual = await _fetch_daily(client, ARCHIVE_URL, lat, lon, start, end)

            entry = {}
            for metric in ("high", "low"):
                errs = _errors(forecast, actual, metric)
                if errs:
                    entry[metric] = {
                        "bias_f": round(statistics.mean(errs), 3),
                        "stdev_f": round(statistics.stdev(errs), 3) if len(errs) > 1 else 0.0,
                        "samples": len(errs),
                    }
                else:
                    entry[metric] = {"bias_f": 0.0, "stdev_f": 0.0, "samples": 0}
            stations[city_key] = entry

    return {
        "computed_at": datetime.utcnow().isoformat(),
        "window_days": days,
        "method": "gfs_seamless_vs_era5_archive",
        "note": "bias_f = mean(forecast - actual); SUBTRACT from forecast mean.",
        "stations": stations,
    }


def write_bias(data: dict, path: Path = BIAS_FILE) -> None:
    path.write_text(json.dumps(data, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Backfill per-station forecast bias.")
    parser.add_argument("--days", type=int, default=DEFAULT_WINDOW_DAYS,
                        help=f"history window in days (default {DEFAULT_WINDOW_DAYS})")
    args = parser.parse_args()

    data = asyncio.run(compute_all(args.days))
    write_bias(data)

    print(f"Wrote {BIAS_FILE}  (window {data['window_days']}d, {data['method']})")
    for city, m in data["stations"].items():
        print(f"  {city:12s} high {m['high']['bias_f']:+.2f}F (n={m['high']['samples']})  "
              f"low {m['low']['bias_f']:+.2f}F (n={m['low']['samples']})")


if __name__ == "__main__":
    main()
