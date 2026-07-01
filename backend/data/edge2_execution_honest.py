"""Edge-2 EXECUTION-HONEST backtest — does the edge survive real action-latency + the live
rail filter?

`edge2_oos_backtest` proved the MODEL has skill: at local hour H=16 our probability beats the
market's price (Brier + P&L, OOS). But it evaluated the trade AS IF we could act the instant the
high sets (H=16 sharp) and it never applied the production near-money ("rail") filter. Live,
neither holds:

  (a) LATENCY. The post-extreme gate opens off `fetch_observed_extreme` = Meteostat, which lags
      the real high by ~1-2h. So we cannot CONFIRM the high is in — and therefore cannot act —
      until ~16h + lag. By then the market has kept repricing the now-known high.

  (b) RAIL FILTER. `weather_markets._parse_bucket_market` drops any bucket priced <=0.01 or
      >=0.99. Once the high is decisively in, Polymarket collapses buckets to ~0.0005/0.9995, so
      the live bot discards them.

This script re-runs the SAME pipeline (same events, forecasts, obs, prices, gates, sizing) but
sweeps the ACTION HOUR Ha and, at each, prices + decides the trade at Ha (model_prob_at_hour uses
obs through Ha; price = CLOB price at Ha) WITH the live rail filter added. Reading down the Ha
rows = watching the edge decay as our action slips later behind the high. The rows that matter
live are Ha >= 17-18 (16h + Meteostat lag), NOT the idealized Ha=16.

Fill caveat: historical CLOB depth is not retrievable, so (like the original) fills use the flat
WEATHER_DEFAULT_SPREAD on the period's price. That UNDERstates real slippage on thin post-high
books, so any decay seen here is a floor on the true one.

Run: PYTHONPATH=. venv/bin/python -m backend.data.edge2_execution_honest \
       --cities denver,chicago,atlanta --hours 16,17,18,19,20 --months 2,3,4,5,6 --split 2026-05-01
"""
import argparse
import asyncio
import statistics
from collections import defaultdict
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import httpx

from backend.config import settings
from backend.data.weather import CITY_CONFIG, METEOSTAT_STATION, get_station_bias
from backend.data.calibration_backfill import extract_event, fetch_blend_means, brier
from backend.data.calibration_intraday import (fetch_hourly_obs, model_prob_at_hour,
                                               _fetch_day_history, _price_at)
from backend.core.sizing import calculate_edge, calculate_kelly_size
from backend.data.edge2_backtest import bucket_center, _c_scale
# reuse the exact resolved-event fetch + bootstrap from the OOS script
from backend.data.edge2_oos_backtest import fetch_both_ends, _bootstrap_ci, HDR

RAIL_LO, RAIL_HI = 0.01, 0.99   # weather_markets._parse_bucket_market drops buckets outside this


def _kelly_pnl(sigs):
    """Event-bootstrapped Kelly P&L for a set of gated trades (same math as edge2_oos_backtest)."""
    per_event = defaultdict(float)
    ntr = nwin = 0
    for s in sigs:
        kmp = s["entry"] if s["direction"] == "yes" else (1.0 - s["entry"])
        stake = calculate_kelly_size(edge=s["net_edge"], probability=s["model_p"], market_price=kmp,
                                     direction=("up" if s["direction"] == "yes" else "down"),
                                     bankroll=settings.INITIAL_BANKROLL)
        if stake <= 0:
            continue
        pnl = (stake / s["entry"] - stake) if s["win"] else -stake
        per_event[(s["city"], s["date"])] += pnl
        ntr += 1
        nwin += 1 if s["win"] else 0
    return ntr, nwin, sum(per_event.values()), list(per_event.values())


async def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cities", type=str, default="denver,chicago,atlanta")
    ap.add_argument("--hours", type=str, default="16,17,18,19,20")
    ap.add_argument("--months", type=str, default="2,3,4,5,6")
    ap.add_argument("--split", type=str, default="2026-05-01")
    ap.add_argument("--metric", type=str, default="high")
    args = ap.parse_args()

    cities = [c.strip() for c in args.cities.split(",")]
    models = [m.strip() for m in settings.WEATHER_BLEND_MODELS.split(",") if m.strip()]
    hours = [int(h) for h in args.hours.split(",")]
    months = set(int(m) for m in args.months.split(","))
    split = date.fromisoformat(args.split)
    metric_want = args.metric

    def half(d: date) -> str:
        return "H1" if d < split else "H2"

    print(f"cities={cities}  action-hours={hours}  months={sorted(months)}  split={split}\n"
          f"edge_gate={settings.WEATHER_MIN_EDGE_THRESHOLD}  entry_cap={settings.WEATHER_MAX_ENTRY_PRICE}  "
          f"spread={settings.WEATHER_DEFAULT_SPREAD}  rail_filter=({RAIL_LO},{RAIL_HI})")

    # ---- resolved events + forecasts/obs (same reconstruction as the OOS backtest) ----
    async with httpx.AsyncClient(timeout=60.0, headers=HDR) as client:
        events = await fetch_both_ends(client)
        raw, dts = [], defaultdict(list)
        for e in events:
            ex = extract_event(e)
            if not ex:
                continue
            city, metric, tdate, buckets = ex
            if city not in cities or metric != metric_want or tdate >= date.today() or tdate.month not in months:
                continue
            raw.append((city, metric, tdate, buckets))
            dts[city].append(tdate)
        nh1 = sum(1 for r in raw if half(r[2]) == "H1")
        print(f"{len(raw)} resolved in-scope events  (H1={nh1}, H2={len(raw)-nh1})")

        means, obs_days, tzs = {}, {}, {}
        for city, d in dts.items():
            cfg, st = CITY_CONFIG.get(city), METEOSTAT_STATION.get(city)
            if not cfg:
                continue
            lo, hi = min(d) - timedelta(days=1), max(d) + timedelta(days=1)
            tzs[city] = cfg.get("tz") or "UTC"
            unit = cfg.get("unit", "F")
            means[city] = await fetch_blend_means(client, cfg, models, lo, hi)
            if st:
                obs_days[city] = await fetch_hourly_obs(client, st, lo, hi, cfg.get("tz") or "UTC", unit)

    # ---- market price histories (one series per bucket token) ----
    tok_date = {b["yes_token"]: tdate.isoformat()
                for city, metric, tdate, buckets in raw for b in buckets if b["yes_token"]}
    toks = list(tok_date)
    print(f"fetching market history for {len(toks)} bucket tokens...")
    sem = asyncio.Semaphore(12)
    async with httpx.AsyncClient(timeout=30.0, headers=HDR) as client:
        hists = await asyncio.gather(*[_fetch_day_history(client, t, tok_date[t], sem) for t in toks])
    hist = dict(zip(toks, hists))

    # ---- evaluate every (event, bucket, action-hour): model+price+gates all AT Ha ----
    # evals: one row per (bucket, Ha) that had a valid model prob, floor active, and a price.
    evals = []
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
        for Ha in hours:
            # event mean from this hour's prices (for the market-gap gate)
            mkt, num, den = {}, 0.0, 0.0
            for b in buckets:
                p = _price_at(hist.get(b["yes_token"]), tdate.isoformat(), tz, Ha) if b["yes_token"] else None
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
            gap = abs(corrected_mean - event_mkt_mean) if event_mkt_mean is not None else None
            gap_ok = not (settings.WEATHER_MARKET_GAP_ENABLED and gap is not None and gap > gap_tol)
            for b in buckets:
                if id(b) not in mkt:
                    continue
                res = model_prob_at_hour(city, city, tdate, unit, metric, b["low"], b["high"], fc, obs_hours, Ha)
                if res is None:
                    continue
                model_p, floor_active = res
                if not floor_active:          # live post-extreme gate: only act once the high is in
                    continue
                price = mkt[id(b)]
                edge, dir_raw = calculate_edge(model_p, price)
                direction = "yes" if dir_raw == "up" else "no"
                side_mid = price if direction == "yes" else (1.0 - price)
                if side_mid <= 0:
                    continue
                entry = min(0.999, side_mid + settings.WEATHER_DEFAULT_SPREAD / 2.0)
                net_edge = edge - (settings.WEATHER_DEFAULT_SPREAD / 2.0 + settings.WEATHER_FEE_RATE)
                rel_spread = settings.WEATHER_DEFAULT_SPREAD / side_mid
                railed = not (RAIL_LO < price < RAIL_HI)
                passes = (not railed
                          and net_edge >= settings.WEATHER_MIN_EDGE_THRESHOLD
                          and entry <= settings.WEATHER_MAX_ENTRY_PRICE
                          and rel_spread <= settings.WEATHER_MAX_REL_SPREAD
                          and gap_ok)
                won = b["won"] if direction == "yes" else (not b["won"])
                evals.append({"city": city, "date": tdate, "half": half(tdate), "Ha": Ha,
                              "model_p": model_p, "price": price, "won_bucket": b["won"],
                              "contested": RAIL_LO < price < RAIL_HI and 0.10 < price < 0.90,
                              "railed": railed, "direction": direction, "entry": entry,
                              "net_edge": net_edge, "win": won, "passes": passes})

    # ===== (A) REACHABILITY & P&L vs ACTION HOUR — the decay =====
    print(f"\n{'='*88}\n(A) EDGE vs ACTION HOUR  (Ha = the local hour we can actually act)\n"
          f"    live, our Meteostat gate opens ~16h+lag, so the HONEST rows are Ha>=17-18, not Ha=16\n{'='*88}")
    print(f"  {'Ha':>3}  {'cands':>5}  {'railed':>12}  {'tradeable':>9}  {'win%':>5}  {'Kelly P&L':>10}  "
          f"{'avg entry':>9}  {'contested-Brier gap':>19}")
    for Ha in hours:
        rows = [e for e in evals if e["Ha"] == Ha]
        if not rows:
            print(f"  {Ha:>3}  (no data)")
            continue
        nrail = sum(1 for e in rows if e["railed"])
        trade = [e for e in rows if e["passes"]]
        ntr, nwin, pnl, pev = _kelly_pnl(trade)
        avg_entry = statistics.mean([e["entry"] for e in trade]) if trade else 0.0
        con = [e for e in rows if e["contested"]]
        bg = ""
        if len(con) >= 6:
            mdl = brier([(e["model_p"], e["won_bucket"]) for e in con])
            mkt = brier([(e["price"], e["won_bucket"]) for e in con])
            bg = f"{mkt - mdl:+.4f} (n={len(con)})"
        print(f"  {Ha:>3}  {len(rows):>5}  {nrail:>5} ({nrail/len(rows):>4.0%})  {ntr:>9}  "
              f"{(nwin/ntr if ntr else 0):>4.0%}  ${pnl:>9,.0f}  {avg_entry:>9.0%}  {bg:>19}")

    # ===== (B) OOS split of the P&L at each action hour (does any survive both halves?) =====
    print(f"\n{'='*88}\n(B) TRADEABLE P&L per OOS half at each action hour (event-bootstrap 95% CI)\n{'='*88}")
    print(f"  {'Ha':>3}  {'half':>4}  {'trades':>6}  {'win%':>5}  {'P&L':>9}  {'95% CI':>22}  verdict")
    for Ha in hours:
        for hv in ("H1", "H2"):
            trade = [e for e in evals if e["Ha"] == Ha and e["passes"] and e["half"] == hv]
            ntr, nwin, pnl, pev = _kelly_pnl(trade)
            if ntr == 0:
                print(f"  {Ha:>3}  {hv:>4}  {0:>6}  {'--':>5}  {'$0':>9}  {'--':>22}  no trades")
                continue
            ci = _bootstrap_ci(pev)
            verdict = "POSITIVE" if ci[0] > 0 else ("NEGATIVE" if ci[1] < 0 else "spans 0")
            print(f"  {Ha:>3}  {hv:>4}  {ntr:>6}  {nwin/ntr:>4.0%}  ${pnl:>8,.0f}  "
                  f"[${ci[0]:>7,.0f}, ${ci[1]:>7,.0f}]  {verdict}")

    # ===== (C) HOW FAST THE WINDOW CLOSES — rail-out & tradeable count by action hour =====
    print(f"\n{'='*88}\n(C) HOW FAST THE WINDOW CLOSES — bucket rail-out by action hour\n"
          f"    (aggregate counts, not a per-bucket cohort: shows the market pricing to 0/1)\n{'='*88}")
    print(f"  buckets tradeable at Ha=16: {len([e for e in evals if e['Ha'] == 16 and e['passes']])}")
    for Ha in hours:
        rows = [e for e in evals if e["Ha"] == Ha]
        rail = sum(1 for e in rows if e["railed"])
        trade = sum(1 for e in rows if e["passes"])
        print(f"    Ha={Ha}: buckets railed={rail}/{len(rows)} ({rail/len(rows) if rows else 0:.0%}), "
              f"still tradeable={trade}")

    print("\nInterpretation: if 'tradeable' and P&L are healthy at Ha=16 but collapse (rails rise, "
          "P&L -> 0 / CI spans 0) by Ha=17-18, the edge lived in a window our Meteostat-gated bot "
          "cannot reach -> not live-capturable. If they hold at Ha>=18, it IS reachable and the fix "
          "is a faster observed-high source.")


if __name__ == "__main__":
    asyncio.run(main())
