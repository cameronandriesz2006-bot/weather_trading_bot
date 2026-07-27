"""Re-fit the intraday σ/drift curve on SETTLEMENT-GRADE obs (IEM METARs) for the
US cities the bot actively trades.

The deployed intraday_curve.json was fit on the Meteostat bulk hourly archive with a
Meteostat-derived "final" extreme. Two mismatches vs what the live bot actually faces:
  1. the FINAL is not the settlement number (per-ob Wunderground integer-F rounding,
     then max — Meteostat's hourly max misses it, e.g. KBKF 2026-07-01: 88.5 vs 90);
  2. the RUNNING extreme at hour H is not what the bot KNOWS at H: the live floor
     reads the NWS feed at ob_time + ~15 min publish latency, and station cadence
     differs (KORD/KATL ~5-min METARs, KBKF hourly at :58).

Here both sides use the live convention: known(H) = extreme over obs with
ob_time+15min <= H:30 (the mid-point of a 15-min-scan hour), final = settlement
number. remaining(H) = final - known(H). Cities not refit (parked / °C) keep their
existing Meteostat-based entries.

Run: PYTHONPATH=. venv/bin/python -m backend.data.intraday_refit_iem --years 5
Rewrites backend/data/intraday_curve.json (per-city entries only for the refit set).
"""
import argparse
import asyncio
import csv
import io
import json
import math
import statistics
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import httpx

from backend.data.weather import CITY_CONFIG

IEM_ASOS = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
CURVE_FILE = Path(__file__).with_name("intraday_curve.json")
SUMMER_MONTHS = (5, 9)         # May 1 .. Sep 30, matching the deployed curve's season
LATENCY_MIN = 15               # NWS publish latency the live floor experiences
DECISION_MINUTE = 30           # decide at H:30, matching edge2_publish_honest
MIN_OB_HOURS = 18              # quality floor: distinct hours with obs on the day


async def fetch_year(client, icao: str, year: int, tzname: str):
    params = {"station": icao.lstrip("K"), "data": "tmpf",
              "year1": year, "month1": SUMMER_MONTHS[0], "day1": 1,
              "year2": year, "month2": SUMMER_MONTHS[1], "day2": 30,
              "tz": tzname, "format": "onlycomma", "missing": "M", "report_type": "3,4"}
    for attempt in range(4):
        try:
            r = await client.get(IEM_ASOS, params=params, timeout=300.0)
            if r.status_code != 429:
                r.raise_for_status()
                return r.text
        except httpx.HTTPError:
            if attempt == 3:
                raise
        await asyncio.sleep(45 * (attempt + 1))
    r.raise_for_status()


def accumulate(text: str, rem_high, rem_low) -> int:
    """Parse one year's METAR CSV and append remaining-move samples per hour."""
    per_day = defaultdict(list)   # local date -> [(known_minute, roundedF, ob_hour)]
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("tmpf") in ("M", "", None):
            continue
        v = row["valid"]
        try:
            d = date(int(v[:4]), int(v[5:7]), int(v[8:10]))
            hh, mm = int(v[11:13]), int(v[14:16])
        except (ValueError, IndexError):
            continue
        per_day[d].append((hh * 60 + mm + LATENCY_MIN,
                           float(math.floor(float(row["tmpf"]) + 0.5)), hh))
    ndays = 0
    for d, obs in per_day.items():
        if len({h for _, _, h in obs}) < MIN_OB_HOURS:
            continue
        ndays += 1
        obs.sort()
        final_high = max(t for _, t, _ in obs)
        final_low = min(t for _, t, _ in obs)
        run_max, run_min, i = None, None, 0
        for H in range(24):
            cutoff = H * 60 + DECISION_MINUTE
            while i < len(obs) and obs[i][0] <= cutoff:
                t = obs[i][1]
                run_max = t if run_max is None else max(run_max, t)
                run_min = t if run_min is None else min(run_min, t)
                i += 1
            if run_max is not None:
                rem_high[H].append(final_high - run_max)
                rem_low[H].append(final_low - run_min)
    return ndays


def summarize(rem):
    return {str(h): {"mean": round(statistics.mean(v), 2),
                     "std": round(statistics.stdev(v), 2), "n": len(v)}
            for h, v in rem.items() if len(v) >= 2}


async def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cities", type=str, default="denver,chicago,atlanta")
    ap.add_argument("--years", type=int, default=5)
    args = ap.parse_args()
    cities = [c.strip() for c in args.cities.split(",")]
    this_year = date.today().year
    years = list(range(this_year - args.years, this_year + 1))

    data = json.loads(CURVE_FILE.read_text())
    cities_out = data.setdefault("cities", {})
    async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}) as client:
        for city in cities:
            cfg = CITY_CONFIG[city]
            icao, tzname = cfg["nws_station"], cfg.get("tz") or "UTC"
            rem_high, rem_low = defaultdict(list), defaultdict(list)
            ndays = 0
            for y in years:
                text = await fetch_year(client, icao, y, tzname)
                ndays += accumulate(text, rem_high, rem_low)
                await asyncio.sleep(8)   # stay under the IEM per-IP throttle
            cities_out[city] = {"unit": cfg.get("unit", "F"), "station": icao, "tz": tzname,
                                "days": ndays, "high": summarize(rem_high), "low": summarize(rem_low)}
            h16 = cities_out[city]["high"].get("16", {})
            h17 = cities_out[city]["high"].get("17", {})
            print(f"{city}: {ndays} days ({years[0]}-{years[-1]} May-Sep)  "
                  f"high σ@16={h16.get('std')} drift@16={h16.get('mean')}  "
                  f"σ@17={h17.get('std')} drift@17={h17.get('mean')}")

    data["computed_at"] = datetime.utcnow().isoformat()
    data["method"] = (data.get("method", "") +
                      f" | {','.join(cities)} REFIT {date.today().isoformat()} on IEM METARs: "
                      f"per-ob WU integer-F rounding, known(H)=obs published by H:{DECISION_MINUTE:02d} "
                      f"(+{LATENCY_MIN}min latency), final=settlement number")
    CURVE_FILE.write_text(json.dumps(data, indent=1))
    print(f"wrote {CURVE_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
