"""Edge-2 OUT-OF-SAMPLE + lead-correct backtest — the go/no-go.

Two independent checks the original `edge2_backtest` could not make:

  (1) FORECAST HONESTY (is it lead-correct, or look-ahead?). The strategy's forecast mean
      comes from the historical-forecast-api (archived deterministic runs). If that were
      ANALYSIS-grade (reanalysis), it would have peeked at the outcome and the edge would be
      fake. We test it directly per time-half: forecast daily-max vs ERA5 reanalysis truth.
      A genuine ~same-day forecast has ~1-2.5F RMSE; an analysis leak would be ~0F.

  (2) OUT-OF-SAMPLE TIME SPLIT. The denver/chicago H=16 cell was DISCOVERED on the full
      Feb-Jun window, so the cell choice could be cherry-picked. We split the resolved events
      at --split, evaluate each half INDEPENDENTLY (each is out-of-sample w.r.t. the other),
      and check the edge shows up in BOTH halves — P&L (flat stake, event-bootstrap CI) AND
      model-vs-market Brier on contested buckets. A period-specific fluke wins one half and
      dies in the other. Plus a select-on-H1 / confirm-on-H2 cell ranking.

Identical pricing/gates/sizing to edge2_backtest (model_prob_at_hour = real EnsembleForecast
intraday floor+sigma+drift; calculate_edge/calculate_kelly_size; net-edge + rel-spread + entry
+ market-gap gates; observed-floor = post-extreme). Flat stake so the two halves compare on
skill, not compounding.

Run: PYTHONPATH=. venv/bin/python -m backend.data.edge2_oos_backtest \
       --cities denver,chicago --hours 13,16,18,20 --months 2,3,4,5,6 --split 2026-05-01
"""
import argparse
import asyncio
import random
import statistics
from collections import defaultdict
from datetime import date, timedelta
from typing import Dict, List, Optional

import httpx

from backend.config import settings
from backend.data.weather import CITY_CONFIG, METEOSTAT_STATION, get_station_bias
from backend.data.calibration_backfill import (fetch_resolved_events, extract_event,
                                               fetch_blend_means, brier, GAMMA)
from backend.data.calibration_intraday import (fetch_hourly_obs, model_prob_at_hour,
                                               _fetch_day_history, _price_at)
from backend.core.sizing import calculate_edge, calculate_kelly_size
from backend.data.edge2_backtest import bucket_center, _c_scale

HDR = {"User-Agent": "Mozilla/5.0"}
ERA5_URL = "https://archive-api.open-meteo.com/v1/archive"
random.seed(20260630)


async def _era5_daily_max(client, lat, lon, s: date, e: date, temp_unit: str) -> Dict[str, float]:
    """ERA5 reanalysis daily max (the closest thing to ground truth) per ISO date."""
    r = await client.get(ERA5_URL, params={
        "latitude": lat, "longitude": lon, "start_date": s.isoformat(), "end_date": e.isoformat(),
        "daily": "temperature_2m_max", "temperature_unit": temp_unit, "timezone": "auto"})
    r.raise_for_status()
    d = r.json().get("daily", {})
    return {t: v for t, v in zip(d.get("time", []), d.get("temperature_2m_max", [])) if v is not None}


async def fetch_both_ends(client) -> List[dict]:
    """Resolved daily-temperature events from BOTH ends of the feed. Gamma 422s past offset
    ~2000, and there are so many daily-temp markets that newest-first only reaches back to
    May; oldest-first only reaches Feb-Apr. Paginating BOTH directions and unioning by id
    recovers the full Feb-Jun span the OOS split needs (the offset cap only loses the middle)."""
    seen, out = set(), []
    for asc in ("false", "true"):
        for off in range(0, 6000, 100):
            r = await client.get(GAMMA, params={"closed": "true", "limit": 100, "offset": off,
                                                "tag_slug": "daily-temperature", "order": "endDate",
                                                "ascending": asc})
            if r.status_code >= 400:
                break
            page = r.json()
            if not page:
                break
            for e in page:
                k = e.get("id") or e.get("slug")
                if k and k not in seen:
                    seen.add(k)
                    out.append(e)
            if len(page) < 100:
                break
    return out


def _bootstrap_ci(per_event_vals: List[float], n_boot=5000):
    evs = list(per_event_vals)
    if not evs:
        return (0.0, 0.0)
    n = len(evs)
    boot = sorted(sum(evs[random.randrange(n)] for _ in range(n)) for _ in range(n_boot))
    return (boot[int(0.025 * n_boot)], boot[int(0.975 * n_boot)])


async def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cities", type=str, default="denver,chicago")
    ap.add_argument("--hours", type=str, default="13,16,18,20")
    ap.add_argument("--months", type=str, default="2,3,4,5,6")
    ap.add_argument("--split", type=str, default="2026-05-01",
                    help="events before this date = H1 (early), on/after = H2 (held-out late)")
    ap.add_argument("--focus-hour", type=int, default=16, help="the proven cell hour to spotlight")
    args = ap.parse_args()

    cities = [c.strip() for c in args.cities.split(",")]
    models = [m.strip() for m in settings.WEATHER_BLEND_MODELS.split(",") if m.strip()]
    hours = [int(h) for h in args.hours.split(",")]
    months = set(int(m) for m in args.months.split(","))
    split = date.fromisoformat(args.split)
    print(f"cities={cities}  hours={hours}  months={sorted(months)}  split={split}  "
          f"focus H={args.focus_hour}\nmodels={models}  edge_gate={settings.WEATHER_MIN_EDGE_THRESHOLD}  "
          f"spread={settings.WEATHER_DEFAULT_SPREAD}  kelly={settings.KELLY_FRACTION}/{settings.KELLY_MAX_TRADE_FRACTION}")

    def half(d: date) -> str:
        return "H1" if d < split else "H2"

    # ---- resolved events + forecasts/obs (reuse the calibration reconstruction) ----
    async with httpx.AsyncClient(timeout=60.0, headers=HDR) as client:
        events = await fetch_both_ends(client)
        raw, dts = [], defaultdict(list)
        for e in events:
            ex = extract_event(e)
            if not ex:
                continue
            city, metric, tdate, buckets = ex
            if city not in cities or tdate >= date.today() or tdate.month not in months:
                continue
            raw.append((city, metric, tdate, buckets))
            dts[city].append(tdate)
        nh1 = sum(1 for r in raw if half(r[2]) == "H1")
        print(f"{len(raw)} resolved in-scope events  (H1={nh1}, H2={len(raw)-nh1})")

        means, obs_days, tzs, era5 = {}, {}, {}, {}
        for city, d in dts.items():
            cfg, st = CITY_CONFIG.get(city), METEOSTAT_STATION.get(city)
            if not cfg:
                continue
            lo, hi = min(d) - timedelta(days=1), max(d) + timedelta(days=1)
            tzs[city] = cfg.get("tz") or "UTC"
            unit = cfg.get("unit", "F")
            means[city] = await fetch_blend_means(client, cfg, models, lo, hi)
            era5[city] = await _era5_daily_max(client, cfg["lat"], cfg["lon"], lo, hi,
                                               "celsius" if unit == "C" else "fahrenheit")
            if st:
                obs_days[city] = await fetch_hourly_obs(client, st, lo, hi, cfg.get("tz") or "UTC", unit)

    # ===== (1) FORECAST HONESTY: forecast blend vs ERA5 truth, per half =====
    print(f"\n{'='*74}\n(1) FORECAST HONESTY — blend daily-max vs ERA5 truth (lead-correct check)\n"
          f"    genuine forecast ~1-2.5F RMSE; ~0F would mean analysis-grade LOOK-AHEAD\n{'='*74}")
    for city in cities:
        for hv in ("H1", "H2"):
            diffs = []
            for city2, metric, tdate, _ in raw:
                if city2 != city or metric != "high" or half(tdate) != hv:
                    continue
                f = means.get(city, {}).get(tdate.isoformat())
                t = era5.get(city, {}).get(tdate.isoformat())
                if f and f.get("high") is not None and t is not None:
                    diffs.append(f["high"] - t)
            if diffs:
                rmse = (sum(x * x for x in diffs) / len(diffs)) ** 0.5
                tag = "GENUINE" if rmse > 0.8 else "** SUSPICIOUS (analysis?) **"
                print(f"  {city:9} {hv}  n={len(diffs):>3}  RMSE={rmse:.2f}F  "
                      f"bias={statistics.mean(diffs):+.2f}F  -> {tag}")

    # ---- market prices ----
    tok_date = {b["yes_token"]: tdate.isoformat()
                for city, metric, tdate, buckets in raw for b in buckets if b["yes_token"]}
    toks = list(tok_date)
    print(f"\nfetching market history for {len(toks)} bucket tokens...")
    sem = asyncio.Semaphore(12)
    async with httpx.AsyncClient(timeout=30.0, headers=HDR) as client:
        hists = await asyncio.gather(*[_fetch_day_history(client, t, tok_date[t], sem) for t in toks])
    hist = dict(zip(toks, hists))

    # ---- build cells (for Brier) + signals (for P&L), tagged by half ----
    from zoneinfo import ZoneInfo
    cells = []     # every evaluated (bucket,hour): for model-vs-market Brier
    signals = []   # actionable trades: for P&L
    for city, metric, tdate, buckets in raw:
        cfg = CITY_CONFIG.get(city, {})
        unit = cfg.get("unit", "F")
        fc = means.get(city, {}).get(tdate.isoformat())
        if not fc or fc.get(metric) is None:
            continue
        obs_hours = obs_days.get(city, {}).get(tdate)
        corrected_mean = fc[metric] - get_station_bias(city, metric)
        tz = ZoneInfo(tzs[city])
        gap_tol = settings.WEATHER_MAX_MARKET_GAP_F * _c_scale(unit)
        for H in hours:
            mkt, num, den = {}, 0.0, 0.0
            for b in buckets:
                p = _price_at(hist.get(b["yes_token"]), tdate.isoformat(), tz, H) if b["yes_token"] else None
                if p is None:
                    continue
                mkt[id(b)] = p
                c = bucket_center(b)
                if c is not None:
                    num += c * p
                    den += p
            if not mkt:
                continue
            event_mkt_mean = (num / den) if den > 0 else None
            for b in buckets:
                if id(b) not in mkt:
                    continue
                res = model_prob_at_hour(city, city, tdate, unit, metric, b["low"], b["high"], fc, obs_hours, H)
                if res is None:
                    continue
                model_p, _floor = res
                market_yes = mkt[id(b)]
                cells.append({"city": city, "H": H, "half": half(tdate), "date": tdate, "metric": metric,
                              "model": model_p, "market": market_yes, "won": 1 if b["won"] else 0,
                              "contested": 0.10 < market_yes < 0.90})
                # gates (faithful to edge2_backtest / passes_threshold)
                edge, dir_raw = calculate_edge(model_p, market_yes)
                direction = "yes" if dir_raw == "up" else "no"
                side_mid = market_yes if direction == "yes" else (1.0 - market_yes)
                if side_mid <= 0:
                    continue
                entry = min(0.999, side_mid + settings.WEATHER_DEFAULT_SPREAD / 2.0)
                net_edge = edge - (settings.WEATHER_DEFAULT_SPREAD / 2.0 + settings.WEATHER_FEE_RATE)
                rel_spread = settings.WEATHER_DEFAULT_SPREAD / side_mid
                if net_edge < settings.WEATHER_MIN_EDGE_THRESHOLD: continue
                if rel_spread > settings.WEATHER_MAX_REL_SPREAD: continue
                if entry > settings.WEATHER_MAX_ENTRY_PRICE: continue
                gap = abs(corrected_mean - event_mkt_mean) if event_mkt_mean is not None else None
                if settings.WEATHER_MARKET_GAP_ENABLED and gap is not None and gap > gap_tol: continue
                won = b["won"] if direction == "yes" else (not b["won"])
                signals.append({"city": city, "H": H, "half": half(tdate), "date": tdate, "metric": metric,
                                "direction": direction, "model_p": model_p, "net_edge": net_edge,
                                "entry": entry, "win": won})

    # ===== (2a) MODEL vs MARKET BRIER, contested, per half — the focus cell + pooled afternoon =====
    AFT = [h for h in hours if h >= 13]
    def brier_pair(sub):
        if len(sub) < 6:
            return None
        mdl = brier([(c["model"], c["won"]) for c in sub])
        mkt = brier([(c["market"], c["won"]) for c in sub])
        return mdl, mkt, len(sub)
    cstr = "+".join(cities)
    print(f"\n{'='*74}\n(2a) MODEL vs MARKET Brier — contested buckets, per time-half (lower=better)\n{'='*74}")
    for label, pred in ((f"{cstr}  H={args.focus_hour} (pooled)",
                         lambda c: c["H"] == args.focus_hour),
                        (f"{cstr}  afternoon (H>=13) pooled", lambda c: c["H"] in AFT)):
        print(f"  {label}")
        for hv in ("H1", "H2"):
            sub = [c for c in cells if c["contested"] and c["half"] == hv and pred(c)]
            r = brier_pair(sub)
            if r is None:
                print(f"      {hv}  (n<6, skip)")
                continue
            mdl, mkt, n = r
            v = "MODEL wins" if mdl < mkt else "market wins"
            print(f"      {hv}  n={n:>4}  market={mkt:.4f}  model={mdl:.4f}  gap={mkt-mdl:+.4f}  -> {v}")

    # ===== (2b) FLAT-STAKE P&L per half (afternoon, all focus cities), bootstrap CI =====
    print(f"\n{'='*74}\n(2b) FLAT-STAKE P&L per half — afternoon signals, $10k fixed stake base\n{'='*74}")
    for hv in ("H1", "H2"):
        sub = [s for s in signals if s["half"] == hv and s["H"] in AFT]
        per_event = defaultdict(float)
        ntr = nwin = 0
        for s in sub:
            kmp = s["entry"] if s["direction"] == "yes" else (1.0 - s["entry"])
            stake = calculate_kelly_size(edge=s["net_edge"], probability=s["model_p"], market_price=kmp,
                                         direction=("up" if s["direction"] == "yes" else "down"),
                                         bankroll=settings.INITIAL_BANKROLL)
            if stake <= 0:
                continue
            shares = stake / s["entry"]
            pnl = (shares - stake) if s["win"] else (-stake)
            per_event[(s["city"], s["date"], s["metric"])] += pnl
            ntr += 1; nwin += 1 if s["win"] else 0
        total = sum(per_event.values())
        ci = _bootstrap_ci(list(per_event.values()))
        verdict = "POSITIVE" if ci[0] > 0 else ("NEGATIVE" if ci[1] < 0 else "spans 0 (inconclusive)")
        print(f"  {hv}  trades={ntr:>3}  win={nwin/ntr if ntr else 0:.0%}  P&L=${total:>8,.0f}  "
              f"CI=[${ci[0]:,.0f}, ${ci[1]:,.0f}]  -> {verdict}")
        # per focus-hour cell within the half
        for city in cities:
            cs = [s for s in sub if s["city"] == city and s["H"] == args.focus_hour]
            if cs:
                pe = defaultdict(float)
                for s in cs:
                    kmp = s["entry"] if s["direction"] == "yes" else (1.0 - s["entry"])
                    stake = calculate_kelly_size(edge=s["net_edge"], probability=s["model_p"], market_price=kmp,
                                                 direction=("up" if s["direction"] == "yes" else "down"),
                                                 bankroll=settings.INITIAL_BANKROLL)
                    if stake <= 0: continue
                    pe[(s["date"])] += (stake / s["entry"] - stake) if s["win"] else -stake
                print(f"       {city:8} H={args.focus_hour}  trades={len(cs):>2}  P&L=${sum(pe.values()):>7,.0f}")

    # ===== (2c) SELECT-ON-H1 / CONFIRM-ON-H2 — does the H1-best cell hold OOS? =====
    print(f"\n{'='*74}\n(2c) Cell ranking: pick the winning (city,H) cells on H1, check them on H2\n{'='*74}")
    def cell_brier_gap(hv, city, H):
        sub = [c for c in cells if c["contested"] and c["half"] == hv and c["city"] == city and c["H"] == H]
        r = brier_pair(sub)
        return None if r is None else (r[1] - r[0], r[2])   # (market-model gap, n)
    keys = sorted({(c["city"], c["H"]) for c in cells if c["H"] in AFT})
    print(f"  {'cell':16} {'H1 gap (n)':>16}   {'H2 gap (n)':>16}   verdict")
    for city, H in keys:
        g1, g2 = cell_brier_gap("H1", city, H), cell_brier_gap("H2", city, H)
        s1 = f"{g1[0]:+.3f} (n={g1[1]})" if g1 else "n<6"
        s2 = f"{g2[0]:+.3f} (n={g2[1]})" if g2 else "n<6"
        v = ""
        if g1 and g2:
            v = "HOLDS OOS" if (g1[0] > 0 and g2[0] > 0) else ("flips" if g1[0] > 0 else "")
        print(f"  {city+' H='+str(H):16} {s1:>16}   {s2:>16}   {v}")
    print("\n(gap = market Brier - model Brier; >0 = model beats market. HOLDS OOS = >0 in BOTH halves.)")


if __name__ == "__main__":
    asyncio.run(main())
