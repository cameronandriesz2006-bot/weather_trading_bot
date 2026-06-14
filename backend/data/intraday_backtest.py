"""Backtest: how much does the daily high/low still MOVE at each hour of the day?

This builds the intraday "shrink schedule" for the forecast's uncertainty near
settlement — the fix for the bot staying too unsure (a flat ~2 deg) when the day's
extreme is nearly locked in. It is calibrated from DECADES of real hourly station
observations (Meteostat bulk archive), NOT from live bets, and uses each market's
SETTLEMENT station (the same ones bias_backfill / the live floor use).

For every summer local day at a station we compute, at each LOCAL hour H:

    remaining_high(H) = final_daily_high - max_observed_so_far(H)   (>= 0)
    remaining_low(H)  = final_daily_low  - min_observed_so_far(H)   (<= 0)

and aggregate the MEAN and STDEV across all days. The **stdev** is the residual
uncertainty in the final extreme given what's been seen by hour H — i.e. exactly
how much the forecast's doubt SHOULD be at that hour (it should shrink toward ~0 by
evening for highs, by late morning for lows). The mean says whether there's a
systematic further drift to expect.

Values are in each city's NATIVE unit (F for US, C for international). Time is the
station's LOCAL clock (DST-aware via zoneinfo), since the market settles on the
local calendar day.

Boundaries (agreed scope):
  - the WHOLE day, hour by hour (not just the final hours);
  - highs AND lows (opposite diurnal rhythms), each its own curve;
  - per city, settlement-station observations only;
  - seasonally matched (summer months) across the last N_YEARS for sample size;
  - weird days (late/early peaks, fronts) are KEPT — they are real uncertainty.

Run:  python -m backend.data.intraday_backtest
Writes backend/data/intraday_curve.json.
"""
import argparse
import asyncio
import gzip
import json
import statistics
from collections import defaultdict
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx

from backend.data.weather import CITY_CONFIG, METEOSTAT_STATION

BULK_HOURLY_URL = "https://bulk.meteostat.net/v2/hourly/{station}.csv.gz"
META_URL = "https://d.meteostat.net/app/proxy/stations/meta"
HEADERS = {"User-Agent": "Mozilla/5.0"}

CACHE_DIR = Path(__file__).with_name("backtest_cache")
OUT_FILE = Path(__file__).with_name("intraday_curve.json")

SUMMER_MONTHS = {5, 6, 7, 8, 9}   # late spring through early autumn (we trade in June)
N_YEARS = 10                      # recent years (climate-relevant) with ample sample
MIN_HOURS_PER_DAY = 20            # data-quality floor; skip sparse days


def _c_to_native(c: float, unit: str) -> float:
    return (c * 9.0 / 5.0 + 32.0) if unit == "F" else c


async def _download_hourly(client: httpx.AsyncClient, station: str) -> Optional[Path]:
    """Download (and cache) a station's full bulk hourly archive."""
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / f"{station}.csv.gz"
    if path.exists() and path.stat().st_size > 0:
        return path
    try:
        r = await client.get(BULK_HOURLY_URL.format(station=station), timeout=120.0)
        if r.status_code != 200:
            print(f"  [{station}] hourly archive HTTP {r.status_code}")
            return None
        path.write_bytes(r.content)
        return path
    except Exception as e:
        print(f"  [{station}] download failed: {e}")
        return None


async def _station_tz(client: httpx.AsyncClient, station: str) -> Optional[str]:
    try:
        r = await client.get(META_URL, params={"id": station}, headers=HEADERS, timeout=20.0)
        return (r.json().get("data") or {}).get("timezone")
    except Exception:
        return None


def _local_days(path: Path, tz: ZoneInfo, unit: str,
                months: set, year_lo: int) -> Dict[date, Dict[int, float]]:
    """Parse the bulk hourly CSV into {local_date: {local_hour: temp_native}} for the
    seasonal window. Columns: date,hour,temp,dwpt,rhum,... (temp °C, time UTC)."""
    out: Dict[date, Dict[int, float]] = defaultdict(dict)
    text = gzip.decompress(path.read_bytes()).decode()
    for line in text.splitlines():
        if not line:
            continue
        parts = line.split(",")
        if len(parts) < 3:
            continue
        d, h, t = parts[0], parts[1], parts[2]
        if not t:
            continue
        try:
            y, m, day = int(d[:4]), int(d[5:7]), int(d[8:10])
            if y < year_lo or m not in months:
                continue
            utc_dt = datetime(y, m, day, int(h), tzinfo=timezone.utc)
            temp = _c_to_native(float(t), unit)
        except (ValueError, IndexError):
            continue
        loc = utc_dt.astimezone(tz)
        # Only keep the converted day if it's still in-season (a UTC row can shift a
        # day either way at the boundary); avoids half-days bleeding in.
        if loc.month not in months:
            continue
        out[loc.date()][loc.hour] = temp
    return out


def _accumulate(days: Dict[date, Dict[int, float]]):
    """For each local hour, the list of remaining moves to the final extreme."""
    rem_high: Dict[int, List[float]] = defaultdict(list)
    rem_low: Dict[int, List[float]] = defaultdict(list)
    for _, by_hour in days.items():
        if len(by_hour) < MIN_HOURS_PER_DAY:
            continue
        final_high = max(by_hour.values())
        final_low = min(by_hour.values())
        run_max, run_min = float("-inf"), float("inf")
        for h in sorted(by_hour):
            run_max = max(run_max, by_hour[h])
            run_min = min(run_min, by_hour[h])
            rem_high[h].append(final_high - run_max)   # >= 0
            rem_low[h].append(final_low - run_min)      # <= 0
    return rem_high, rem_low


def _summarize(rem: Dict[int, List[float]]) -> Dict[int, dict]:
    out = {}
    for h, vals in rem.items():
        if len(vals) < 2:
            continue
        out[h] = {
            "mean": round(statistics.mean(vals), 2),
            "std": round(statistics.stdev(vals), 2),
            "n": len(vals),
        }
    return out


async def build(months: set, n_years: int) -> dict:
    year_lo = date.today().year - n_years
    cities: Dict[str, dict] = {}

    async with httpx.AsyncClient(headers=HEADERS) as client:
        # Download all archives concurrently (cached after the first run).
        stations = {ck: METEOSTAT_STATION.get(ck) for ck in CITY_CONFIG}
        await asyncio.gather(*[
            _download_hourly(client, s) for s in stations.values() if s
        ], return_exceptions=True)
        tzs = dict(zip(
            [ck for ck, s in stations.items() if s],
            await asyncio.gather(*[_station_tz(client, s) for ck, s in stations.items() if s]),
        ))

    for ck, cfg in CITY_CONFIG.items():
        station = stations.get(ck)
        tzname = tzs.get(ck)
        if not station or not tzname:
            cities[ck] = {"skipped": "no_station_or_tz"}
            continue
        path = CACHE_DIR / f"{station}.csv.gz"
        if not path.exists():
            cities[ck] = {"skipped": "no_data"}
            continue
        unit = cfg.get("unit", "F")
        days = _local_days(path, ZoneInfo(tzname), unit, months, year_lo)
        rem_high, rem_low = _accumulate(days)
        cities[ck] = {
            "unit": unit, "station": station, "tz": tzname, "days": len(days),
            "high": _summarize(rem_high),
            "low": _summarize(rem_low),
        }
        print(f"  {ck:12} {station:6} {tzname:18} days={len(days):4} unit={unit}")

    return {
        "computed_at": datetime.utcnow().isoformat(),
        "method": "meteostat_hourly_intraday_remaining_move",
        "months": sorted(months), "years_back": n_years, "min_hours_per_day": MIN_HOURS_PER_DAY,
        "note": ("At each LOCAL hour: mean/std of (final daily extreme - extreme so far), "
                 "in native unit. std = residual uncertainty -> the forecast's doubt at that hour."),
        "cities": cities,
    }


def _print_curve(data: dict):
    print("\nResidual uncertainty (std of remaining move) by local hour — the shrink schedule:")
    hours = [9, 12, 14, 16, 18, 20, 22]
    for ck, c in data["cities"].items():
        if c.get("skipped"):
            print(f"  {ck:12} [{c['skipped']}]"); continue
        u = c["unit"]
        hi = c["high"]
        cells = "  ".join(f"{h:02d}h={hi[h]['std']:.1f}" if h in hi else f"{h:02d}h=  -" for h in hours)
        print(f"  {ck:12} HIGH std{u}:  {cells}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", default=",".join(map(str, sorted(SUMMER_MONTHS))))
    ap.add_argument("--years", type=int, default=N_YEARS)
    args = ap.parse_args()
    months = {int(m) for m in args.months.split(",")}
    data = asyncio.run(build(months, args.years))
    OUT_FILE.write_text(json.dumps(data, indent=2))
    print(f"\nWrote {OUT_FILE}  (months {sorted(months)}, last {args.years}y)")
    _print_curve(data)


if __name__ == "__main__":
    main()
