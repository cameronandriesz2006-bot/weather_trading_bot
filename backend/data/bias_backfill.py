"""Backfill per-station forecast bias against the REAL settlement station.

We measure the systematic offset of our model (GFS) vs the **actual observed**
daily high/low at each market's settlement station, so the forecast can subtract
it before pricing buckets. Raw gridded forecasts have repeatable per-station
offsets — especially at coastal/microclimate stations (LAX, Miami) where a coarse
model grid can't resolve local cooling — and the market has already priced them
in; uncorrected, they masquerade as trading edge (and lose).

Critically the "actual" half is **realized station observations** (Meteostat),
NOT ERA5 reanalysis. ERA5 is itself a gridded product that agrees with GFS to
<1F while differing from the official station by 2-3F, so calibrating GFS->ERA5
left the real gap (to the station the market settles on) uncorrected. Each value
is computed in the city's NATIVE unit (F for US, C for international); the stored
bias is in that unit.
  - forecast half: historical-forecast-api (archived GFS forecasts) at the station
  - actual half:   Meteostat daily obs at the nearest settlement station

Both halves already exist in history, so this needs NO trading sim and NO waiting.

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
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
METEOSTAT_DAILY_URL = "https://d.meteostat.net/app/proxy/stations/daily"
METEOSTAT_HEADERS = {"User-Agent": "Mozilla/5.0"}  # the proxy 403s a bare client
BIAS_FILE = Path(__file__).with_name("station_bias.json")

DEFAULT_WINDOW_DAYS = 60
# Observations lag real time slightly; end the window a couple days back so the
# most recent days are complete.
OBS_LAG_DAYS = 2

# The 60-day forecast history comes from historical-forecast-api, but the LIVE bot
# trades on ensemble-api. For some coastal coords (e.g. Incheon) those two APIs
# snap to different grid cells and disagree by ~5°C for the SAME gfs_seamless model
# — so a bias measured from historical-forecast-api would be the WRONG model's bias.
# We guard against it: only trust a city's bias if historical-forecast-api agrees
# with the live ensemble on recent overlapping days within this tolerance (°F;
# scaled 1/1.8 for °C). Otherwise skip (the market-gap guardrail still protects it).
CONSISTENCY_MAX_F = 2.0

# Meteostat station id per city = the realized-observation source for the market's
# settlement station (resolved via Meteostat's nearby endpoint, matched to the
# named ICAO/station). None = no usable obs station near the settlement point
# (Shanghai/Pudong's nearest is ~35km, too far to be the same microclimate) -> we
# skip it and let the market-gap guardrail cover those events instead.
METEOSTAT_STATION: Dict[str, Optional[str]] = {
    "nyc": "72503",          # KLGA LaGuardia
    "chicago": "72530",      # KORD O'Hare
    "miami": "72202",        # KMIA
    "los_angeles": "72295",  # KLAX
    "denver": "KBKF0",       # Buckley SFB
    "london": "EGLC0",       # London City
    "tokyo": "47671",        # Haneda
    "seoul": "47113",        # Incheon
    "paris": "07150",        # Le Bourget
    "shanghai": None,        # no station within ~35km of ZSPD Pudong -> skip
    "hong_kong": "45007",    # HKO HQ (the named resolution source)
}


def _c_to_native(c: Optional[float], unit: str) -> Optional[float]:
    """Meteostat reports °C; convert to the city's native unit for differencing."""
    if c is None:
        return None
    return (c * 9.0 / 5.0 + 32.0) if unit == "F" else c


async def _fetch_forecast_daily(
    client: httpx.AsyncClient, lat: float, lon: float,
    start: date, end: date, temp_unit: str,
) -> Dict[str, Dict[str, Optional[float]]]:
    """Archived GFS daily max/min in `temp_unit`, keyed by ISO date. Local-day
    aggregation (timezone=auto) matches the live forecast and market settlement."""
    params = {
        "latitude": lat, "longitude": lon,
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "daily": "temperature_2m_max,temperature_2m_min",
        "temperature_unit": temp_unit, "timezone": "auto",
        "models": "gfs_seamless",
    }
    r = await client.get(HIST_FORECAST_URL, params=params)
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


async def _fetch_ensemble_recent(
    client: httpx.AsyncClient, lat: float, lon: float, temp_unit: str, past_days: int = 10,
) -> Dict[str, Dict[str, Optional[float]]]:
    """Live-model (ensemble-api) recent daily max/min, ensemble MEAN per day, in
    `temp_unit`. Only the last few days carry data; used to validate that the
    historical-forecast source matches the model the bot actually trades on."""
    params = {
        "latitude": lat, "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min",
        "temperature_unit": temp_unit, "timezone": "auto",
        "models": "gfs_seamless", "past_days": past_days, "forecast_days": 1,
    }
    r = await client.get(ENSEMBLE_URL, params=params)
    r.raise_for_status()
    daily = r.json().get("daily", {})
    times = daily.get("time", []) or []
    out: Dict[str, Dict[str, Optional[float]]] = {}
    for i, t in enumerate(times):
        highs = [v[i] for k, v in daily.items()
                 if "temperature_2m_max" in k and isinstance(v, list) and i < len(v) and v[i] is not None]
        lows = [v[i] for k, v in daily.items()
                if "temperature_2m_min" in k and isinstance(v, list) and i < len(v) and v[i] is not None]
        out[t] = {
            "high": statistics.mean(highs) if highs else None,
            "low": statistics.mean(lows) if lows else None,
        }
    return out


def _consistency_error(hist: dict, ensemble: dict, recent_days: int = 3) -> Optional[float]:
    """Mean |historical-forecast high - live-ensemble-mean high| over the most
    recent `recent_days` overlapping days. None if no overlap. Large => the two
    APIs disagree for this city, so a historical-forecast bias would not apply to
    the live model (e.g. Incheon coastal grid-snapping, ~5°C).

    We use only the most RECENT overlapping days and skip the ensemble's oldest
    available day: at the `past_days` boundary the ensemble returns an incomplete,
    artificially-low value (observed ~10-15° low) that would falsely fail the
    check. The newest few days are the reliable, fully-populated ones."""
    pairs = []
    for d, ev in ensemble.items():
        hv = hist.get(d)
        if not hv:
            continue
        a, b = hv.get("high"), ev.get("high")
        if a is None or b is None:
            continue
        pairs.append((d, abs(float(a) - float(b))))
    if not pairs:
        return None
    pairs.sort(key=lambda x: x[0])          # chronological
    recent = [diff for _, diff in pairs[-recent_days:]]
    return statistics.mean(recent)


async def _fetch_obs_daily(
    client: httpx.AsyncClient, station_id: str,
    start: date, end: date, unit: str,
) -> Dict[str, Dict[str, Optional[float]]]:
    """Realized daily max/min from the Meteostat station, converted to `unit`.
    Keyed by ISO date. Station-local daily aggregation (the official daily max)."""
    params = {
        "station": station_id,
        "start": start.isoformat(), "end": end.isoformat(),
    }
    r = await client.get(METEOSTAT_DAILY_URL, params=params, headers=METEOSTAT_HEADERS)
    r.raise_for_status()
    out: Dict[str, Dict[str, Optional[float]]] = {}
    for row in r.json().get("data", []) or []:
        d = row.get("date")
        if not d:
            continue
        out[d[:10]] = {
            "high": _c_to_native(row.get("tmax"), unit),
            "low": _c_to_native(row.get("tmin"), unit),
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
    """Compute bias = mean(forecast - observed) per station and metric, in the
    city's native unit, using realized station observations as the 'actual'."""
    # Fetch through today so the consistency check overlaps the live ensemble's
    # recent days; obs naturally lack the last day or two, and those forecast days
    # simply drop out of the bias pairing (no matching observation).
    end = date.today()
    start = end - timedelta(days=days + OBS_LAG_DAYS)
    stations: Dict[str, dict] = {}

    async with httpx.AsyncClient(timeout=30.0) as client:
        for city_key, cfg in CITY_CONFIG.items():
            unit = cfg.get("unit", "F")
            station_id = METEOSTAT_STATION.get(city_key)
            entry: Dict[str, object] = {"unit": unit, "station": station_id}

            if not station_id:
                # No usable obs station near the settlement point -> no correction;
                # the market-gap guardrail protects these events instead.
                for metric in ("high", "low"):
                    entry[metric] = {"bias_f": 0.0, "stdev_f": 0.0, "samples": 0}
                entry["skipped"] = "no_obs_station"
                stations[city_key] = entry
                continue

            temp_unit = "celsius" if unit == "C" else "fahrenheit"
            forecast = await _fetch_forecast_daily(client, cfg["lat"], cfg["lon"], start, end, temp_unit)
            actual = await _fetch_obs_daily(client, station_id, start, end, unit)

            # Guard: only trust this bias if the 60-day forecast source agrees with
            # the live ensemble model on recent days (else it's a different model's
            # bias — e.g. coastal grid-snapping divergence at Incheon).
            ensemble = await _fetch_ensemble_recent(client, cfg["lat"], cfg["lon"], temp_unit)
            consistency = _consistency_error(forecast, ensemble)
            tol = CONSISTENCY_MAX_F * ((1.0 / 1.8) if unit == "C" else 1.0)
            if consistency is None or consistency > tol:
                for metric in ("high", "low"):
                    entry[metric] = {"bias_f": 0.0, "stdev_f": 0.0, "samples": 0}
                entry["skipped"] = f"source_inconsistent({consistency:.1f}>{tol:.1f})" if consistency is not None else "no_ensemble_overlap"
                stations[city_key] = entry
                continue

            for metric in ("high", "low"):
                errs = _errors(forecast, actual, metric)
                if errs:
                    entry[metric] = {
                        "bias_f": round(statistics.mean(errs), 3),  # in native unit (name kept for compat)
                        "stdev_f": round(statistics.stdev(errs), 3) if len(errs) > 1 else 0.0,
                        "samples": len(errs),
                    }
                else:
                    entry[metric] = {"bias_f": 0.0, "stdev_f": 0.0, "samples": 0}
            stations[city_key] = entry

    return {
        "computed_at": datetime.utcnow().isoformat(),
        "window_days": days,
        "method": "gfs_seamless_vs_meteostat_station_obs",
        "note": ("bias_f = mean(forecast - observed) in each city's NATIVE unit "
                 "(F US / C intl); SUBTRACT from forecast mean before pricing."),
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
        u = m.get("unit", "F")
        skip = f"  [{m['skipped']}]" if m.get("skipped") else ""
        print(f"  {city:12s} high {m['high']['bias_f']:+.2f}{u} (n={m['high']['samples']:3})  "
              f"low {m['low']['bias_f']:+.2f}{u} (n={m['low']['samples']:3}){skip}")


if __name__ == "__main__":
    main()
