"""Stream 2, step 2 — measure the evening-overconfidence fix against ALL resolved history,
OUT-OF-SAMPLE, with no Open-Meteo calls (reads calibration_cache.json).

The leak (calibration memory): on the in-progress day the intraday σ collapses to the
WEATHER_INTRADAY_SIGMA_MIN_F rail (0.3°F) by evening, so near a rounding BOUNDARY the model
commits ~certain when the outcome is really a coin-flip — confident-wrong tanks Brier while
the market hedges. This harness re-prices every resolved bucket with the EXACT live pipeline
(real EnsembleForecast: intraday curve + observed floor + drift) under different evening σ
rails, and asks: does raising the rail lower our Brier in the EVENING without hurting the
other hours, and does it close the gap to the market — on data it WASN'T tuned on?

Discipline (so we fix reality, not the backtest):
  - TRAIN/TEST split by date. Pick the rail on TRAIN, report the gain on TEST (held out).
  - Report EVERY hour, not just evening — a fix that helps the evening but hurts mornings is
    rejected (no free lunch).
  - MEASURE the empirical evening residual std(settled − our center): the principled rail is
    the real uncertainty we're ignoring, not a Brier-minimising knob.

Run:  PYTHONPATH=. venv/bin/python -m backend.data.overconfidence_eval
      ... --rails 0.3,0.5,0.8,1.2,1.6,2.0   --hours 7,10,13,16,18,20
"""
import argparse
import json
import statistics
from collections import defaultdict
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from backend.config import settings
from backend.data.weather import (CITY_CONFIG, EnsembleForecast, _HIGH_SET_LOCAL_HOUR,
                                   _LOW_SET_LOCAL_HOUR)
from backend.data.calibration_intraday import _observed_so_far, _price_at
from backend.data.calibration_backfill import brier

CACHE_FILE = Path(__file__).with_name("calibration_cache.json")
EVENING = {16, 18, 20}
MORNING = {7, 10}


def _bucket_center(b):
    if b["low"] is not None and b["high"] is not None:
        return (b["low"] + b["high"]) / 2.0
    if b["high"] is not None:
        return b["high"] - 0.5
    if b["low"] is not None:
        return b["low"] + 0.5
    return None


def build_static(cache, hours):
    """Per (event, hour): the inputs that DON'T depend on our σ rail — observed-so-far,
    the forecast, each bucket's market price + outcome. Computed once; reused for every rail."""
    means, obs, tzs, hist = cache["means"], cache["obs"], cache["tzs"], cache["histories"]
    recs = []
    for ev in cache["events"]:
        city, metric, diso = ev["city"], ev["metric"], ev["date"]
        cfg = CITY_CONFIG.get(city, {})
        unit = cfg.get("unit", "F")
        fc = means.get(city, {}).get(diso)
        if not fc or fc.get(metric) is None:
            continue
        tz = ZoneInfo(tzs.get(city, "UTC"))
        oh_raw = obs.get(city, {}).get(diso, {})
        obs_hours = {int(h): t for h, t in oh_raw.items()}
        gate = _HIGH_SET_LOCAL_HOUR if metric == "high" else _LOW_SET_LOCAL_HOUR
        for H in hours:
            observed = _observed_so_far(obs_hours, H, metric) if (obs_hours and H >= gate) else None
            buckets = []
            for b in ev["buckets"]:
                if not b["yes_token"]:
                    continue
                mp = _price_at(hist.get(b["yes_token"]), diso, tz, H)
                if mp is None:
                    continue
                buckets.append({"low": b["low"], "high": b["high"], "won": b["won"],
                                "mkt": mp, "center": _bucket_center(b)})
            if buckets:
                recs.append({"city": city, "metric": metric, "date": diso, "unit": unit,
                             "H": H, "fc": fc, "observed": observed, "buckets": buckets})
    return recs


def price_all(recs, rail_f):
    """Re-price every bucket under evening σ rail = rail_f (°F; scaled for °C inside the
    pipeline). Returns flat list of dicts with model prob, market prob, outcome, context."""
    settings.WEATHER_INTRADAY_SIGMA_MIN_F = rail_f   # in-process only; live config untouched
    out = []
    for r in recs:
        city, metric, H = r["city"], r["metric"], r["H"]
        fc, observed, unit = r["fc"], r["observed"], r["unit"]
        ef = EnsembleForecast(city_key=city, city_name=city, target_date=date.fromisoformat(r["date"]),
                              member_highs=[fc["high"]] * 32 if metric == "high" else [],
                              member_lows=[fc["low"]] * 32 if metric == "low" else [],
                              unit=unit, is_blend=True)
        center = ef.pricing_center(metric, local_hour=H, observed=observed)
        sigma_eff = ef.effective_sigma_for(metric, local_hour=H)
        ekey = (city, metric, r["date"], H)
        for b in r["buckets"]:
            if metric == "high":
                p = ef.probability_high_in_range(b["low"], b["high"], floor=observed, local_hour=H)
            else:
                p = ef.probability_low_in_range(b["low"], b["high"], ceiling=observed, local_hour=H)
            p = max(0.01, min(0.99, p))
            out.append({"city": city, "metric": metric, "H": H, "date": r["date"], "ekey": ekey,
                        "model": p, "mkt": b["mkt"], "won": 1 if b["won"] else 0,
                        "center": center, "sigma_eff": sigma_eff, "bcenter": b["center"],
                        "had_floor": observed is not None})
    return out


def _guardrail_keep(rows):
    """Apply the live market-gap guardrail: suppress an (event,hour) when our pricing center
    disagrees with the market-IMPLIED mean by more than clamp(SIGMA_K·σ_eff, MIN, MAX). Returns
    only the rows on surviving event-hours — the realistic TRADED subset (the guardrail already
    vetoes many confident-wrong evening events, so this shows the fix's MARGINAL value)."""
    by_ev = defaultdict(list)
    for r in rows:
        by_ev[r["ekey"]].append(r)
    keep = []
    for ekey, ev in by_ev.items():
        finite = [r for r in ev if r["bcenter"] is not None]
        wsum = sum(r["mkt"] for r in finite)
        if wsum <= 0 or len(finite) < settings.WEATHER_MARKET_GAP_MIN_BUCKETS:
            continue
        mkt_mean = sum(r["mkt"] * r["bcenter"] for r in finite) / wsum
        unit = CITY_CONFIG.get(ev[0]["city"], {}).get("unit", "F")
        scale = (1.0 / 1.8) if unit == "C" else 1.0
        sig = ev[0]["sigma_eff"]
        thr = max(settings.WEATHER_MIN_MARKET_GAP_F * scale,
                  min(settings.WEATHER_MARKET_GAP_SIGMA_K * sig,
                      settings.WEATHER_MAX_MARKET_GAP_F * scale))
        if abs(ev[0]["center"] - mkt_mean) <= thr:
            keep.extend(ev)
    return keep


def _brier(rows, key):
    return brier([(r[key], r["won"]) for r in rows]) if rows else None


def _contested(rows):
    return [r for r in rows if 0.10 < r["mkt"] < 0.90]


def report_split(recs, rails, train, test):
    print(f"\nTRAIN = events before {test[0] if test else '-'}  ({len(train)} static-recs)   "
          f"TEST = held out ({len(test)} static-recs)")
    # market Brier is rail-independent — compute once per split on contested
    def mkt_brier(split):
        rows = _contested(price_all(split, rails[0]))
        return _brier(rows, "mkt"), len(rows)

    print("\n" + "=" * 96)
    print("EVENING-σ-RAIL SWEEP — model Brier on CONTESTED buckets (lower=better; market is the bar)")
    print("=" * 96)
    for split, name in ((train, "TRAIN"), (test, "TEST")):
        mb, n = mkt_brier(split)
        print(f"\n  [{name}]  contested n={n}   market Brier={mb:.4f}")
        print(f"    {'rail°F':>7} {'ALL':>8} {'EVENING':>8} {'MORNING':>8} {'MIDDAY':>8}  "
              f"{'evening n':>9}")
        for rail in rails:
            rows = _contested(price_all(split, rail))
            allb = _brier(rows, "model")
            ev = _brier([r for r in rows if r["H"] in EVENING], "model")
            mo = _brier([r for r in rows if r["H"] in MORNING], "model")
            mid = _brier([r for r in rows if r["H"] not in EVENING and r["H"] not in MORNING], "model")
            evn = len([r for r in rows if r["H"] in EVENING])
            tag = "  <- live" if abs(rail - 0.3) < 1e-9 else ""
            print(f"    {rail:>7.2f} {allb:>8.4f} {ev:>8.4f} {mo:>8.4f} {mid:>8.4f}  {evn:>9}{tag}")
        # market evening/morning bar for reference
        rows = _contested(price_all(split, rails[0]))
        evm = _brier([r for r in rows if r["H"] in EVENING], "mkt")
        mom = _brier([r for r in rows if r["H"] in MORNING], "mkt")
        print(f"    {'market':>7} {mb:>8.4f} {evm:>8.4f} {mom:>8.4f}  {'':>8}   (the bar)")


def measure_residual(recs):
    """Empirical std of (winning-bucket center − our pricing center) in the EVENING — the real
    uncertainty the 0.3°F rail ignores. This is the mechanism-derived rail, independent of Brier."""
    rows = price_all(recs, 0.3)
    by_band = defaultdict(list)
    for r in rows:
        if r["won"] and r["bcenter"] is not None and r["had_floor"]:
            band = "EVENING" if r["H"] in EVENING else ("MORNING" if r["H"] in MORNING else "MIDDAY")
            # residual in °F-equivalent (scale °C up by 1.8 so the rail comparison is apples-to-apples)
            resid = (r["bcenter"] - r["center"]) * (1.8 if CITY_CONFIG.get(r["city"], {}).get("unit") == "C" else 1.0)
            by_band[band].append(resid)
    print("\n" + "=" * 96)
    print("EMPIRICAL RESIDUAL  std(winning-bucket center − our pricing center), °F-equiv, floor-engaged")
    print("=" * 96)
    print(f"  {'band':10} {'n':>5} {'mean':>7} {'std':>7}   (std = the σ the model SHOULD carry there)")
    for band in ("MORNING", "MIDDAY", "EVENING"):
        v = by_band.get(band, [])
        if len(v) > 2:
            print(f"  {band:10} {len(v):>5} {statistics.mean(v):>7.2f} {statistics.pstdev(v):>7.2f}")
    return by_band


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rails", type=str, default="0.3,0.5,0.8,1.2,1.6,2.0")
    ap.add_argument("--hours", type=str, default="7,10,13,16,18,20")
    ap.add_argument("--test-frac", type=float, default=0.4, help="fraction of (newest) events held out")
    args = ap.parse_args()
    rails = [float(x) for x in args.rails.split(",")]
    hours = [int(h) for h in args.hours.split(",")]

    cache = json.loads(CACHE_FILE.read_text())
    print(f"cache: {len(cache['events'])} events, cities={cache['cities']}, "
          f"blend_inflation={cache['blend_inflation']}, live rail={cache['intraday_min_f']}")
    recs = build_static(cache, hours)
    print(f"{len(recs)} (event,hour) static records over hours {hours}")

    # split by DATE: newest test_frac held out
    recs.sort(key=lambda r: r["date"])
    dates = sorted({r["date"] for r in recs})
    cut = dates[int(len(dates) * (1 - args.test_frac))]
    train = [r for r in recs if r["date"] < cut]
    test = [r for r in recs if r["date"] >= cut]

    measure_residual(recs)
    report_split(recs, rails, train, test)

    # --- robustness: 3 date-folds, evening contested Brier at live rail vs proposed -----
    LIVE, FIX = 0.3, 2.0
    print("\n" + "=" * 96)
    print(f"ROBUSTNESS — EVENING contested Brier per date-fold (live rail {LIVE} vs proposed {FIX})")
    print("=" * 96)
    print(f"  {'fold (dates)':28} {'n':>5} {'market':>8} {f'rail{LIVE}':>8} {f'rail{FIX}':>8} {'Δ model':>8}")
    nf = 3
    for i in range(nf):
        lo, hi = dates[i * len(dates) // nf], dates[min((i + 1) * len(dates) // nf, len(dates) - 1)]
        fold = [r for r in recs if lo <= r["date"] <= hi]
        ev_live = [r for r in _contested(price_all(fold, LIVE)) if r["H"] in EVENING]
        ev_fix = [r for r in _contested(price_all(fold, FIX)) if r["H"] in EVENING]
        if not ev_live:
            continue
        mb = _brier(ev_live, "mkt")
        bl, bf = _brier(ev_live, "model"), _brier(ev_fix, "model")
        print(f"  {lo[:7]+'..'+hi[:7]:28} {len(ev_live):>5} {mb:>8.4f} {bl:>8.4f} {bf:>8.4f} "
              f"{bf-bl:>+8.4f}")

    # --- guardrail-aware: the fix's value on the realistic TRADED subset -----------------
    # Hold the traded set FIXED to what the LIVE config (rail 0.3 + its guardrail) actually
    # trades, then re-price that SAME set under each rail — isolating the σ change from the
    # guardrail-set change (the guardrail threshold itself scales with σ, so we must pin it).
    print("\n" + "=" * 96)
    print(f"GUARDRAIL-TRADED — same traded set (live guardrail), only the pricing σ varies")
    print("=" * 96)
    print(f"  {'subset':24} {'n':>5} {'market':>8} {f'rail{LIVE}':>8} {f'rail{FIX}':>8} {'Δ model':>8}")
    for split, name in ((train, "TRAIN"), (test, "TEST")):
        traded = {r["ekey"] for r in _guardrail_keep(price_all(split, LIVE))}
        priced = {rail: [r for r in price_all(split, rail) if r["ekey"] in traded] for rail in (LIVE, FIX)}
        for band, hrs in (("all hours", None), ("evening only", EVENING)):
            def sub(rows):
                rows = [r for r in rows if (hrs is None or r["H"] in hrs)]
                return [r for r in rows if 0.10 < r["mkt"] < 0.90]
            kl, kf = sub(priced[LIVE]), sub(priced[FIX])
            if not kl:
                continue
            mb = _brier(kl, "mkt")
            bl, bf = _brier(kl, "model"), _brier(kf, "model")
            print(f"  {name+' '+band:24} {len(kl):>5} {mb:>8.4f} {bl:>8.4f} {bf:>8.4f} {bf-bl:>+8.4f}")


if __name__ == "__main__":
    main()
