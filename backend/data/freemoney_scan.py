"""Edge-1 "free money" scanner — locked-out buckets the market still prices > 0.

The thesis (Edge 1, the priority): on the in-progress local day, once the day's
extreme has ALREADY passed a bucket, that bucket is impossible — yet the market
sometimes still prices its YES above zero. Buying NO there is locked profit that
needs NO forecast skill at all: it's the thermometer, not out-guessing the market,
so it survives a sharp market. This harness measures whether that free money is
RECURRING, WHERE, WHEN, and HOW MUCH — in dollars and liquidity, not Brier.

What it does, per resolved Polymarket daily-temperature event:
  1. Reconstruct observed-so-far (running max/min of the station's hourly obs up to
     each station-local hour H) — the same Meteostat feed the live observed-floor reads.
  2. Flag buckets already IMPOSSIBLE at H (high already >= bucket top+0.5, or low
     already <= bucket bottom-0.5 — the bucket's rounding edge).
  3. Pull each impossible bucket's market YES price at H from CLOB prices-history.
     A YES price still > 0 there is the mispricing (gross locked profit = that price).

Three outputs:
  A. PROVIDER-MATCH VALIDATION — does our obs source agree with Polymarket's
     settlement? For each event we take the full-day observed extreme and check it
     lands in the WINNING bucket and never marks the winner "impossible". If a city's
     obs disagrees with settlement, its "impossible" calls are NOT trustworthy and its
     free money is a mirage. This is the verification the edge stands or falls on.
  B. FREE-MONEY FREQUENCY/SIZE (historical) — per city & hour: count of
     impossible-but-priced buckets above a cent threshold, and the mispricing size (c).
     prices-history gives only a price, NOT depth, so this is the gross opportunity.
  C. LIVE DEPTH PROBE — historical order-book depth is gone (resolved books are
     empty), so depth (the "is it a business" question) can only be measured live:
     fetch currently-OPEN markets, find buckets impossible RIGHT NOW, and walk the
     real NO-token book (orderbook.py) to report the $ / contracts actually takeable.

Run:  PYTHONPATH=. venv/bin/python -m backend.data.freemoney_scan
      ... --hours 7,10,13,16,18,20  --months 5,6,7,8,9  --min-cents 2,5,10
      ... --cities nyc,chicago,denver,tokyo,paris,hong_kong  --live-depth
"""
import argparse
import asyncio
import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx

from backend.config import settings
from backend.data.weather import CITY_CONFIG, METEOSTAT_STATION
from backend.data.weather_markets import parse_event_slug, parse_bucket_label
from backend.data.calibration_backfill import GAMMA, _outcome_prices
from backend.data.calibration_intraday import (fetch_hourly_obs, _observed_so_far,
                                               _fetch_day_history, _price_at)
from backend.data.orderbook import fetch_books, walk_asks_for_cash

HEADERS = {"User-Agent": "Mozilla/5.0"}
DEFAULT_HOURS = [7, 10, 13, 16, 18, 20]


# --------------------------------------------------------------------------- events
def _tokens(market: dict) -> Tuple[Optional[str], Optional[str]]:
    """(yes_token, no_token) from clobTokenIds; Polymarket binary = [yes, no]."""
    ids = market.get("clobTokenIds")
    if isinstance(ids, str):
        try:
            ids = json.loads(ids)
        except Exception:
            return None, None
    if not ids or len(ids) < 2:
        return (ids[0] if ids else None), None
    return ids[0], ids[1]


def extract_event_full(event: dict, require_resolved: bool):
    """-> (city, metric, target_date, buckets) with each bucket carrying
    {low, high, won, yes_token, no_token, label}. require_resolved=True keeps only
    cleanly-settled events (exactly one winner) for the historical pass; False keeps
    open events (no winner yet) for the live-depth probe."""
    pe = parse_event_slug(event.get("slug", ""))
    if not pe:
        return None
    city, metric, tdate = pe
    buckets, winners = [], 0
    for m in event.get("markets", []):
        rng = parse_bucket_label(m.get("groupItemTitle") or "")
        if rng is None:
            continue
        yes, no = _tokens(m)
        op = _outcome_prices(m)
        won = bool(op and op[0] > 0.9)
        if require_resolved:
            if op is None:
                # an unpriced bucket: if it's the (unknown) winner we'd mislabel -> bail
                continue
            if won:
                winners += 1
        buckets.append({"low": rng[0], "high": rng[1], "won": won,
                        "yes_token": yes, "no_token": no,
                        "label": m.get("groupItemTitle") or ""})
    if require_resolved and (winners != 1 or len(buckets) < 2):
        return None
    if not require_resolved and len(buckets) < 2:
        return None
    return city, metric, tdate, buckets


async def fetch_events(client, *, closed: bool, earliest: date, cities: set) -> List[dict]:
    """Closed (resolved) OR open daily-temperature events, MOST-RECENT FIRST.

    The default Gamma closed feed returns oldest-first and 422s past offset ~2000,
    so it never reaches recent months (the bug that made the May-Sep scan empty).
    order=endDate&ascending=false pages from the newest backward; we stop once events
    fall before `earliest`."""
    out, seen_old = [], 0
    for off in range(0, 8000, 100):
        params = {"closed": "true" if closed else "false", "limit": 100, "offset": off,
                  "tag_slug": "daily-temperature", "order": "endDate", "ascending": "false"}
        try:
            r = await client.get(GAMMA, params=params)
        except Exception:
            break
        if r.status_code >= 400:
            break
        page = r.json()
        if not page:
            break
        stop = False
        for e in page:
            pe = parse_event_slug(e.get("slug", ""))
            if not pe:
                continue
            _, _, tdate = pe
            if tdate < earliest:
                seen_old += 1
                if seen_old > 150:        # comfortably past the window -> stop paging
                    stop = True
                continue
            out.append(e)
        if stop or len(page) < 100:
            break
    return out


# --------------------------------------------------------------------------- impossibility
def _impossible(bucket: dict, obs: float, metric: str) -> bool:
    """Is this bucket already ruled out by the observed-so-far extreme `obs`?
    The settlement value rounds into [low-0.5, high+0.5). For a HIGH the final value
    can only be >= obs, so any bucket whose top rounding-edge is below obs is dead;
    symmetric for a LOW (final <= obs)."""
    if metric == "high":
        return bucket["high"] is not None and obs >= bucket["high"] + 0.5
    return bucket["low"] is not None and obs <= bucket["low"] - 0.5


def _bucket_contains(bucket: dict, val: float) -> bool:
    lo = (bucket["low"] - 0.5) if bucket["low"] is not None else float("-inf")
    hi = (bucket["high"] + 0.5) if bucket["high"] is not None else float("inf")
    return lo <= val < hi


# --------------------------------------------------------------------------- main
async def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cities", type=str, default=None)
    ap.add_argument("--hours", type=str, default=None)
    ap.add_argument("--months", type=str, default=None, help="restrict to these target months")
    ap.add_argument("--min-cents", type=str, default="2,5,10", help="YES-price thresholds (cents)")
    ap.add_argument("--market-sample", type=int, default=600, help="max events for the market-price pass")
    ap.add_argument("--live-depth", action="store_true", help="probe live NO-book depth on open markets")
    args = ap.parse_args()

    cities = set(c.strip() for c in args.cities.split(",")) if args.cities else \
        set(c.strip() for c in settings.WEATHER_CITIES.split(",") if c.strip())
    hours = [int(h) for h in args.hours.split(",")] if args.hours else DEFAULT_HOURS
    months = set(int(m) for m in args.months.split(",")) if args.months else None
    thresholds = sorted(float(c) / 100.0 for c in args.min_cents.split(","))
    print(f"cities={sorted(cities)}\nhours(local)={hours}  months={sorted(months) if months else 'all'}  "
          f"cent-thresholds={[int(t*100) for t in thresholds]}")

    # ---- fetch resolved events (recent-first) -------------------------------
    earliest = date(2026, min(months), 1) if months else date(2026, 1, 1)
    async with httpx.AsyncClient(timeout=45.0, headers=HEADERS) as client:
        events = await fetch_events(client, closed=True, earliest=earliest, cities=cities)
    raw, dates_by_city = [], defaultdict(list)
    for e in events:
        ex = extract_event_full(e, require_resolved=True)
        if not ex:
            continue
        city, metric, tdate, buckets = ex
        if city not in cities or tdate >= date.today():
            continue
        if months and tdate.month not in months:
            continue
        raw.append((city, metric, tdate, buckets))
        dates_by_city[city].append(tdate)
    print(f"{len(raw)} cleanly-resolved in-scope events across {len(dates_by_city)} cities "
          f"({', '.join(f'{c}:{len(d)}' for c,d in sorted(dates_by_city.items()))})")
    if not raw:
        print("no events — nothing to scan")
        return

    # ---- observed hourly obs per city (the obs the live floor reads) --------
    obs_days, tzs = {}, {}
    async with httpx.AsyncClient(timeout=60.0, headers=HEADERS) as client:
        for city, dts in dates_by_city.items():
            cfg, st = CITY_CONFIG.get(city), METEOSTAT_STATION.get(city)
            tzs[city] = ZoneInfo(cfg.get("tz") or "UTC")
            if not st:
                print(f"  ! {city}: no Meteostat station — cannot verify obs, skipping")
                continue
            obs_days[city] = await fetch_hourly_obs(
                client, st, min(dts) - timedelta(days=1), max(dts) + timedelta(days=1),
                cfg.get("tz") or "UTC", cfg.get("unit", "F"))

    # ============================ A. PROVIDER-MATCH VALIDATION ================
    # full-day observed extreme vs the actual winner: does our thermometer agree
    # with Polymarket's settlement? false_impossible = we'd have wrongly killed the winner.
    print(f"\n{'='*86}\nA. PROVIDER-MATCH VALIDATION (obs source vs Polymarket settlement)\n"
          f"   per city: events with full-day obs; agree=obs-extreme lands in winner bucket;\n"
          f"   FALSE-IMPOSSIBLE=obs would have wrongly marked the WINNER impossible (edge-killer)\n{'='*86}")
    print(f"  {'city':12} {'n':>4} {'agree':>7} {'adjacent':>9} {'FALSE-IMP':>10} {'mean|gap|':>9}")
    val_ok = {}
    for city in sorted(dates_by_city):
        if city not in obs_days:
            continue
        n = agree = adjacent = false_imp = 0
        gaps = []
        for c2, metric, tdate, buckets in raw:
            if c2 != city:
                continue
            oh = obs_days[city].get(tdate)
            if not oh:
                continue
            ext = _observed_so_far(oh, 23, metric)
            if ext is None:
                continue
            winner = next((b for b in buckets if b["won"]), None)
            if not winner:
                continue
            n += 1
            if _bucket_contains(winner, ext):
                agree += 1
            else:
                # signed gap of obs extreme outside the winner interval
                lo = (winner["low"] - 0.5) if winner["low"] is not None else None
                hi = (winner["high"] + 0.5) if winner["high"] is not None else None
                if hi is not None and ext >= hi:
                    gaps.append(ext - hi)
                    if ext < hi + 1.0:
                        adjacent += 1
                elif lo is not None and ext < lo:
                    gaps.append(lo - ext)
                    if ext > lo - 1.0:
                        adjacent += 1
            if _impossible(winner, ext, metric):
                false_imp += 1
        if n == 0:
            continue
        mg = (sum(gaps) / len(gaps)) if gaps else 0.0
        unit = CITY_CONFIG.get(city, {}).get("unit", "F")
        val_ok[city] = (false_imp / n) <= 0.02
        flag = "" if val_ok[city] else "   <-- obs UNRELIABLE for impossible-calls"
        print(f"  {city:12} {n:>4} {agree/n:>6.0%} {adjacent:>9} {false_imp:>4} ({false_imp/n:>4.0%}) "
              f"{mg:>7.2f}{unit}{flag}")
    print("  (agree should be ~100% and FALSE-IMP ~0% for the edge to be real; a non-zero\n"
          "   FALSE-IMP means our thermometer reads hotter/colder than the settlement source.)")

    # ============================ B. FREE-MONEY DURABILITY ===================
    # A bucket becomes impossible at the moment the running extreme crosses its edge.
    # The market sees the SAME temperature, so the danger is that it reprices to ~0
    # within minutes of the crossing (the Chicago 72-73 case: 94c at the crossing hour,
    # 0.1c one hour later). The only capturable edge is mispricing that PERSISTS long
    # enough that (a) we have the station ob and (b) we can take it. So we measure the
    # YES price at a LAG after each bucket's crossing, with a tight tolerance, requiring
    # a real quote AFTER the crossing — not a stale carry-forward from before it.
    LAGS = [0.0, 1.0, 2.0, 3.0]   # hours after the impossibility crossing
    TOL_H = 0.75                  # a matched quote must be within 45 min of the lag time

    # distinct impossible buckets, each with the local hour it first became impossible
    imp = {}    # (city,metric,date,token) -> {bucket, cross_hour}
    for city, metric, tdate, buckets in raw:
        if city not in obs_days or not val_ok.get(city, False):
            continue   # only cities whose obs provably matches settlement (section A)
        oh = obs_days[city].get(tdate)
        if not oh:
            continue
        for hh in range(0, 24):
            obs = _observed_so_far(oh, hh, metric)
            if obs is None:
                continue
            for b in buckets:
                if not b["yes_token"]:
                    continue
                key = (city, metric, tdate, b["yes_token"])
                if key not in imp and _impossible(b, obs, metric):
                    imp[key] = {"bucket": b, "cross": hh}
    print(f"\n{'='*86}\nB. FREE-MONEY DURABILITY: {len(imp)} distinct impossible buckets "
          f"(trusted cities only)\n   YES price at a LAG after the impossibility crossing "
          f"(tol +-45min; quote must post AFTER crossing)\n{'='*86}")

    # sample to bound CLOB calls; fetch each impossible token's day history once
    keys = sorted(imp)
    stride = max(1, len(keys) // args.market_sample)
    keep_keys = keys[::stride]
    tok_date = {k[3]: k[2].isoformat() for k in keep_keys}
    tokens = [k[3] for k in keep_keys]
    print(f"  fetching price history for {len(tokens)} impossible tokens...")
    sem = asyncio.Semaphore(12)
    async with httpx.AsyncClient(timeout=30.0, headers=HEADERS) as client:
        hists = await asyncio.gather(*[_fetch_day_history(client, t, tok_date[t], sem) for t in tokens])
    hist_by_token = dict(zip(tokens, hists))

    def _price_lag(hist, diso, tz, cross_hour, lag):
        """YES price nearest (cross_hour+lag), within TOL_H, and posted at/after the
        crossing instant — so we never read a pre-crossing (still-live) quote."""
        if not hist:
            return None
        midnight = int(datetime.fromisoformat(diso).replace(hour=0, tzinfo=tz).timestamp())
        cross_ts = midnight + int(cross_hour * 3600)
        target = midnight + int((cross_hour + lag) * 3600)
        after = [p for p in hist if p.get("t", 0) >= cross_ts - 60]
        if not after:
            return None
        best = min(after, key=lambda p: abs(p.get("t", 0) - target))
        if abs(best.get("t", 0) - target) > TOL_H * 3600:
            return None
        p = best.get("p")
        return float(p) if p is not None else None

    # aggregate per lag, and the realistic (+1h) cell per city/hour
    lag_stat = {lag: {"n": 0, "ge": defaultdict(int), "sum": 0.0} for lag in LAGS}
    rows1h = []                       # rows priced at +1h (the realistic capture)
    cross_hist = defaultdict(int)
    for k in keep_keys:
        rec = imp[k]
        city, metric, tdate, tok = k
        hist = hist_by_token.get(tok)
        cross_hist[rec["cross"]] += 1
        for lag in LAGS:
            mp = _price_lag(hist, tdate.isoformat(), tzs[city], rec["cross"], lag)
            if mp is None:
                continue
            st = lag_stat[lag]
            st["n"] += 1
            if mp >= thresholds[0]:
                st["sum"] += mp * 100
                for t in thresholds:
                    if mp >= t:
                        st["ge"][t] += 1
                if lag == 1.0:
                    rows1h.append((city, metric, tdate.isoformat(), rec["cross"], rec["bucket"]["label"], mp))

    print(f"\n  decay of the mispricing after a bucket goes impossible:")
    thr_hdr = " ".join(f">={int(t*100):>2}c" for t in thresholds)
    print(f"  {'lag':>5} {'n_quotes':>9} {thr_hdr} {'avg_c(>=2c)':>11}")
    for lag in LAGS:
        st = lag_stat[lag]
        if st["n"] == 0:
            print(f"  {('+%.0fh'%lag):>5} {0:>9}")
            continue
        ge = " ".join(f"{st['ge'][t]:>4}" for t in thresholds)
        npr = st["ge"][thresholds[0]]
        avg = (st["sum"] / npr) if npr else 0.0
        print(f"  {('+%.0fh'%lag):>5} {st['n']:>9} {ge} {avg:>11.1f}")
    print(f"  (+0h = the crossing instant, mostly the transient spike; +1h = earliest we'd\n"
          f"   realistically have the ob and could act; +2h/+3h = genuinely durable mispricing.)")

    print(f"\n  when buckets become impossible (local hour of crossing) — early = more time to act:")
    for hh in sorted(cross_hist):
        bar = "#" * (cross_hist[hh] * 40 // max(cross_hist.values()))
        print(f"    {hh:>2}:00  {cross_hist[hh]:>4}  {bar}")

    print(f"\n  CAPTURABLE rows (still >= {int(thresholds[0]*100)}c a full hour AFTER going impossible):")
    if not rows1h:
        print("    (none — every mispricing had decayed to <2c within an hour of the crossing)")
    else:
        rows1h.sort(key=lambda x: -x[5])
        byc = defaultdict(lambda: [0, 0.0])
        for city, metric, d, ch, label, mp in rows1h:
            byc[city][0] += 1
            byc[city][1] += mp * 100
        for city in sorted(byc):
            print(f"    {city:12} count={byc[city][0]:>3}  total_locked_cents={byc[city][1]:>6.0f}")
        print("    top:")
        for city, metric, d, ch, label, mp in rows1h[:12]:
            print(f"      {d} {city:10} {metric:4} cross={ch:>2}:00 '{label:14}' YES@+1h={mp*100:>5.1f}c")

    # ============================ C. LIVE DEPTH PROBE ========================
    if args.live_depth:
        trusted = {c for c, ok in val_ok.items() if ok}
        await live_depth_probe(cities & trusted, hours)


async def live_depth_probe(cities: set, hours: list):
    """Historical depth is unrecoverable (resolved books are empty), so measure depth
    NOW: open markets, buckets impossible at the current local hour, real NO-book walk."""
    print(f"\n{'='*86}\nC. LIVE DEPTH PROBE — open markets, impossible RIGHT NOW, real NO-book depth\n{'='*86}")
    today = date.today()
    async with httpx.AsyncClient(timeout=45.0, headers=HEADERS) as client:
        events = await fetch_events(client, closed=False, earliest=today, cities=cities)
    open_by_city = defaultdict(list)
    for e in events:
        ex = extract_event_full(e, require_resolved=False)
        if not ex:
            continue
        city, metric, tdate, buckets = ex
        if city in cities and tdate == today:
            open_by_city[city].append((metric, tdate, buckets))
    if not open_by_city:
        print("  (no open same-day markets for these cities right now)")
        return

    # current obs per city (today only)
    cand = []   # (city, metric, label, no_token, yes_token, obs, Hnow)
    async with httpx.AsyncClient(timeout=60.0, headers=HEADERS) as client:
        for city, evs in open_by_city.items():
            cfg, st = CITY_CONFIG.get(city), METEOSTAT_STATION.get(city)
            if not st:
                continue
            Hnow = datetime.now(ZoneInfo(cfg.get("tz") or "UTC")).hour
            oh_all = await fetch_hourly_obs(client, st, today - timedelta(days=1), today,
                                            cfg.get("tz") or "UTC", cfg.get("unit", "F"))
            oh = oh_all.get(today, {})
            for metric, tdate, buckets in evs:
                obs = _observed_so_far(oh, Hnow, metric)
                if obs is None:
                    continue
                for b in buckets:
                    if b["no_token"] and _impossible(b, obs, metric):
                        cand.append((city, metric, b["label"], b["no_token"], b["yes_token"], obs, Hnow))
    if not cand:
        print("  (no buckets are provably impossible at the current hour on open markets)")
        return

    no_tokens = [c[3] for c in cand]
    async with httpx.AsyncClient(timeout=20.0, headers=HEADERS) as client:
        books = await fetch_books(no_tokens + [c[4] for c in cand], client)
    print(f"  {len(cand)} impossible buckets live; walking the NO book for up to $200 each:")
    print(f"  {'city':10} {'metric':5} {'bucket':14} {'obs':>6} {'NO_ask':>7} {'$takeable':>10} "
          f"{'NO_vwap':>8} {'profit$':>8}")
    total_profit = 0.0
    for city, metric, label, no_tok, yes_tok, obs, Hnow in cand:
        lb = books.get(no_tok)
        if not lb or not lb.asks:
            continue
        fill = walk_asks_for_cash([(p, s) for p, s in lb.asks if p < 0.999], 200.0)
        if not fill:
            continue
        profit = fill.contracts - fill.cash   # each NO contract settles to $1
        total_profit += max(0.0, profit)
        unit = CITY_CONFIG.get(city, {}).get("unit", "F")
        print(f"  {city:10} {metric:5} {label:14} {obs:>5.0f}{unit} {lb.top.best_ask or 0:>7.2f} "
              f"{fill.cash:>9.0f} {fill.vwap:>8.3f} {profit:>8.1f}")
    print(f"\n  total takeable locked profit across open impossible buckets (<= $200 each): "
          f"${total_profit:.0f}")


if __name__ == "__main__":
    asyncio.run(main())
