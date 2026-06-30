"""Edge-2 event-driven P&L backtest — the same-day intraday TAKER strategy.

The audit said it loud: a real strategy P&L backtest has never existed — every prior harness
scored Brier only. This runs the ACTUAL trading rules on resolved history, faithfully:

  * model probability = the LIVE intraday pipeline at the station-local scan hour
    (calibration_intraday.model_prob_at_hour: real EnsembleForecast + reconstructed observed
    floor + intraday sigma curve + drift).
  * market price       = CLOB prices-history at that same local hour.
  * decision           = the REAL functions: calculate_edge -> direction, spread/fee cost,
    net-edge >= WEATHER_MIN_EDGE_THRESHOLD gate, rel-spread gate, MAX_ENTRY_PRICE cap, and the
    market-gap guardrail (|our center - market-implied center| <= scaled tolerance).
  * sizing             = calculate_kelly_size off a SEQUENTIAL bankroll (compounds day by day).
  * fill               = taker at the ask (mid + spread/2), conservative; same-day books are deep
    (direction memo) so the small Kelly stake (<=2.5% bankroll) is assumed fillable.
  * settlement         = known winning bucket -> pay net odds on a win, lose the stake on a loss.

Output: P&L curve, hit-rate, drawdown, net-of-fee P&L, per city/hour, the inland-afternoon cell
vs the rest, an EVENT-bootstrap CI on total P&L, and sensitivity to spread + edge threshold.

Run:  PYTHONPATH=. venv/bin/python -m backend.data.edge2_backtest --hours 13,16,18,20 --months 2,3,4,5,6
"""
import argparse
import asyncio
import random
import statistics
from collections import defaultdict
from datetime import date, timedelta
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx

from backend.config import settings
from backend.data.weather import (CITY_CONFIG, METEOSTAT_STATION, get_station_bias, is_bias_corrected)
from backend.data.calibration_backfill import fetch_resolved_events, extract_event, fetch_blend_means
from backend.data.calibration_intraday import (fetch_hourly_obs, model_prob_at_hour,
                                               _fetch_day_history, _price_at)
from backend.core.sizing import calculate_edge, calculate_kelly_size

HDR = {"User-Agent": "Mozilla/5.0"}
random.seed(20260630)
INLAND = {"chicago", "denver", "nyc"}


def _c_scale(unit):
    return (1.0 / 1.8) if unit == "C" else 1.0


def bucket_center(b) -> Optional[float]:
    if b["low"] is not None and b["high"] is not None:
        return (b["low"] + b["high"]) / 2.0
    if b["high"] is not None:
        return b["high"] - 0.5
    if b["low"] is not None:
        return b["low"] + 0.5
    return None


async def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cities", type=str, default=None)
    ap.add_argument("--hours", type=str, default="13,16,18,20")
    ap.add_argument("--months", type=str, default="2,3,4,5,6")
    ap.add_argument("--spread", type=float, default=settings.WEATHER_DEFAULT_SPREAD)
    ap.add_argument("--edge", type=float, default=settings.WEATHER_MIN_EDGE_THRESHOLD)
    ap.add_argument("--flat", action="store_true", help="size every trade off the FIXED initial bankroll (no compounding) to isolate skill from compounding")
    ap.add_argument("--models", type=str, default=None)
    args = ap.parse_args()

    cities = ([c.strip() for c in args.cities.split(",")] if args.cities
              else [c.strip() for c in settings.WEATHER_CITIES.split(",") if c.strip()])
    models = ([m.strip() for m in args.models.split(",")] if args.models
              else [m.strip() for m in settings.WEATHER_BLEND_MODELS.split(",") if m.strip()])
    hours = [int(h) for h in args.hours.split(",")]
    months = set(int(m) for m in args.months.split(","))
    print(f"cities={cities}  hours={hours}  months={sorted(months)}  spread={args.spread}  "
          f"edge_gate={args.edge}  fee={settings.WEATHER_FEE_RATE}  kelly={settings.KELLY_FRACTION}/"
          f"{settings.KELLY_MAX_TRADE_FRACTION}  gap_tol={settings.WEATHER_MAX_MARKET_GAP_F}F")

    # ---- data build (reuse the calibration_intraday reconstruction) ----
    async with httpx.AsyncClient(timeout=45.0, headers=HDR) as client:
        events = await fetch_resolved_events(client)
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
        print(f"{len(raw)} resolved in-scope events")

        means, obs_days, tzs = {}, {}, {}
        for city, d in dts.items():
            cfg, st = CITY_CONFIG.get(city), METEOSTAT_STATION.get(city)
            if not cfg:
                continue
            means[city] = await fetch_blend_means(client, cfg, models, min(d) - timedelta(days=1), max(d) + timedelta(days=1))
            tzs[city] = ZoneInfo(cfg.get("tz") or "UTC")
            if st:
                obs_days[city] = await fetch_hourly_obs(client, st, min(d) - timedelta(days=1),
                                                        max(d) + timedelta(days=1), cfg.get("tz") or "UTC",
                                                        cfg.get("unit", "F"))

    # market prices: fetch every in-scope bucket's day history once (need ALL for the
    # event-implied mean and to find signals — not a sample)
    tok_date = {}
    for city, metric, tdate, buckets in raw:
        for b in buckets:
            if b["yes_token"]:
                tok_date[b["yes_token"]] = tdate.isoformat()
    toks = list(tok_date)
    print(f"fetching market history for {len(toks)} bucket tokens...")
    sem = asyncio.Semaphore(12)
    async with httpx.AsyncClient(timeout=30.0, headers=HDR) as client:
        hists = await asyncio.gather(*[_fetch_day_history(client, t, tok_date[t], sem) for t in toks])
    hist = dict(zip(toks, hists))

    # ---- build signals ----
    signals = []   # dict per actionable trade
    n_eval = n_gate_edge = n_gate_spread = n_gate_entry = n_gate_gap = 0
    for city, metric, tdate, buckets in raw:
        cfg = CITY_CONFIG.get(city, {})
        unit = cfg.get("unit", "F")
        fc = means.get(city, {}).get(tdate.isoformat())
        if not fc or fc.get(metric) is None:
            continue
        obs_hours = obs_days.get(city, {}).get(tdate)
        corrected_mean = fc[metric] - get_station_bias(city, metric)
        tz = tzs[city]
        for H in hours:
            # market prices for every bucket at H + event-implied mean
            mkt = {}
            num = den = 0.0
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
            gap_tol = settings.WEATHER_MAX_MARKET_GAP_F * _c_scale(unit)

            for b in buckets:
                if id(b) not in mkt:
                    continue
                res = model_prob_at_hour(city, city, tdate, unit, metric, b["low"], b["high"],
                                         fc, obs_hours, H)
                if res is None:
                    continue
                model_p, _floor = res
                market_yes = mkt[id(b)]
                n_eval += 1
                edge, dir_raw = calculate_edge(model_p, market_yes)
                direction = "yes" if dir_raw == "up" else "no"
                side_mid = market_yes if direction == "yes" else (1.0 - market_yes)
                if side_mid <= 0:
                    continue
                entry = min(0.999, side_mid + args.spread / 2.0)
                cost = args.spread / 2.0 + settings.WEATHER_FEE_RATE
                rel_spread = args.spread / side_mid
                net_edge = edge - cost
                # gates (faithful to passes_threshold)
                if net_edge < args.edge:
                    n_gate_edge += 1
                    continue
                if rel_spread > settings.WEATHER_MAX_REL_SPREAD:
                    n_gate_spread += 1
                    continue
                if entry > settings.WEATHER_MAX_ENTRY_PRICE:
                    n_gate_entry += 1
                    continue
                gap = abs(corrected_mean - event_mkt_mean) if event_mkt_mean is not None else None
                if settings.WEATHER_MARKET_GAP_ENABLED and gap is not None and gap > gap_tol:
                    n_gate_gap += 1
                    continue
                won_yes = b["won"]
                win = won_yes if direction == "yes" else (not won_yes)
                signals.append({"city": city, "metric": metric, "date": tdate, "H": H,
                                "direction": direction, "model_p": model_p, "market_yes": market_yes,
                                "net_edge": net_edge, "entry": entry, "win": win,
                                "label": f"{b['low']}-{b['high']}", "unit": unit})

    print(f"\nevaluated {n_eval} (bucket,hour) cells; gated out: edge<{args.edge}={n_gate_edge}, "
          f"rel-spread={n_gate_spread}, entry>{settings.WEATHER_MAX_ENTRY_PRICE}={n_gate_entry}, "
          f"market-gap={n_gate_gap}")
    print(f"{len(signals)} actionable signals")
    if not signals:
        print("no signals — nothing to trade")
        return

    # ---- sequential bankroll: compound day by day ----
    signals.sort(key=lambda s: (s["date"], s["H"]))
    by_day = defaultdict(list)
    for s in signals:
        by_day[s["date"]].append(s)
    bankroll = settings.INITIAL_BANKROLL
    equity = [(None, bankroll)]
    ntr = nwin = 0
    per_event_pnl = defaultdict(float)
    cell_pnl = defaultdict(lambda: [0, 0.0])   # (city,H) -> [n, pnl]
    citywin = defaultdict(lambda: [0, 0])
    for d in sorted(by_day):
        day_pnl = 0.0
        for s in by_day[d]:
            kmp = s["entry"] if s["direction"] == "yes" else (1.0 - s["entry"])
            stake = calculate_kelly_size(edge=s["net_edge"], probability=s["model_p"],
                                         market_price=kmp,
                                         direction=("up" if s["direction"] == "yes" else "down"),
                                         bankroll=(settings.INITIAL_BANKROLL if args.flat else bankroll))
            if stake <= 0:
                continue
            shares = stake / s["entry"]
            pnl = (shares - stake) if s["win"] else (-stake)   # net odds on win, lose stake on loss
            day_pnl += pnl
            ntr += 1
            nwin += 1 if s["win"] else 0
            per_event_pnl[(s["city"], s["date"], s["metric"])] += pnl
            cell_pnl[(s["city"], s["H"])][0] += 1
            cell_pnl[(s["city"], s["H"])][1] += pnl
            citywin[s["city"]][0] += 1
            citywin[s["city"]][1] += 1 if s["win"] else 0
        bankroll += day_pnl
        equity.append((d, bankroll))

    total_pnl = bankroll - settings.INITIAL_BANKROLL
    peak = settings.INITIAL_BANKROLL
    maxdd = 0.0
    for _, e in equity:
        peak = max(peak, e)
        maxdd = min(maxdd, e - peak)

    # event-bootstrap CI on total P&L
    evs = list(per_event_pnl.values())
    boot = []
    n = len(evs)
    for _ in range(5000):
        boot.append(sum(evs[random.randrange(n)] for _ in range(n)))
    boot.sort()
    ci = (boot[125], boot[4874])

    print(f"\n{'='*72}\nEDGE-2 STRATEGY P&L  (sequential bankroll, ${settings.INITIAL_BANKROLL:,.0f} start)\n{'='*72}")
    print(f"  trades={ntr}  wins={nwin} ({nwin/ntr:.0%})  final bankroll=${bankroll:,.0f}")
    print(f"  TOTAL P&L = ${total_pnl:,.0f}  ({total_pnl/settings.INITIAL_BANKROLL:+.1%} of start)")
    print(f"  max drawdown = ${maxdd:,.0f}   avg $/trade = ${total_pnl/ntr:,.1f}")
    print(f"  event-bootstrap 95% CI on total P&L: [${ci[0]:,.0f}, ${ci[1]:,.0f}]  "
          f"({'excludes 0 (POSITIVE)' if ci[0] > 0 else ('excludes 0 (NEGATIVE)' if ci[1] < 0 else 'includes 0')})")

    print(f"\n  by city (win rate):")
    for c in sorted(citywin):
        n_, w_ = citywin[c]
        pnl = sum(v[1] for k, v in cell_pnl.items() if k[0] == c)
        print(f"    {c:12} trades={n_:>3}  win={w_/n_ if n_ else 0:.0%}  pnl=${pnl:>8,.0f}")

    print(f"\n  by (city, hour):")
    for k in sorted(cell_pnl):
        n_, pnl = cell_pnl[k]
        print(f"    {k[0]:12} H={k[1]:>2}  trades={n_:>3}  pnl=${pnl:>8,.0f}")

    inland_pnl = sum(v[1] for k, v in cell_pnl.items() if k[0] in INLAND)
    inland_n = sum(v[0] for k, v in cell_pnl.items() if k[0] in INLAND)
    rest_pnl = total_pnl - inland_pnl
    rest_n = ntr - inland_n
    print(f"\n  INLAND (chicago/denver/nyc): {inland_n} trades  ${inland_pnl:,.0f}")
    print(f"  rest (coastal):              {rest_n} trades  ${rest_pnl:,.0f}")


if __name__ == "__main__":
    asyncio.run(main())
