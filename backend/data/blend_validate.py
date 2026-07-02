"""Offline validation harness for the multi-model blend (config.WEATHER_BLEND_ENABLED).

RUN THIS BEFORE ENABLING THE BLEND LIVE. Two checks, both offline (no trading sim, no
DB) — they answer the two open questions the build left:

 1. SKILL — does the equal-weight GFS+ECMWF+ICON blend beat GFS-only on daily high/low
    accuracy vs the station thermometer (Meteostat), over the cities we ACTUALLY trade
    (config.WEATHER_CITIES)? Reports bias + de-biased RMSE (the correctable-bias-removed
    error) per metric. Expectation from the 2026-06-20 backtest: blend ~10% lower
    de-biased RMSE on highs and ~half the cold bias.

 2. DISPERSION — is the 3-model ensemble pool better-dispersed than GEFS alone, and what
    WEATHER_BLEND_SIGMA_INFLATION calibrates it? This is the ONE open knob the build left
    at a placeholder (1.3). Reports the spread-skill ratio (ensemble std / forecast RMSE;
    ~1.0 = calibrated) for GEFS vs the pool and a recommended inflation.

Usage:
  PYTHONPATH=. venv/bin/python -m backend.data.blend_validate
  PYTHONPATH=. venv/bin/python -m backend.data.blend_validate --days 90 --cities nyc,london

Caveats: the dispersion sample is bounded by the ensemble-api archive depth (~5 weeks),
so treat the inflation as a starting point and confirm coverage. Re-run in the season
you'll trade — skill/dispersion are regime-dependent.
"""
import argparse
import asyncio
import math
import statistics
from datetime import date, timedelta

import httpx

from backend.config import settings
from backend.data.weather import CITY_CONFIG, METEOSTAT_STATION
from backend.data.bias_backfill import _fetch_obs_daily_iem

HIST = "https://historical-forecast-api.open-meteo.com/v1/forecast"
ENS = "https://ensemble-api.open-meteo.com/v1/ensemble"
MET = "https://d.meteostat.net/app/proxy/stations/daily"
MH = {"User-Agent": "Mozilla/5.0"}


def _c2f(c):
    return None if c is None else c * 9 / 5 + 32


def _rmse(xs):
    return math.sqrt(sum(x * x for x in xs) / len(xs)) if xs else float("nan")


async def _hist(cl, lat, lon, model, start, end):
    r = await cl.get(HIST, params={
        "latitude": lat, "longitude": lon, "start_date": start.isoformat(),
        "end_date": end.isoformat(), "daily": "temperature_2m_max,temperature_2m_min",
        "temperature_unit": "fahrenheit", "timezone": "auto", "models": model})
    r.raise_for_status()
    d = r.json().get("daily", {})
    t = d.get("time", []) or []
    hi = d.get("temperature_2m_max", []) or []
    lo = d.get("temperature_2m_min", []) or []
    return {t[i]: {"high": hi[i] if i < len(hi) else None, "low": lo[i] if i < len(lo) else None}
            for i in range(len(t))}


OBS_MODE = "meteostat"   # set to "iem" by --obs iem (settlement-grade METAR obs)


async def _obs_for(cl, cfg, st, start, end):
    """Realized daily obs for one city: settlement-grade IEM METARs (per-ob WU-rounded
    F, the number the market settles on) when OBS_MODE='iem' and the city has an NWS
    station; else the Meteostat daily aggregate (legacy reference)."""
    icao = cfg.get("nws_station")
    if OBS_MODE == "iem" and icao and cfg.get("unit", "F") == "F":
        return await _fetch_obs_daily_iem(cl, icao, start, end, cfg.get("tz") or "UTC")
    return await _obs(cl, st, start, end)


async def _obs(cl, st, start, end):
    r = await cl.get(MET, params={"station": st, "start": start.isoformat(), "end": end.isoformat()}, headers=MH)
    r.raise_for_status()
    return {x["date"][:10]: {"high": _c2f(x.get("tmax")), "low": _c2f(x.get("tmin"))}
            for x in r.json().get("data", []) or [] if x.get("date")}


async def _ens(cl, lat, lon, model, past_days):
    r = await cl.get(ENS, params={
        "latitude": lat, "longitude": lon, "daily": "temperature_2m_max",
        "temperature_unit": "fahrenheit", "timezone": "auto",
        "models": model, "past_days": past_days, "forecast_days": 1})
    r.raise_for_status()
    d = r.json().get("daily", {})
    t = d.get("time", []) or []
    out = {}
    for i, day in enumerate(t):
        mem = [v[i] for k, v in d.items()
               if "temperature_2m_max" in k and isinstance(v, list) and i < len(v) and v[i] is not None]
        if mem:
            out[day] = mem
    return out


async def run_skill(cl, cities, models, days):
    end = date.today()
    start = end - timedelta(days=days + 2)
    res = {"gfs": {"high": [], "low": []}, "blend": {"high": [], "low": []}}      # de-biased errors
    bias = {"gfs": {"high": [], "low": []}, "blend": {"high": [], "low": []}}     # raw errors
    for ck in cities:
        cfg = CITY_CONFIG.get(ck)
        st = METEOSTAT_STATION.get(ck)
        if not cfg or not st:
            continue
        per = {m: await _hist(cl, cfg["lat"], cfg["lon"], m, start, end) for m in models}
        obs = await _obs_for(cl, cfg, st, start, end)
        for metric in ("high", "low"):
            g, b = [], []
            for day, ov in obs.items():
                a = ov.get(metric)
                if a is None:
                    continue
                gv = per.get("gfs_seamless", {}).get(day, {}).get(metric)
                bv = [per[m].get(day, {}).get(metric) for m in models]
                if gv is not None:
                    g.append(gv - a)
                if all(v is not None for v in bv):
                    b.append(sum(bv) / len(bv) - a)
            for tag, errs in (("gfs", g), ("blend", b)):
                if len(errs) >= 5:                       # de-bias per-city before pooling
                    mb = statistics.mean(errs)
                    res[tag][metric] += [e - mb for e in errs]
                    bias[tag][metric] += errs
    return res, bias


async def run_dispersion(cl, cities, models, past_days):
    g_std, g_err, p_std, p_err = [], [], [], []
    g_cov = g_n = p_cov = p_n = 0
    for ck in cities:
        cfg = CITY_CONFIG.get(ck)
        st = METEOSTAT_STATION.get(ck)
        if not cfg or not st:
            continue
        per = {m: await _ens(cl, cfg["lat"], cfg["lon"], m, past_days) for m in models}
        obs = await _obs_for(cl, cfg, st, date.today() - timedelta(days=past_days + 2), date.today())
        for day, ov in obs.items():
            a = ov.get("high")
            if a is None or any(day not in per[m] for m in models):
                continue
            gefs = per["gfs_seamless"][day]
            pool = [v for m in models for v in per[m][day]]
            g_std.append(statistics.pstdev(gefs)); g_err.append(statistics.mean(gefs) - a)
            p_std.append(statistics.pstdev(pool)); p_err.append(statistics.mean(pool) - a)
            g_n += 1; p_n += 1
            if min(gefs) <= a <= max(gefs): g_cov += 1
            if min(pool) <= a <= max(pool): p_cov += 1
    return {"g_std": g_std, "g_err": g_err, "p_std": p_std, "p_err": p_err,
            "g_cov": g_cov, "p_cov": p_cov, "n": g_n}


async def main():
    ap = argparse.ArgumentParser(description="Validate the multi-model blend offline.")
    ap.add_argument("--days", type=int, default=60, help="skill window (days, default 60)")
    ap.add_argument("--past-days", type=int, default=35, help="dispersion window (ensemble archive, default 35)")
    ap.add_argument("--cities", type=str, default=None, help="comma list (default = active WEATHER_CITIES)")
    ap.add_argument("--skip-dispersion", action="store_true")
    ap.add_argument("--obs", choices=("meteostat", "iem"), default="meteostat",
                    help="'iem' = settlement-grade METAR obs for US cities")
    args = ap.parse_args()
    global OBS_MODE
    OBS_MODE = args.obs

    cities = ([c.strip() for c in args.cities.split(",")] if args.cities
              else [c.strip() for c in settings.WEATHER_CITIES.split(",") if c.strip()])
    models = [m.strip() for m in settings.WEATHER_BLEND_MODELS.split(",") if m.strip()]
    print(f"cities={cities}\nmodels={models}\n")

    async with httpx.AsyncClient(timeout=45) as cl:
        print(f"===== 1) SKILL vs station obs ({args.days}d, de-biased RMSE = correctable-bias-removed error) =====")
        res, bias = await run_skill(cl, cities, models, args.days)
        for metric in ("high", "low"):
            print(f"  --- {metric.upper()} ---")
            for tag in ("gfs", "blend"):
                e = res[tag][metric]; rb = bias[tag][metric]
                if not e:
                    print(f"    {tag:<6} (no data)"); continue
                print(f"    {tag:<6} n={len(rb):<4} bias={statistics.mean(rb):+.2f}  de-biased_RMSE={_rmse(e):.2f}")
            if res["gfs"][metric] and res["blend"][metric]:
                d = _rmse(res["gfs"][metric]) - _rmse(res["blend"][metric])
                print(f"    -> blend {'BEATS' if d > 0 else 'LOSES TO'} GFS by {d:+.2f} de-biased RMSE")

        if not args.skip_dispersion:
            print(f"\n===== 2) DISPERSION (HIGH, ensemble archive ~{args.past_days}d) =====")
            dz = await run_dispersion(cl, cities, models, args.past_days)
            if dz["n"]:
                g_ratio = statistics.mean(dz["g_std"]) / _rmse(dz["g_err"])
                p_ratio = statistics.mean(dz["p_std"]) / _rmse(dz["p_err"])
                print(f"  n={dz['n']} city-days. spread-skill ratio = mean(ens std)/RMSE (1.0 = calibrated):")
                print(f"    GEFS alone   ratio={g_ratio:.2f}  range-coverage={dz['g_cov']}/{dz['n']}={dz['g_cov']/dz['n']:.0%}")
                print(f"    3-model POOL ratio={p_ratio:.2f}  range-coverage={dz['p_cov']}/{dz['n']}={dz['p_cov']/dz['n']:.0%}")
                rec = max(1.0, min(2.5, 1.0 / p_ratio if p_ratio > 0 else 1.3))
                print(f"\n  RECOMMENDED WEATHER_BLEND_SIGMA_INFLATION ~ {rec:.2f}  (= 1/pool-ratio, clamped [1.0,2.5];")
                print(f"    the floor still adds on top — confirm coverage rises toward ~90% before trusting it).")
            else:
                print("  (no aligned ensemble+obs days — archive too shallow or quota hit; retry later)")
    print("\nDone. Enable only after: blend beats GFS on skill above AND the bias table is re-fit")
    print("(`python -m backend.data.bias_backfill --blend`) AND the inflation above is set.")


if __name__ == "__main__":
    asyncio.run(main())
