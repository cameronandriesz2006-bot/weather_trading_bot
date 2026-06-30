"""Edge-1 event-driven P&L backtest — is impossible-bucket capture worth pursuing?

Tests the REAL strategy: scan -> our real-time obs says a bucket is HARD-impossible
(the day's extreme has already passed its edge) -> take NO at the ask -> hold to
settlement (collect $1). Built to dodge every trap the 2026-06-29 audit flagged:

  * TRIGGER from the EXACT settlement source, with honest publication latency.
    US cities settle on NWS stations (KLGA/KORD/KMIA/KBKF/KLAX) -> we read NWS METAR
    per-ob (the same thermometer Polymarket settles on), so "impossible" is a fact, not
    a forecast, and provider-mismatch ~ 0. t_cross_true = first NWS ob whose running
    extreme crosses the bucket edge; we can only ACT at t_cross + L_obs (METAR publish
    lag), rounded up to the next scan tick.

  * FILL/DEPTH from REAL historical trades (data-api/trades), NOT a guessed book.
    A NO-token BUY at price q after the crossing is liquidity that was actually takeable
    at q (profit 1-q). We capture a fraction f of the NO-buy volume in [t_act, t_act+win]
    at q <= cap_price, cheapest-first, up to a per-event $cap. Trades UNDERCOUNT resting
    asks, so this is a conservative (lower-bound) measure of takeable depth.

  * NO win-cherry-picking: where our obs wrongly calls a WON bucket impossible (provider
    mismatch), we buy NO and LOSE the stake — those losses are included.

  * Race modelled by pricing at the actual post-latency moment from the real tape: if the
    market already repriced, the cheap NO is gone and we capture ~nothing, automatically.

Output: per operating point (L_obs x window x f), net P&L, $ deployed, ROI, wins/losses,
net $/event with an EVENT-bootstrap CI, plus the universe "money on the table". The one
thing it cannot settle (true resting depth + our real latency) needs a live shadow run.

Run:  PYTHONPATH=. venv/bin/python -m backend.data.edge1_backtest --months 2,3,4,5,6
"""
import argparse
import asyncio
import json
import random
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx

from backend.data.weather import CITY_CONFIG
from backend.data.weather_markets import parse_event_slug, parse_bucket_label
from backend.data.calibration_backfill import GAMMA, _outcome_prices
from backend.data.freemoney_scan import fetch_events, _impossible

HDR = {"User-Agent": "Mozilla/5.0"}
TRADES_URL = "https://data-api.polymarket.com/trades"
IEM_ASOS = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
US_CITIES = ["nyc", "chicago", "miami", "denver", "los_angeles"]   # settle on NWS/ASOS stations
# Polymarket-settlement NWS station (CITY_CONFIG.nws_station) -> IEM ASOS id (drop the K).
IEM_ID = {"nyc": "LGA", "chicago": "ORD", "miami": "MIA", "denver": "BKF", "los_angeles": "LAX"}
random.seed(20260630)


# --------------------------------------------------------------------- ASOS obs (historical)
async def fetch_iem_city(client, city, start, end, tz, sem) -> Dict[date, List[Tuple[int, float]]]:
    """Whole-span per-METAR temperature, keyed by station-LOCAL day:
    {local_date: [(utc_ts, temp_F)]}.

    Source = IEM ASOS archive of the EXACT station Polymarket settles US markets on (KORD,
    KLGA, ...). The live NWS api.weather.gov feed only retains ~a week, so for history we read
    IEM — the same ASOS METARs (routine ~hourly at :51 + specials) the live bot would key off,
    so the crossing time is faithful to production, not a proxy. One request per station for
    the full span (IEM throttles per-date bursts), retried a few times."""
    out: Dict[date, List[Tuple[int, float]]] = defaultdict(list)
    station = IEM_ID.get(city)
    if not station:
        return out
    text = None
    async with sem:
        for _ in range(4):
            try:
                r = await client.get(IEM_ASOS, params={
                    "station": station, "data": "tmpf",
                    "year1": start.year, "month1": start.month, "day1": start.day,
                    "year2": (end + timedelta(days=2)).year, "month2": (end + timedelta(days=2)).month,
                    "day2": (end + timedelta(days=2)).day,
                    "tz": "Etc/UTC", "format": "onlytdf", "latlon": "no", "missing": "M", "trace": "T"})
                if r.status_code == 200 and "tmpf" in r.text[:40]:
                    text = r.text
                    break
            except Exception:
                pass
            await asyncio.sleep(3)
    if not text:
        return out
    for line in text.splitlines()[1:]:                 # skip header
        parts = line.split("\t")
        if len(parts) < 3 or parts[2] in ("M", "T", ""):
            continue
        try:
            ts = int(datetime.strptime(parts[1], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())
            tmpf = float(parts[2])                      # already °F (US cities)
        except (ValueError, IndexError):
            continue
        out[datetime.fromtimestamp(ts, tz).date()].append((ts, tmpf))
    for d in out:
        out[d].sort()
    return out


def cross_ts(series, bucket, metric) -> Optional[int]:
    """UTC ts of the first ob whose running extreme makes the bucket impossible, else None."""
    run = None
    for ts, t in series:
        run = t if run is None else (max(run, t) if metric == "high" else min(run, t))
        if _impossible(bucket, run, metric):
            return ts
    return None


def full_extreme(series, metric) -> Optional[float]:
    if not series:
        return None
    return max(t for _, t in series) if metric == "high" else min(t for _, t in series)


# --------------------------------------------------------------------- trades
async def fetch_no_trades(client, cond, no_token, sem) -> List[Tuple[int, str, float, float]]:
    """All trades on the NO token for a bucket's condition: [(ts, side, price, size)]."""
    out = []
    async with sem:
        for off in range(0, 6000, 500):
            try:
                r = await client.get(TRADES_URL, params={"market": cond, "limit": 500, "offset": off})
                if r.status_code >= 400:
                    break
                page = r.json()
            except Exception:
                break
            if not page:
                break
            for t in page:
                if t.get("asset") != no_token:
                    continue
                try:
                    out.append((int(t["timestamp"]), t["side"], float(t["price"]), float(t["size"])))
                except (KeyError, ValueError, TypeError):
                    continue
            if len(page) < 500:
                break
    out.sort()
    return out


# --------------------------------------------------------------------- sim
def simulate(no_trades, t_cross, won, L_obs, window, cadence, f, cap, cap_price, fee):
    """One operating point. Returns (pnl, cash_deployed, contracts) or None if no fill.
    We act at t_cross + L_obs rounded up to the next scan tick, then take a fraction f of
    the discounted NO-BUY volume in the window, cheapest-first, up to $cap. NO settles to
    $1 if the bucket truly lost, $0 if it actually won (provider-mismatch loss)."""
    t_known = t_cross + L_obs * 60
    t_act = ((t_known + cadence * 60 - 1) // (cadence * 60)) * (cadence * 60)
    fills = sorted([(p, s) for (ts, side, p, s) in no_trades
                    if side == "BUY" and t_act <= ts <= t_act + window * 60 and p <= cap_price],
                   key=lambda x: x[0])
    if not fills:
        return None
    cash = contracts = 0.0
    for p, s in fills:
        take = s * f
        if cash + take * p > cap:
            take = max(0.0, (cap - cash) / p)
        if take <= 0:
            break
        contracts += take
        cash += take * p
        if cash >= cap - 1e-9:
            break
    if contracts <= 0:
        return None
    settle = 0.0 if won else 1.0          # NO pays 1 if the bucket lost (impossible held)
    pnl = contracts * settle - cash - fee * cash
    return pnl, cash, contracts


def boot_ci(per_event_pnls, nb=5000):
    if len(per_event_pnls) < 4:
        return (float("nan"), float("nan"))
    n = len(per_event_pnls)
    tot = []
    for _ in range(nb):
        tot.append(sum(per_event_pnls[random.randrange(n)] for _ in range(n)))
    tot.sort()
    return tot[int(0.025 * nb)], tot[int(0.975 * nb)]


async def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cities", type=str, default=None, help="default = US/NWS cities")
    ap.add_argument("--months", type=str, default="2,3,4,5,6")
    ap.add_argument("--cap", type=float, default=200.0, help="$ per-event position cap")
    ap.add_argument("--cap-price", type=float, default=0.95, help="max NO price we'd pay")
    ap.add_argument("--fee", type=float, default=0.0)
    ap.add_argument("--cadence", type=int, default=5, help="scan tick minutes")
    args = ap.parse_args()

    cities = ([c.strip() for c in args.cities.split(",")] if args.cities else US_CITIES)
    months = set(int(m) for m in args.months.split(","))
    print(f"cities={cities}  months={sorted(months)}  cap=${args.cap}  cap_price={args.cap_price}  "
          f"cadence={args.cadence}min  fee={args.fee}")

    earliest = date(2026, min(months), 1)
    async with httpx.AsyncClient(timeout=45.0, headers=HDR) as client:
        events = await fetch_events(client, closed=True, earliest=earliest, cities=set(cities))
    raw = []   # (city, metric, tdate, buckets-with-cond)
    for e in events:
        pe = parse_event_slug(e.get("slug", ""))
        if not pe:
            continue
        city, metric, tdate = pe
        if city not in cities or tdate >= date.today() or tdate.month not in months:
            continue
        buckets, winners = [], 0
        for m in e.get("markets", []):
            rng = parse_bucket_label(m.get("groupItemTitle") or "")
            op = _outcome_prices(m)
            if rng is None or op is None:
                continue
            ids = m.get("clobTokenIds")
            if isinstance(ids, str):
                ids = json.loads(ids)
            won = op[0] > 0.9
            winners += 1 if won else 0
            buckets.append({"low": rng[0], "high": rng[1], "won": won,
                            "no_token": ids[1] if ids and len(ids) > 1 else None,
                            "cond": m.get("conditionId"), "label": m.get("groupItemTitle")})
        if winners == 1 and len(buckets) >= 2:
            raw.append((city, metric, tdate, buckets))
    by_city = defaultdict(int)
    for c, _, _, _ in raw:
        by_city[c] += 1
    print(f"{len(raw)} resolved US events: {dict(by_city)}")

    # ASOS obs — one whole-span request per station (IEM throttles per-date bursts)
    dates_by_city = defaultdict(list)
    for city, _, tdate, _ in raw:
        dates_by_city[city].append(tdate)
    city_list = list(dates_by_city)
    sem_iem = asyncio.Semaphore(2)
    async with httpx.AsyncClient(timeout=120.0) as client:
        obs_lists = await asyncio.gather(*[
            fetch_iem_city(client, c, min(dates_by_city[c]), max(dates_by_city[c]),
                           ZoneInfo(CITY_CONFIG[c]["tz"]), sem_iem) for c in city_list])
    obs_by_city = dict(zip(city_list, obs_lists))
    for c in city_list:
        print(f"  ASOS {c}: {sum(len(v) for v in obs_by_city[c].values())} obs over "
              f"{len(obs_by_city[c])} days")
    evs = [(city, metric, tdate, buckets, obs_by_city.get(city, {}).get(tdate, []))
           for city, metric, tdate, buckets in raw]

    # provider-match check (NWS obs full-day extreme vs winner) + collect impossible candidates
    pm = defaultdict(lambda: [0, 0])      # city -> [n, false_impossible]
    cand = []     # (city, metric, tdate, bucket, t_cross, won)
    for city, metric, tdate, buckets, ser in evs:
        if not ser:
            continue
        ext = full_extreme(ser, metric)
        winner = next((b for b in buckets if b["won"]), None)
        if winner is not None and ext is not None:
            pm[city][0] += 1
            if _impossible(winner, ext, metric):
                pm[city][1] += 1     # we'd wrongly kill the winner -> obs disagrees w/ settlement
        for b in buckets:
            if not b["no_token"] or not b["cond"]:
                continue
            tc = cross_ts(ser, b, metric)
            if tc is not None:
                cand.append((city, metric, tdate, b, tc, b["won"]))
    print(f"\nNWS provider-match (full-day obs extreme vs winning bucket):")
    eligible = set()
    for city in sorted(pm):
        n, fi = pm[city]
        ok = n > 0 and fi / n <= 0.02
        if ok:
            eligible.add(city)
        print(f"  {city:12} n={n:>4}  false-impossible={fi} ({(fi/n if n else 0):.0%})"
              f"{'  ELIGIBLE' if ok else '  <-- obs != settlement, EXCLUDE'}")
    cand = [c for c in cand if c[0] in eligible]
    print(f"\n{len(cand)} impossible-bucket opportunities in eligible cities; fetching NO-token trades...")

    sem_tr = asyncio.Semaphore(10)
    async with httpx.AsyncClient(timeout=30.0, headers=HDR) as client:
        trades = await asyncio.gather(*[fetch_no_trades(client, b["cond"], b["no_token"], sem_tr)
                                        for (_, _, _, b, _, _) in cand])

    # ---- universe "money on the table": f=1, full window after crossing, no cap ----
    mot = 0.0
    mot_loss = 0.0
    for (city, metric, tdate, b, tc, won), tr in zip(cand, trades):
        for ts, side, p, s in tr:
            if side == "BUY" and ts >= tc and p <= 0.97:
                if won:
                    mot_loss += s * p          # false-impossible: we'd lose the stake
                else:
                    mot += s * (1.0 - p)       # true impossible: profit per contract
    print(f"\n{'='*78}\nUNIVERSE 'money on the table' (every discounted NO-buy after the crossing,\n"
          f"f=1, no cap, no latency) = GROSS UPPER BOUND on the whole opportunity\n{'='*78}")
    print(f"  gross profit available = ${mot:,.0f}   provider-mismatch losses = ${mot_loss:,.0f}   "
          f"NET ceiling = ${mot - mot_loss:,.0f}   (over {len(set((c,d) for c,_,d,_,_,_ in cand))} city-days)")

    # ---- realistic operating points -------------------------------------------
    print(f"\n{'='*78}\nREALISTIC operating points (cap=${args.cap}/event, cadence={args.cadence}min, "
          f"cap_price={args.cap_price})\n{'='*78}")
    print(f"  {'L_obs':>5} {'win':>4} {'f':>4} {'n_fill':>6} {'gross$':>8} {'losses$':>8} "
          f"{'NET$':>8} {'deployed$':>9} {'ROI':>6} {'$/op':>6} {'95% CI $/op':>20}")
    for L_obs in (2, 5, 10, 20):
        for window in (10, 30):
            for f in (1.0, 0.3):
                ev_pnl = defaultdict(float)
                ev_cash = defaultdict(float)
                n_fill = wins = losses = 0
                tot_pnl = tot_cash = 0.0
                for (city, metric, tdate, b, tc, won), tr in zip(cand, trades):
                    res = simulate(tr, tc, won, L_obs, window, args.cadence, f,
                                   args.cap, args.cap_price, args.fee)
                    if res is None:
                        continue
                    pnl, cash, contracts = res
                    key = (city, tdate, metric)
                    ev_pnl[key] += pnl
                    ev_cash[key] += cash
                    n_fill += 1
                    tot_pnl += pnl
                    tot_cash += cash
                    if won:
                        losses += 1
                    else:
                        wins += 1
                per_ev = list(ev_pnl.values())
                lo, hi = boot_ci(per_ev)
                roi = (tot_pnl / tot_cash) if tot_cash else 0.0
                dpe = (tot_pnl / len(per_ev)) if per_ev else 0.0
                gross = sum(p for p in ev_pnl.values() if p > 0)  # approx; net below is the truth
                tag = f"L{L_obs} w{window}"
                print(f"  {tag:>7} {'':>0} f={f:<3} {n_fill:>6} {'':>8} "
                      f"{'':>8} {tot_pnl:>8.0f} {tot_cash:>9.0f} {roi:>5.0%} {dpe:>6.1f} "
                      f"[{lo:>+7.0f},{hi:>+7.0f}]  (events w/ fill={len(per_ev)}, wins={wins} losses={losses})")

    print(f"\n  NET$ = total P&L across all events at that operating point (incl. provider-mismatch")
    print(f"  losses); $/op = NET / events-with-a-fill; CI = event-bootstrap on total NET.")


if __name__ == "__main__":
    asyncio.run(main())
