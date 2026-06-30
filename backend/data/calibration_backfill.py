"""Calibrate our day-ahead forecast distribution against RESOLVED Polymarket outcomes.

We don't have to WAIT for live trades to accumulate to learn whether our probabilities
are well-calibrated. Every daily-temperature market that has already settled is a
labelled example: we know which bucket won (= the realized high/low). So we reconstruct
what our blend forecast WOULD have priced for each of those markets and compare the
predicted bucket probabilities to the actual winner — a reliability diagram + Brier on
hundreds of buckets of history we already have.

Method per resolved event:
  1. Outcome: from Polymarket's settled prices, the bucket whose YES resolved ~1 won.
  2. Forecast MEAN: rebuild the equal-weight blend (config.WEATHER_BLEND_MODELS) daily
     max/min for that (city, date) from the archived historical-forecast-api, in the
     city's native unit, then SUBTRACT the same per-station bias the live bot uses
     (station_bias_blend.json via get_station_bias).
  3. Price every bucket with a fitted Normal N(center, sigma) over its rounding interval
     [low-0.5, high+0.5) — identical to EnsembleForecast._fitted_bucket_prob for the
     day-ahead case (no observed-floor, no intraday curve).
  4. Sweep sigma to find the width that best calibrates our probabilities to reality.

This isolates the day-ahead SPREAD — the WEATHER_BLEND_SIGMA_INFLATION / floor knob,
the one part of the pipeline that is a fudge factor rather than data-derived. (The
same-day intraday width is separately backtested in intraday_backtest.py.)

CAVEAT: the historical forecast source is the DETERMINISTIC archived run, while the live
bot trades the ensemble MEAN; for some coastal cells these differ (the same divergence
the bias backfill guards against). That adds noise to the reconstructed mean, so the
best-fit sigma here is a mild UPPER bound on the true needed width. Cities whose bias was
skipped for source-inconsistency will show as miscalibrated — correctly flagging them.

Run:  PYTHONPATH=. venv/bin/python -m backend.data.calibration_backfill
      ... --models gfs_seamless         (GFS-only, to contrast with the blend)
      ... --cities nyc,london           (subset)
"""
import argparse
import asyncio
import json
import math
import statistics
from collections import defaultdict
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

import httpx

from backend.config import settings
from backend.data.weather import CITY_CONFIG, get_station_bias, is_bias_corrected
from backend.data.weather_markets import parse_event_slug, parse_bucket_label

GAMMA = "https://gamma-api.polymarket.com/events"
HIST_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
CLOB_PRICES_HISTORY = "https://clob.polymarket.com/prices-history"
HEADERS = {"User-Agent": "Mozilla/5.0"}

# sigma sweep grid, in degrees F (scaled 1/1.8 for °C cities, exactly like the pipeline)
SIGMA_GRID = [round(1.0 + 0.25 * i, 2) for i in range(0, 33)]   # 1.00 .. 9.00 °F


# ---------------------------------------------------------------------------
# 1) Outcomes: resolved Polymarket daily-temperature buckets
# ---------------------------------------------------------------------------
async def fetch_resolved_events(client: httpx.AsyncClient) -> List[dict]:
    """All closed daily-temperature events (paginated past the 100-event page cap).

    Ordered MOST-RECENT-FIRST (endDate desc): the default Gamma feed returns
    oldest-first and 422s past offset ~2000, so it silently never reaches the newest
    months — a latent bug that made the May-Sep intraday scan come back empty. With
    desc ordering the recent events (the ones we actually want to evaluate) are the
    first pages; the oldest few hundred fall off the far end of the cap instead."""
    out: List[dict] = []
    for off in range(0, 6000, 100):
        r = await client.get(GAMMA, params={"closed": "true", "limit": 100,
                                            "offset": off, "tag_slug": "daily-temperature",
                                            "order": "endDate", "ascending": "false"})
        if r.status_code >= 400:
            # Gamma caps the offset (~2000) with a 422 — treat as end of results.
            break
        page = r.json()
        if not page:
            break
        out.extend(page)
        if len(page) < 100:
            break
    return out


def _outcome_prices(market: dict) -> Optional[List[float]]:
    op = market.get("outcomePrices")
    if isinstance(op, str):
        try:
            op = json.loads(op)
        except Exception:
            return None
    if not op or len(op) < 2:
        return None
    try:
        return [float(x) for x in op]
    except Exception:
        return None


def _yes_token(market: dict) -> Optional[str]:
    ids = market.get("clobTokenIds")
    if isinstance(ids, str):
        try:
            ids = json.loads(ids)
        except Exception:
            return None
    return ids[0] if ids and len(ids) >= 1 else None


def extract_event(event: dict) -> Optional[Tuple[str, str, date, List[dict]]]:
    """-> (city, metric, target_date, buckets) where each bucket is
    {low, high, won, yes_token}. Returns None unless the event is a recognised,
    CLEANLY resolved daily-temperature event (exactly one winning bucket)."""
    pe = parse_event_slug(event.get("slug", ""))
    if not pe:
        return None
    city, metric, tdate = pe
    buckets: List[dict] = []
    winners = 0
    for m in event.get("markets", []):
        rng = parse_bucket_label(m.get("groupItemTitle") or "")
        op = _outcome_prices(m)
        if rng is None or op is None:
            # an unparseable/unpriced bucket — if it's the winner we'd mislabel the
            # event, so bail on the whole event to stay honest.
            if op is not None and op[0] > 0.9:
                return None
            continue
        won = op[0] > 0.9
        if won:
            winners += 1
        buckets.append({"low": rng[0], "high": rng[1], "won": won,
                        "yes_token": _yes_token(m)})
    if winners != 1 or len(buckets) < 2:
        return None
    return city, metric, tdate, buckets


# ---------------------------------------------------------------------------
# 2) Forecast means: archived blend, bias-corrected
# ---------------------------------------------------------------------------
async def _hist_one(client, lat, lon, model, start, end, temp_unit):
    r = await client.get(HIST_FORECAST_URL, params={
        "latitude": lat, "longitude": lon,
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "daily": "temperature_2m_max,temperature_2m_min",
        "temperature_unit": temp_unit, "timezone": "auto", "models": model})
    r.raise_for_status()
    d = r.json().get("daily", {})
    t = d.get("time", []) or []
    hi = d.get("temperature_2m_max", []) or []
    lo = d.get("temperature_2m_min", []) or []
    return {t[i]: {"high": hi[i] if i < len(hi) else None,
                   "low": lo[i] if i < len(lo) else None} for i in range(len(t))}


async def fetch_blend_means(client, cfg, models, start, end) -> Dict[str, Dict[str, Optional[float]]]:
    """Equal-weight blend daily high/low per ISO date, in the city's native unit.
    A date is kept only where every model has a value (well-defined mean)."""
    unit = cfg.get("unit", "F")
    temp_unit = "celsius" if unit == "C" else "fahrenheit"
    per_model = [await _hist_one(client, cfg["lat"], cfg["lon"], m, start, end, temp_unit)
                 for m in models]
    dates = set(per_model[0])
    for pm in per_model[1:]:
        dates &= set(pm)
    out: Dict[str, Dict[str, Optional[float]]] = {}
    for day in dates:
        row = {}
        for metric in ("high", "low"):
            vals = [pm[day].get(metric) for pm in per_model]
            row[metric] = (sum(vals) / len(vals)) if all(v is not None for v in vals) else None
        out[day] = row
    return out


# ---------------------------------------------------------------------------
# 3) Pricing (day-ahead: fitted Normal over the rounding interval; no floor/intraday)
# ---------------------------------------------------------------------------
def _norm_cdf(x: float, mean: float, sigma: float) -> float:
    if sigma <= 0:
        return 1.0 if x >= mean else 0.0
    return 0.5 * (1.0 + math.erf((x - mean) / (sigma * math.sqrt(2.0))))


def price_bucket(center: float, sigma: float,
                 low: Optional[float], high: Optional[float]) -> float:
    lo = (low - 0.5) if low is not None else None
    hi = (high + 0.5) if high is not None else None
    p_lo = _norm_cdf(lo, center, sigma) if lo is not None else 0.0
    p_hi = _norm_cdf(hi, center, sigma) if hi is not None else 1.0
    return max(0.0, p_hi - p_lo)


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------
def brier(preds_outcomes: List[Tuple[float, int]]) -> float:
    return sum((p - y) ** 2 for p, y in preds_outcomes) / len(preds_outcomes)


def reliability(preds_outcomes: List[Tuple[float, int]], n_bins: int = 10) -> Tuple[list, float]:
    """10-bin reliability table + expected calibration error (ECE)."""
    bins = [[] for _ in range(n_bins)]
    for p, y in preds_outcomes:
        idx = min(n_bins - 1, int(p * n_bins))
        bins[idx].append((p, y))
    table, ece, N = [], 0.0, len(preds_outcomes)
    for i, b in enumerate(bins):
        if not b:
            table.append((i / n_bins, (i + 1) / n_bins, 0, None, None))
            continue
        mp = statistics.mean(p for p, _ in b)
        freq = statistics.mean(y for _, y in b)
        table.append((i / n_bins, (i + 1) / n_bins, len(b), mp, freq))
        ece += abs(mp - freq) * len(b) / N
    return table, ece


def _records_at_sigma(records: List[dict], sigma_f: float) -> List[Tuple[float, int]]:
    out = []
    for r in records:
        sigma = sigma_f * (1.0 / 1.8 if r["unit"] == "C" else 1.0)
        p = price_bucket(r["center"], sigma, r["low"], r["high"])
        out.append((p, 1 if r["won"] else 0))
    return out


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
async def build_records(cities: List[str], models: List[str]) -> List[dict]:
    async with httpx.AsyncClient(timeout=45.0, headers=HEADERS) as client:
        events = await fetch_resolved_events(client)
        print(f"fetched {len(events)} closed daily-temperature events")

        # (city, metric, date) -> list of bucket dicts
        raw = []
        dates_by_city: Dict[str, list] = defaultdict(list)
        for e in events:
            ex = extract_event(e)
            if not ex:
                continue
            city, metric, tdate, buckets = ex
            if city not in cities or tdate >= date.today():
                continue
            raw.append((city, metric, tdate, buckets))
            dates_by_city[city].append(tdate)

        print(f"{len(raw)} cleanly-resolved events across {len(dates_by_city)} cities")

        # one forecast fetch per city covering its full date span
        means_by_city: Dict[str, dict] = {}
        for city, dates in dates_by_city.items():
            cfg = CITY_CONFIG.get(city)
            if not cfg:
                continue
            start, end = min(dates) - timedelta(days=1), max(dates) + timedelta(days=1)
            try:
                means_by_city[city] = await fetch_blend_means(client, cfg, models, start, end)
            except Exception as ex:
                print(f"  ! forecast fetch failed for {city}: {ex}")
                means_by_city[city] = {}

    # flatten to per-bucket records with the bias-corrected center attached
    records: List[dict] = []
    skipped_nofc = 0
    for city, metric, tdate, buckets in raw:
        cfg = CITY_CONFIG.get(city, {})
        unit = cfg.get("unit", "F")
        fc = means_by_city.get(city, {}).get(tdate.isoformat())
        raw_mean = fc.get(metric) if fc else None
        if raw_mean is None:
            skipped_nofc += len(buckets)
            continue
        center = raw_mean - get_station_bias(city, metric)
        corrected = is_bias_corrected(city, metric)
        for b in buckets:
            records.append({"city": city, "metric": metric, "date": tdate.isoformat(),
                            "unit": unit, "low": b["low"], "high": b["high"],
                            "won": b["won"], "center": center, "corrected": corrected,
                            "yes_token": b["yes_token"]})
    if skipped_nofc:
        print(f"  ({skipped_nofc} buckets skipped — no reconstructed forecast for that date)")
    return records


def _report(records: List[dict], label: str):
    print(f"\n{'='*72}\n{label}: {len(records)} buckets "
          f"({len(set((r['city'], r['date'], r['metric']) for r in records))} events)\n{'='*72}")
    if not records:
        return

    # sigma sweep
    print(f"\n  sigma sweep (Brier over all buckets; °F, scaled 1/1.8 for °C):")
    best = None
    for s in SIGMA_GRID:
        b = brier(_records_at_sigma(records, s))
        if best is None or b < best[1]:
            best = (s, b)
    # print a compact sweep around the minimum and at reference points
    refs = sorted(set([2.7, 3.5, 4.5, 5.5, best[0]]))
    for s in SIGMA_GRID:
        if s in refs or abs(s - best[0]) < 1e-9:
            b = brier(_records_at_sigma(records, s))
            tag = "  <-- BEST" if abs(s - best[0]) < 1e-9 else ("  (live floor+lead)" if abs(s - 2.7) < 1e-9 else "")
            print(f"    sigma={s:4.2f}F  Brier={b:.4f}{tag}")
    sig_star, brier_star = best
    print(f"\n  best-calibrated sigma = {sig_star:.2f}F   Brier* = {brier_star:.4f}")

    # reliability at sigma*
    po = _records_at_sigma(records, sig_star)
    table, ece = reliability(po)
    avg_sum = statistics.mean(
        sum(price_bucket(r["center"], sig_star * (1/1.8 if r['unit']=='C' else 1.0), r["low"], r["high"])
            for r in grp)
        for grp in _group_events(records).values()
    )
    print(f"  reliability @ sigma* (ECE={ece:.3f}; mean per-event prob sum={avg_sum:.2f}):")
    print(f"    {'pred bin':>12} {'n':>5} {'mean pred':>10} {'realized':>10}")
    for lo, hi, n, mp, freq in table:
        if n == 0:
            continue
        flag = ""
        if mp is not None and freq is not None:
            if freq - mp > 0.06: flag = "  under-confident (raise prob)"
            elif mp - freq > 0.06: flag = "  OVER-confident (lower prob)"
        print(f"    {lo:.1f}-{hi:.1f}   {n:>5} {mp:>10.3f} {freq:>10.3f}{flag}")


def _group_events(records):
    g = defaultdict(list)
    for r in records:
        g[(r["city"], r["date"], r["metric"])].append(r)
    return g


# ---------------------------------------------------------------------------
# market-price comparison (the real beat-the-market test, on the same buckets)
# ---------------------------------------------------------------------------
async def _fetch_market_price(client, token, tdate_iso, sem):
    """The market's YES price ~06:00 UTC on the target day — a same-day-ahead
    snapshot, before the daily high/low plays out (a fair forecast-vs-forecast point,
    not a near-settlement price that already 'knows' the obs so far)."""
    if not token:
        return None
    from datetime import datetime, timezone
    start = int(datetime.fromisoformat(tdate_iso).replace(tzinfo=timezone.utc).timestamp())
    async with sem:
        try:
            r = await client.get(CLOB_PRICES_HISTORY, params={
                "market": token, "startTs": start, "endTs": start + 24 * 3600, "fidelity": 60})
            if r.status_code >= 400:
                return None
            hist = r.json().get("history", []) or []
        except Exception:
            return None
    if not hist:
        return None
    target = start + 6 * 3600
    best = min(hist, key=lambda p: abs(p.get("t", 0) - target))
    p = best.get("p")
    return float(p) if p is not None else None


async def run_market_compare(records: List[dict], sig_star: float, sample_events: int):
    events = _group_events(records)
    keys = sorted(events.keys())
    stride = max(1, len(keys) // sample_events)
    sampled = [r for k in keys[::stride] for r in events[k]]
    print(f"\n{'='*72}\nMARKET vs MODEL — sampling {len(keys[::stride])} events "
          f"({len(sampled)} buckets); market YES price @~06:00 UTC day-of\n{'='*72}")

    sem = asyncio.Semaphore(12)
    async with httpx.AsyncClient(timeout=30.0, headers=HEADERS) as client:
        prices = await asyncio.gather(*[
            _fetch_market_price(client, r["yes_token"], r["date"], sem) for r in sampled])

    rows = [(r, mp) for r, mp in zip(sampled, prices) if mp is not None]
    print(f"  got market prices for {len(rows)}/{len(sampled)} buckets")
    if not rows:
        return

    def _model_p(r, sig):
        return price_bucket(r["center"], sig * (1/1.8 if r["unit"] == "C" else 1.0), r["low"], r["high"])

    def _report(subset, name):
        if not subset:
            return
        mkt = brier([(mp, 1 if r["won"] else 0) for r, mp in subset])
        m_opt = brier([(_model_p(r, sig_star), 1 if r["won"] else 0) for r, mp in subset])
        m_con = brier([(_model_p(r, 3.5), 1 if r["won"] else 0) for r, mp in subset])
        print(f"\n  {name} (n={len(subset)} buckets):")
        print(f"    market Brier             = {mkt:.4f}")
        print(f"    model Brier @sigma*={sig_star:.2f} = {m_opt:.4f}  (optimistic; near-analysis recon)")
        print(f"    model Brier @sigma=3.50  = {m_con:.4f}  (conservative day-ahead proxy)")

    _report(rows, "ALL sampled buckets")
    _report([(r, mp) for r, mp in rows if 0.10 < mp < 0.90], "CONTESTED (0.1<market<0.9) — where we trade")
    _report([(r, mp) for r, mp in rows if r["corrected"]], "bias-corrected cities")
    _report([(r, mp) for r, mp in rows if not r["corrected"]], "uncorrected cities")

    # per-city contested
    print(f"\n  per-city, CONTESTED buckets (market vs model@3.5):")
    by_city = defaultdict(list)
    for r, mp in rows:
        if 0.10 < mp < 0.90:
            by_city[r["city"]].append((r, mp))
    for city in sorted(by_city):
        sub = by_city[city]
        mkt = brier([(mp, 1 if r["won"] else 0) for r, mp in sub])
        mdl = brier([(_model_p(r, 3.5), 1 if r["won"] else 0) for r, mp in sub])
        verdict = "MODEL" if mdl < mkt else "market"
        print(f"    {city:12} n={len(sub):4}  market={mkt:.4f}  model@3.5={mdl:.4f}  -> {verdict} better")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cities", type=str, default=None, help="comma list (default = active WEATHER_CITIES)")
    ap.add_argument("--models", type=str, default=None, help="comma list (default = WEATHER_BLEND_MODELS)")
    ap.add_argument("--no-market", action="store_true", help="skip the market-price comparison pass")
    ap.add_argument("--market-sample", type=int, default=250, help="events to sample for market prices (default 250)")
    args = ap.parse_args()

    cities = ([c.strip() for c in args.cities.split(",")] if args.cities
              else [c.strip() for c in settings.WEATHER_CITIES.split(",") if c.strip()])
    models = ([m.strip() for m in args.models.split(",")] if args.models
              else [m.strip() for m in settings.WEATHER_BLEND_MODELS.split(",") if m.strip()])
    print(f"cities={cities}\nmodels={models}\n"
          f"bias: {'blend' if settings.WEATHER_BLEND_ENABLED else 'gfs'} table, "
          f"live blend sigma_inflation={settings.WEATHER_BLEND_SIGMA_INFLATION}, "
          f"floor={settings.WEATHER_SIGMA_FLOOR_F}F + lead={settings.WEATHER_SIGMA_PER_LEAD_DAY_F}F")

    records = asyncio.run(build_records(cities, models))
    if not records:
        print("no records — nothing to calibrate")
        return

    _report(records, "ALL")
    _report([r for r in records if r["metric"] == "high"], "HIGHS")
    _report([r for r in records if r["metric"] == "low"], "LOWS")
    _report([r for r in records if r["corrected"]], "BIAS-CORRECTED cities")
    _report([r for r in records if not r["corrected"]], "UNCORRECTED cities")

    # per-city Brier at the global best sigma for a quick where-it-hurts view
    allpo = _records_at_sigma(records, 1.0)  # placeholder to compute best below
    best_s = min(SIGMA_GRID, key=lambda s: brier(_records_at_sigma(records, s)))
    print(f"\n{'='*72}\nper-city Brier @ global sigma*={best_s:.2f}F\n{'='*72}")
    by_city = defaultdict(list)
    for r in records:
        by_city[r["city"]].append(r)
    for city in sorted(by_city, key=lambda c: -brier(_records_at_sigma(by_city[c], best_s))):
        recs = by_city[city]
        b = brier(_records_at_sigma(recs, best_s))
        nev = len(set((r["date"], r["metric"]) for r in recs))
        print(f"  {city:12} {CITY_CONFIG.get(city,{}).get('unit','F'):2}  "
              f"Brier={b:.4f}  buckets={len(recs):4}  events={nev}")

    if not args.no_market:
        asyncio.run(run_market_compare(records, best_s, args.market_sample))


if __name__ == "__main__":
    main()
