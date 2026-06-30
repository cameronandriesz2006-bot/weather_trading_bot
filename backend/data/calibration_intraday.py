"""FAITHFUL calibration of the LIVE intraday model vs the market, on resolved markets.

calibration_backfill.py priced every bucket with a flat static sigma — but that is NOT
what the bot runs. The live model is intraday: on the in-progress local day it uses the
empirical sigma CURVE, censors at the observed-so-far floor/ceiling, and re-centers on
observed + drift, all keyed to the station-local hour at trade time. This harness
reconstructs EXACTLY that — it builds a real EnsembleForecast and calls its real
probability_high/low_in_range with the reconstructed floor + local_hour — and compares it
to the market price AT THE SAME station-local hour, swept across the day.

Inputs, all reconstructable for past dates with no waiting:
  - outcome:       resolved Polymarket winning bucket (Gamma closed events).
  - forecast mean: archived blend (historical-forecast-api), bias-corrected (live bias file).
  - observed-so-far: running max/min up to local hour H from the station's BULK HOURLY
                     archive (meteostat) — the same obs the live floor reads, truncated to
                     "what was known by hour H" (quota-free bulk download, cached).
  - sigma / center / censoring: the REAL EnsembleForecast methods (intraday curve, drift,
                     clamped CDF) + the real >=16h(high)/>=10h(low) observed-floor gate.
  - market:        CLOB price history at the UTC instant of local hour H on the target day.

So at each hour H we ask: priced exactly as the bot would at that moment, does our YES
probability beat the market's? This is the real, like-for-like edge test.

Run:  PYTHONPATH=. venv/bin/python -m backend.data.calibration_intraday
      ... --hours 7,13,18  --market-sample 300  --months 5,6,7,8,9
"""
import argparse
import asyncio
import gzip
import json
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx

from backend.config import settings
from backend.data.weather import (CITY_CONFIG, METEOSTAT_STATION, EnsembleForecast,
                                   get_station_bias, is_bias_corrected,
                                   _HIGH_SET_LOCAL_HOUR, _LOW_SET_LOCAL_HOUR)
from backend.data.calibration_backfill import (fetch_resolved_events, extract_event,
                                               fetch_blend_means, brier, CLOB_PRICES_HISTORY)

HEADERS = {"User-Agent": "Mozilla/5.0"}
METEOSTAT_HOURLY = "https://d.meteostat.net/app/proxy/stations/hourly"
DEFAULT_HOURS = [7, 10, 13, 16, 18, 20]


def _c_to_native(c: float, unit: str) -> float:
    return (c * 9.0 / 5.0 + 32.0) if unit == "F" else c


async def fetch_hourly_obs(client, station: str, start: date, end: date,
                           tzname: str, unit: str) -> Dict[date, Dict[int, float]]:
    """Recent hourly obs from the Meteostat proxy -> {local_date: {local_hour: temp_native}}.
    With the tz param the proxy returns LOCAL timestamps, so date/hour parse directly. This
    is the same provider the live observed-floor reads; here we keep the running max/min up
    to each hour to recover 'what was observed by hour H' on a past day (the bulk archive
    lags ~months, so it can't cover recently-resolved markets)."""
    out: Dict[date, Dict[int, float]] = defaultdict(dict)
    from datetime import timedelta
    # The proxy caps the per-request span (~weeks), returning empty for long ranges, so
    # walk it in 10-day chunks and merge.
    chunks = []
    cur = start
    while cur <= end:
        nxt = min(cur + timedelta(days=10), end)
        chunks.append((cur, nxt))
        cur = nxt + timedelta(days=1)

    async def _one(s, e):
        try:
            r = await client.get(METEOSTAT_HOURLY, params={
                "station": station, "start": s.isoformat(), "end": e.isoformat(),
                "tz": tzname}, headers=HEADERS)
            if r.status_code >= 400:
                return []
            return r.json().get("data", []) or []
        except Exception:
            return []

    for rows in await asyncio.gather(*[_one(s, e) for s, e in chunks]):
        for row in rows:
            t, temp = row.get("time"), row.get("temp")
            if not t or temp is None:
                continue
            try:
                d = date(int(t[:4]), int(t[5:7]), int(t[8:10]))
                h = int(t[11:13])
            except (ValueError, IndexError):
                continue
            out[d][h] = _c_to_native(float(temp), unit)
    return out


def _observed_so_far(hours: Dict[int, float], H: int, metric: str) -> Optional[float]:
    vals = [t for h, t in hours.items() if h <= H]
    if not vals:
        return None
    return max(vals) if metric == "high" else min(vals)


def model_prob_at_hour(city, name, tdate, unit, metric, low, high,
                       fc_mean, obs_hours, H) -> Optional[float]:
    """The LIVE model's YES probability for this bucket, priced exactly as the bot would
    on the in-progress local day at station-local hour H."""
    if fc_mean is None:
        return None
    # observed-so-far floor/ceiling, with the live time-gate
    gate = _HIGH_SET_LOCAL_HOUR if metric == "high" else _LOW_SET_LOCAL_HOUR
    observed = _observed_so_far(obs_hours, H, metric) if (obs_hours and H >= gate) else None
    # real EnsembleForecast: members carry the (raw) forecast mean; corrected_mean subtracts
    # bias inside; on the in-progress day the intraday curve sets sigma (member std unused).
    ef = EnsembleForecast(city_key=city, city_name=name, target_date=tdate,
                          member_highs=[fc_mean["high"]] * 32 if fc_mean.get("high") is not None else [],
                          member_lows=[fc_mean["low"]] * 32 if fc_mean.get("low") is not None else [],
                          unit=unit, is_blend=True)
    if metric == "high":
        if not ef.member_highs:
            return None
        p = ef.probability_high_in_range(low, high, floor=observed, local_hour=H)
    else:
        if not ef.member_lows:
            return None
        p = ef.probability_low_in_range(low, high, ceiling=observed, local_hour=H)
    return max(0.01, min(0.99, p)), (observed is not None)


# standardized offsets with mean 0 and sample-stdev exactly 1, so synthesized members
# [mean + std*z for z in _ZK] reproduce a chosen mean AND std -> the real EnsembleForecast
# uses that std in its base (next-day) sigma. Lets us call the LIVE code, not re-derive it.
_ZK = [z / 1.2909944487 for z in (-1.5, -0.5, 0.5, 1.5)]


def nextday_prob(city, name, unit, metric, low, high, fc_mean, std) -> Optional[float]:
    """The LIVE model's YES probability for this bucket priced as the bot would ONE DAY
    AHEAD: local_hour=None and no observed floor -> the base sigma formula
    (max(std*BLEND_INFLATION, FLOOR) + 1*PER_LEAD) with the real bias-corrected center.
    We set target_date = today+1 so the real _effective_sigma computes lead_days = 1, and
    synthesize members so its raw std equals the measured live ensemble spread."""
    from datetime import timedelta
    m = fc_mean.get(metric)
    if m is None or std is None:
        return None
    members = [m + std * z for z in _ZK]
    ef = EnsembleForecast(city_key=city, city_name=name, target_date=date.today() + timedelta(days=1),
                          member_highs=members if metric == "high" else [],
                          member_lows=members if metric == "low" else [],
                          unit=unit, is_blend=True)
    if metric == "high":
        p = ef.probability_high_in_range(low, high, floor=None, local_hour=None)
    else:
        p = ef.probability_low_in_range(low, high, ceiling=None, local_hour=None)
    return max(0.01, min(0.99, p))


async def fetch_ensemble_std(client, cfg, models) -> Dict[str, float]:
    """Typical live ensemble member spread (native unit) per metric, from the last few days
    of the LIVE ensemble feed (the only window it retains) — the std the next-day base sigma
    needs. Mean of the recent daily pooled-member stdevs."""
    import statistics
    unit = "celsius" if cfg.get("unit") == "C" else "fahrenheit"
    out = {}
    for metric, field in (("high", "temperature_2m_max"), ("low", "temperature_2m_min")):
        try:
            r = await client.get("https://ensemble-api.open-meteo.com/v1/ensemble", params={
                "latitude": cfg["lat"], "longitude": cfg["lon"], "daily": field,
                "temperature_unit": unit, "timezone": "auto", "models": ",".join(models),
                "past_days": 7, "forecast_days": 1})
            d = r.json().get("daily", {})
            times = d.get("time", [])
            mk = [k for k in d if field in k]
            stds = []
            for i in range(len(times)):
                vals = [d[k][i] for k in mk if d[k] and i < len(d[k]) and d[k][i] is not None]
                if len(vals) > 1:
                    stds.append(statistics.pstdev(vals))
            out[metric] = statistics.mean(stds) if stds else None
        except Exception:
            out[metric] = None
    return out


# --------------------------------------------------------------------------- market
async def _fetch_day_history(client, token, tdate_iso, sem):
    """Price history spanning the day BEFORE settlement through the target day (one request);
    index by (day, hour) later — covers both the same-day and next-day market snapshots."""
    if not token:
        return None
    start = int(datetime.fromisoformat(tdate_iso).replace(tzinfo=timezone.utc).timestamp())
    async with sem:
        try:
            r = await client.get(CLOB_PRICES_HISTORY, params={
                "market": token, "startTs": start - 30 * 3600,
                "endTs": start + 30 * 3600, "fidelity": 30})
            if r.status_code >= 400:
                return None
            return r.json().get("history", []) or None
        except Exception:
            return None


def _price_at(hist, tdate_iso, tz: ZoneInfo, H: int, tol_h=4) -> Optional[float]:
    """Market YES price nearest to local hour H on the target day (within tol_h)."""
    if not hist:
        return None
    target = int(datetime.fromisoformat(tdate_iso).replace(hour=0, tzinfo=tz).timestamp()) + H * 3600
    best = min(hist, key=lambda p: abs(p.get("t", 0) - target))
    if abs(best.get("t", 0) - target) > tol_h * 3600:
        return None
    p = best.get("p")
    return float(p) if p is not None else None


async def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cities", type=str, default=None)
    ap.add_argument("--models", type=str, default=None)
    ap.add_argument("--hours", type=str, default=None, help="local hours to evaluate (default 7,10,13,16,18,20)")
    ap.add_argument("--months", type=str, default=None, help="restrict to these target months (e.g. 5,6,7,8,9)")
    ap.add_argument("--market-sample", type=int, default=300)
    ap.add_argument("--dump", type=str, default=None, help="write joined (city,H,model,market,won) records to this JSONL for offline bootstrap")
    args = ap.parse_args()

    cities = ([c.strip() for c in args.cities.split(",")] if args.cities
              else [c.strip() for c in settings.WEATHER_CITIES.split(",") if c.strip()])
    models = ([m.strip() for m in args.models.split(",")] if args.models
              else [m.strip() for m in settings.WEATHER_BLEND_MODELS.split(",") if m.strip()])
    hours = ([int(h) for h in args.hours.split(",")] if args.hours else DEFAULT_HOURS)
    months = set(int(m) for m in args.months.split(",")) if args.months else None
    print(f"cities={cities}\nmodels={models}\nhours(local)={hours}  months={sorted(months) if months else 'all'}\n"
          f"intraday_enabled={settings.WEATHER_INTRADAY_SIGMA_ENABLED} blend={settings.WEATHER_BLEND_ENABLED}")

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
            if months and tdate.month not in months:
                continue
            raw.append((city, metric, tdate, buckets))
            dates_by_city[city].append(tdate)
        print(f"{len(raw)} cleanly-resolved in-scope events")

        # forecasts (one fetch per city), hourly obs + tz, and the live ensemble spread
        means, obs_days, tzs, stds = {}, {}, {}, {}
        for city, dts in dates_by_city.items():
            cfg = CITY_CONFIG.get(city)
            st = METEOSTAT_STATION.get(city)
            if not cfg:
                continue
            from datetime import timedelta
            means[city] = await fetch_blend_means(client, cfg, models, min(dts) - timedelta(days=1), max(dts) + timedelta(days=1))
            tzname = cfg.get("tz") or "UTC"
            tzs[city] = ZoneInfo(tzname)
            stds[city] = await fetch_ensemble_std(client, cfg, models)
            if st:
                obs_days[city] = await fetch_hourly_obs(
                    client, st, min(dts) - timedelta(days=1), max(dts) + timedelta(days=1),
                    tzname, cfg.get("unit", "F"))
        print("live ensemble spread (next-day base sigma input):  " +
              "  ".join(f"{c}:{(stds[c].get('high') or 0):.1f}/{(stds[c].get('low') or 0):.1f}" for c in stds))

    # build per-(bucket, hour) model predictions
    records = []  # dict per (bucket, hour)
    for city, metric, tdate, buckets in raw:
        cfg = CITY_CONFIG.get(city, {})
        unit = cfg.get("unit", "F")
        fc = means.get(city, {}).get(tdate.isoformat())
        if not fc:
            continue
        obs_hours = obs_days.get(city, {}).get(tdate)
        corrected = is_bias_corrected(city, metric)
        for b in buckets:
            for H in hours:
                res = model_prob_at_hour(city, city, tdate, unit, metric,
                                         b["low"], b["high"], fc, obs_hours, H)
                if res is None:
                    continue
                p, had_floor = res
                records.append({"city": city, "metric": metric, "date": tdate.isoformat(),
                                "unit": unit, "H": H, "model": p, "won": b["won"],
                                "corrected": corrected, "yes_token": b["yes_token"],
                                "had_floor": had_floor})
    print(f"{len(records)} (bucket,hour) model predictions")

    # market prices: sample events, fetch each token's day history once
    ev_keys = sorted(set((r["city"], r["date"], r["metric"]) for r in records))
    stride = max(1, len(ev_keys) // args.market_sample)
    keep = set(ev_keys[::stride])
    tokens = sorted(set(r["yes_token"] for r in records
                        if (r["city"], r["date"], r["metric"]) in keep and r["yes_token"]))
    print(f"\nfetching market history for {len(tokens)} tokens ({len(keep)} events)...")
    sem = asyncio.Semaphore(12)
    async with httpx.AsyncClient(timeout=30.0, headers=HEADERS) as client:
        # map token -> its (date) via records
        tok_date = {r["yes_token"]: r["date"] for r in records if r["yes_token"]}
        hists = await asyncio.gather(*[_fetch_day_history(client, t, tok_date[t], sem) for t in tokens])
    hist_by_token = dict(zip(tokens, hists))

    if args.dump:
        with open(args.dump, "w") as fh:
            for r in records:
                if (r["city"], r["date"], r["metric"]) not in keep:
                    continue
                mp = _price_at(hist_by_token.get(r["yes_token"]), r["date"], tzs[r["city"]], r["H"])
                if mp is None:
                    continue
                fh.write(json.dumps({"city": r["city"], "metric": r["metric"], "date": r["date"],
                                     "H": r["H"], "model": r["model"], "market": mp,
                                     "won": 1 if r["won"] else 0, "had_floor": r["had_floor"],
                                     "contested": 1 if 0.10 < mp < 0.90 else 0}) + "\n")
        print(f"  [dumped joined records -> {args.dump}]")

    # join + report per hour
    print(f"\n{'='*78}\nLIVE INTRADAY MODEL vs MARKET — Brier by station-local hour\n"
          f"(model = exact live pipeline: intraday sigma + observed floor + drift)\n{'='*78}")
    print(f"  {'hour':>4} {'n_buckets':>9} {'%w/floor':>8} {'market':>8} {'MODEL':>8} {'verdict':>9}")
    for H in hours:
        rs = [r for r in records if r["H"] == H and (r["city"], r["date"], r["metric"]) in keep]
        joined = []
        for r in rs:
            hist = hist_by_token.get(r["yes_token"])
            mp = _price_at(hist, r["date"], tzs[r["city"]], H)
            if mp is not None:
                joined.append((r, mp))
        if not joined:
            print(f"  {H:>4}   (no market prices)")
            continue
        # contested only (where trading happens)
        con = [(r, mp) for r, mp in joined if 0.10 < mp < 0.90]
        base = con if con else joined
        mkt = brier([(mp, 1 if r["won"] else 0) for r, mp in base])
        mdl = brier([(r["model"], 1 if r["won"] else 0) for r, mp in base])
        floorpct = 100.0 * sum(r["had_floor"] for r, _ in base) / len(base)
        verdict = "MODEL" if mdl < mkt else "market"
        print(f"  {H:>4} {len(base):>9} {floorpct:>7.0f}% {mkt:>8.4f} {mdl:>8.4f} {verdict:>9}  (contested)")

    # per-city at the best (lowest model-Brier) hour
    print(f"\n{'='*78}\nper-city, CONTESTED, at each city's BEST hour (model vs market)\n{'='*78}")
    by_city = defaultdict(list)
    for r in records:
        if (r["city"], r["date"], r["metric"]) in keep:
            hist = hist_by_token.get(r["yes_token"])
            mp = _price_at(hist, r["date"], tzs[r["city"]], r["H"])
            if mp is not None and 0.10 < mp < 0.90:
                by_city[(r["city"], r["H"])].append((r, mp))
    best_by_city = {}
    for (city, H), sub in by_city.items():
        if len(sub) < 15:
            continue
        mdl = brier([(r["model"], 1 if r["won"] else 0) for r, _ in sub])
        mkt = brier([(mp, 1 if r["won"] else 0) for r, mp in sub])
        cur = best_by_city.get(city)
        if cur is None or mdl < cur[1]:
            best_by_city[city] = (H, mdl, mkt, len(sub))
    for city in sorted(best_by_city):
        H, mdl, mkt, n = best_by_city[city]
        verdict = "MODEL" if mdl < mkt else "market"
        print(f"  {city:12} bestH={H:>2} n={n:>4}  market={mkt:.4f}  model={mdl:.4f}  -> {verdict} better")

    # ===== NEXT-DAY regime (caveat #2): priced ~24h ahead, base sigma, no floor/curve =====
    print(f"\n{'='*78}\nNEXT-DAY MODEL vs MARKET — priced ~24h before settlement\n"
          f"(base sigma = max(std*{settings.WEATHER_BLEND_SIGMA_INFLATION}, "
          f"{settings.WEATHER_SIGMA_FLOOR_F}) + {settings.WEATHER_SIGMA_PER_LEAD_DAY_F}; "
          f"std = live ensemble spread; market price @ noon the day before)\n{'='*78}")
    nd = []  # (record-ish, market_price)
    for city, metric, tdate, buckets in raw:
        if (city, tdate.isoformat(), metric) not in keep:
            continue
        cfg = CITY_CONFIG.get(city, {})
        unit = cfg.get("unit", "F")
        fc = means.get(city, {}).get(tdate.isoformat())
        if not fc:
            continue
        corrected = is_bias_corrected(city, metric)
        for b in buckets:
            p = nextday_prob(city, city, unit, metric, b["low"], b["high"], fc,
                             (stds.get(city) or {}).get(metric))
            if p is None or not b["yes_token"]:
                continue
            hist = hist_by_token.get(b["yes_token"])
            mp = _price_at(hist, tdate.isoformat(), tzs[city], -12)   # noon prior day
            if mp is None:
                continue
            nd.append({"city": city, "metric": metric, "won": b["won"],
                       "corrected": corrected, "model": p, "mkt": mp})
    if nd:
        def _rep(sub, name):
            if not sub:
                return
            mkt = brier([(r["mkt"], 1 if r["won"] else 0) for r in sub])
            mdl = brier([(r["model"], 1 if r["won"] else 0) for r in sub])
            v = "MODEL" if mdl < mkt else "market"
            print(f"  {name:30} n={len(sub):>4}  market={mkt:.4f}  model={mdl:.4f}  -> {v} better")
        _rep(nd, "ALL")
        _rep([r for r in nd if 0.10 < r["mkt"] < 0.90], "CONTESTED (where we trade)")
        _rep([r for r in nd if r["corrected"]], "bias-corrected cities")
        _rep([r for r in nd if not r["corrected"]], "uncorrected cities")
        print("  per-city (contested):")
        bc = defaultdict(list)
        for r in nd:
            if 0.10 < r["mkt"] < 0.90:
                bc[r["city"]].append(r)
        for city in sorted(bc):
            if len(bc[city]) < 15:
                continue
            _rep(bc[city], f"  {city}")
    else:
        print("  (no next-day market snapshots available)")


if __name__ == "__main__":
    asyncio.run(main())
