"""Edge-2 PUBLISH-TIME-HONEST backtest — the go/no-go with no obs look-ahead at all.

`edge2_execution_honest` swept the action hour Ha but still fed the model ARCHIVED
(final, revised) hourly obs — i.e. it assumed that at Ha the bot knows everything the
station had measured by Ha. Live, it doesn't: an observation only becomes actionable at
its PUBLISH time. The 2026-07-01 autopsy showed this gap is fatal-sized (Meteostat fed a
floor 1.3-3.4F low; and even a perfect feed lags: KBKF reports HOURLY at :58 +10-15 min
publish, so a new high set at :05 is unknowable for ~70 min).

This script closes the last look-ahead:
  - obs = IEM ASOS METARs (routine + specials) — the settlement-grade series (per-ob
    Wunderground integer-F rounding BEFORE max, validated 54/54 city-days in the
    settled bucket vs Gamma resolutions, Jun 10 - Jul 1).
  - knowledge gating: at decision time Ha:30 (the mid-point of a 15-min-scan hour) the
    model sees only obs with ob_time + LATENCY <= Ha:30. LATENCY defaults to 15 min
    (measured NWS/AWC publish delay). Station cadence is inherited from the real METAR
    timestamps, so KBKF's hourly rhythm handicaps Denver automatically.
  - everything else (events, forecasts, bias, sigma curve, gates, rail filter, sizing)
    is byte-identical to edge2_execution_honest = the live pipeline.

Also doubles as the CITY SCREEN: run with all six US cities and read section (D).

Run: PYTHONPATH=. venv/bin/python -m backend.data.edge2_publish_honest \
       --cities denver,chicago,atlanta,nyc,miami,los_angeles \
       --hours 16,17,18,19 --months 2,3,4,5,6 --split 2026-05-01 --latency-min 15
"""
import argparse
import asyncio
import csv
import io
import math
import statistics
from collections import defaultdict
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import httpx

from backend.config import settings
from backend.data.weather import CITY_CONFIG, get_station_bias
from backend.data.calibration_backfill import extract_event, fetch_blend_means, brier
from backend.data.calibration_intraday import model_prob_at_hour, _fetch_day_history, _price_at
from backend.core.sizing import calculate_edge
from backend.data.edge2_backtest import bucket_center, _c_scale
from backend.data.edge2_oos_backtest import fetch_both_ends, _bootstrap_ci, HDR
from backend.data.edge2_execution_honest import RAIL_LO, RAIL_HI, _kelly_pnl

IEM_ASOS = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
DECISION_MINUTE = 30   # act at Ha:30 — the expected position of a 15-min scan inside hour Ha


def _wu_round(f: float) -> float:
    """Wunderground displays each ob as integer F (round half up); settlement max is the
    max of those integers — so round per-ob BEFORE the running extreme."""
    return float(math.floor(f + 0.5))


async def fetch_iem_known_obs(client, icao: str, start: date, end: date, tzname: str,
                              latency_min: int, metric: str):
    """{local_date: {H: running extreme over obs KNOWN by H:30}} from IEM METARs.

    An ob taken at t is known at t + latency_min. Values are per-ob WU-rounded F.
    The dict is running (monotone), so calibration_intraday._observed_so_far's
    max-over-hours<=H returns exactly the known-by-H:30 extreme."""
    r = await client.get(IEM_ASOS, params={
        "station": icao.lstrip("K"), "data": "tmpf",
        "year1": start.year, "month1": start.month, "day1": start.day,
        "year2": end.year, "month2": end.month, "day2": end.day,
        "tz": tzname, "format": "onlycomma", "missing": "M", "report_type": "3,4"},
        timeout=180.0)
    r.raise_for_status()
    per_day = defaultdict(list)   # local_date -> [(known_minute_of_day, roundedF)]
    for row in csv.DictReader(io.StringIO(r.text)):
        if row.get("tmpf") in ("M", "", None):
            continue
        v = row["valid"]  # local "YYYY-MM-DD HH:MM"
        try:
            d = date(int(v[:4]), int(v[5:7]), int(v[8:10]))
            known = int(v[11:13]) * 60 + int(v[14:16]) + latency_min
        except (ValueError, IndexError):
            continue
        per_day[d].append((known, _wu_round(float(row["tmpf"]))))
    out = {}
    agg = max if metric == "high" else min
    for d, obs in per_day.items():
        obs.sort()
        hours, run = {}, None
        i = 0
        for H in range(24):
            cutoff = H * 60 + DECISION_MINUTE
            while i < len(obs) and obs[i][0] <= cutoff:
                run = obs[i][1] if run is None else agg(run, obs[i][1])
                i += 1
            if run is not None:
                hours[H] = run
        out[d] = hours
    return out


async def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cities", type=str, default="denver,chicago,atlanta,nyc,miami,los_angeles")
    ap.add_argument("--hours", type=str, default="16,17,18,19")
    ap.add_argument("--months", type=str, default="2,3,4,5,6")
    ap.add_argument("--split", type=str, default="2026-05-01")
    ap.add_argument("--latency-min", type=int, default=15)
    args = ap.parse_args()

    cities = [c.strip() for c in args.cities.split(",")]
    models = [m.strip() for m in settings.WEATHER_BLEND_MODELS.split(",") if m.strip()]
    hours = [int(h) for h in args.hours.split(",")]
    months = set(int(m) for m in args.months.split(","))
    split = date.fromisoformat(args.split)

    def half(d: date) -> str:
        return "H1" if d < split else "H2"

    print(f"PUBLISH-TIME-HONEST  cities={cities}  Ha={hours}  months={sorted(months)}  "
          f"split={split}  latency={args.latency_min}min  decision=Ha:{DECISION_MINUTE:02d}\n"
          f"edge_gate={settings.WEATHER_MIN_EDGE_THRESHOLD}  entry_cap={settings.WEATHER_MAX_ENTRY_PRICE}  "
          f"spread={settings.WEATHER_DEFAULT_SPREAD}  rail=({RAIL_LO},{RAIL_HI})")

    async with httpx.AsyncClient(timeout=60.0, headers=HDR) as client:
        events = await fetch_both_ends(client)
        raw, dts = [], defaultdict(list)
        for e in events:
            ex = extract_event(e)
            if not ex:
                continue
            city, metric, tdate, buckets = ex
            if (city not in cities or metric != "high" or tdate >= date.today()
                    or tdate.month not in months):
                continue
            raw.append((city, metric, tdate, buckets))
            dts[city].append(tdate)
        nh1 = sum(1 for r in raw if half(r[2]) == "H1")
        print(f"{len(raw)} resolved in-scope events  (H1={nh1}, H2={len(raw)-nh1})")

        means, obs_days, tzs = {}, {}, {}
        for city, d in dts.items():
            cfg = CITY_CONFIG.get(city)
            if not cfg or not cfg.get("nws_station"):
                continue
            lo, hi = min(d) - timedelta(days=1), max(d) + timedelta(days=1)
            tzs[city] = cfg.get("tz") or "UTC"
            means[city] = await fetch_blend_means(client, cfg, models, lo, hi)
            obs_days[city] = await fetch_iem_known_obs(
                client, cfg["nws_station"], lo, hi, tzs[city], args.latency_min, "high")
            ndays = len(obs_days[city])
            nobs_day = statistics.mean([len(v) for v in obs_days[city].values()]) if ndays else 0
            print(f"  {city}: {ndays} obs-days from IEM ({cfg['nws_station']}), "
                  f"avg {nobs_day:.0f} known-hours/day")

    tok_date = {b["yes_token"]: tdate.isoformat()
                for city, metric, tdate, buckets in raw for b in buckets if b["yes_token"]}
    toks = list(tok_date)
    print(f"fetching market history for {len(toks)} bucket tokens...")
    sem = asyncio.Semaphore(12)
    async with httpx.AsyncClient(timeout=30.0, headers=HDR) as client:
        hists = await asyncio.gather(*[_fetch_day_history(client, t, tok_date[t], sem) for t in toks])
    hist = dict(zip(toks, hists))

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
                res = model_prob_at_hour(city, city, tdate, unit, metric,
                                         b["low"], b["high"], fc, obs_hours, Ha)
                if res is None:
                    continue
                model_p, floor_active = res
                if not floor_active:
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

    # ===== (A) edge vs action hour =====
    print(f"\n{'='*88}\n(A) EDGE vs ACTION HOUR — all knowledge at real publish time\n{'='*88}")
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

    # ===== (B) OOS split =====
    print(f"\n{'='*88}\n(B) TRADEABLE P&L per OOS half (event-bootstrap 95% CI)\n{'='*88}")
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

    # ===== (D) CITY SCREEN — per city x Ha, contested Brier per half + tradeable P&L =====
    print(f"\n{'='*88}\n(D) CITY SCREEN — contested Brier gap (mkt−mdl, + = model better) per half, "
          f"and tradeable P&L\n{'='*88}")
    print(f"  {'city':12} {'Ha':>3}  {'H1 gap (n)':>16}  {'H2 gap (n)':>16}  {'trades':>6}  {'P&L':>9}  {'CI':>20}")
    for city in cities:
        for Ha in hours:
            rows = [e for e in evals if e["city"] == city and e["Ha"] == Ha]
            if not rows:
                continue
            cells = []
            for hv in ("H1", "H2"):
                con = [e for e in rows if e["half"] == hv and e["contested"]]
                if len(con) >= 6:
                    mdl = brier([(e["model_p"], e["won_bucket"]) for e in con])
                    mkt = brier([(e["price"], e["won_bucket"]) for e in con])
                    cells.append(f"{mkt-mdl:+.4f} ({len(con):>3})")
                else:
                    cells.append(f"-- ({len(con):>3})")
            trade = [e for e in rows if e["passes"]]
            ntr, nwin, pnl, pev = _kelly_pnl(trade)
            ci_s = ""
            if ntr:
                ci = _bootstrap_ci(pev)
                ci_s = f"[{ci[0]:>7,.0f},{ci[1]:>7,.0f}]"
            print(f"  {city:12} {Ha:>3}  {cells[0]:>16}  {cells[1]:>16}  {ntr:>6}  ${pnl:>8,.0f}  {ci_s:>20}")

    print("\nRead: a city earns its slot only if the contested Brier gap is POSITIVE in BOTH halves "
          "at a reachable hour (Ha>=16 for 5-min stations, Ha>=17 realistically for hourly KBKF) "
          "AND tradeable P&L doesn't contradict it.")


if __name__ == "__main__":
    asyncio.run(main())
