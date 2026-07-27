"""Platform-wide TRUE-ARBITRAGE scanner for Polymarket (live, right now).

"True arb" = a position that locks profit regardless of resolution, not a directional bet.

CATEGORY A — YES+NO mispricing (pure order-book math, no outcome data, EVERY market):
  Buy 1 YES + 1 NO of the same binary market for ask(YES)+ask(NO). At resolution exactly one
  pays $1, so if ask(YES)+ask(NO) < $1 you lock $1 − sum per pair — and on Polymarket you can
  MERGE the complete set back to $1 immediately, so it needn't even wait for resolution. We walk
  BOTH ask ladders in tandem (cheapest YES with cheapest NO) so the reported size/$ is the real
  executable depth, not a top-of-book mirage.

CATEGORY B — locked/impossible outcomes (needs a verified real-world feed per market type):
  B1 weather: open daily-temp markets where the day's extreme has already passed a bucket, read
     against the EXACT ASOS settlement station (US) — provider-match verified (edge1_backtest).
  B2 resolution-known: markets whose UMA resolution is already proposed/known yet still tradable
     (Polymarket IS the settlement source, so the match is exact by construction).

Costs modelled: Polymarket runs on Polygon with relayer-subsidized (gasless) trading; realized
trading fees on standard binary markets have been 0 (a feeSchedule rate=0.05 takerOnly exists in
metadata — we report BOTH gross and net under that worst-case taker fee). Redeem/merge gas on
Polygon ≈ a few cents. orderMinSize=$5 and tick=$0.01 are flagged where they bind.

Run:  PYTHONPATH=. venv/bin/python -m backend.data.arb_scan
"""
import argparse
import asyncio
import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import httpx

from backend.data.orderbook import fetch_books

HDR = {"User-Agent": "Mozilla/5.0"}
GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets"

# keyword -> market-type, first match wins (checked against question + event title + slug)
CATEGORIES = [
    ("crypto", ["bitcoin", "btc", "ethereum", " eth ", "solana", " sol ", "crypto", "dogecoin",
                "xrp", "ripple", "price of", "$100k", "all-time high", "coinbase", "binance"]),
    ("sports", ["nba", "nfl", "mlb", "nhl", "ufc", "soccer", "premier league", "la liga", "vs.",
                " vs ", " beat ", "champions league", "world cup", "super bowl", "playoff",
                "tennis", "golf", "f1", "grand prix", "cricket", "match", "game", "wins the",
                "score", "epl", "serie a", "bundesliga", "win the"]),
    ("politics", ["election", "president", "senate", "congress", "governor", "trump", "biden",
                  "democrat", "republican", "parliament", "prime minister", "vote", "poll",
                  "nominee", "primary", "cabinet", "supreme court", "impeach", "referendum"]),
    ("weather", ["temperature", "highest temp", "lowest temp", "rain", "snow", "hurricane",
                 "weather", "degrees", "warmest", "coldest", "climate"]),
    ("econ", ["fed ", "interest rate", "inflation", "cpi", "gdp", "recession", "unemployment",
              "jobs report", "rate cut", "rate hike", "s&p", "nasdaq", "dow "]),
    ("mentions", ["say ", "tweet", "post ", "mention", "times will", "how many times"]),
    ("entertainment", ["album", "movie", "box office", "oscar", "grammy", "spotify", "netflix",
                       "rotten tomatoes", "imdb", "release", "gta", "song", "billboard"]),
]


def categorize(q: str, ev_title: str, slug: str) -> str:
    text = f"{q} {ev_title} {slug}".lower()
    for cat, kws in CATEGORIES:
        if any(k in text for k in kws):
            return cat
    return "other"


def _loads(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return None
    return v


def walk_arb(yes_asks: List[Tuple[float, float]],
             no_asks: List[Tuple[float, float]]) -> Optional[dict]:
    """Pair cheapest YES ask with cheapest NO ask while their sum < $1; the matched ladder is
    the real locked arb. Returns gross profit $, executable pairs (shares), cost $, and the
    marginal/top sums — or None if no profitable pair exists."""
    if not yes_asks or no_asks is None or not no_asks:
        return None
    ya = sorted(yes_asks)
    na = sorted(no_asks)
    i = j = 0
    yi_rem = ya[0][1]
    nj_rem = na[0][1]
    profit = cost = shares = 0.0
    top_sum = ya[0][0] + na[0][0]
    levels = 0
    while i < len(ya) and j < len(na):
        yp, np_ = ya[i][0], na[j][0]
        if yp + np_ >= 1.0:
            break
        qty = min(yi_rem, nj_rem)
        if qty <= 0:
            break
        profit += (1.0 - yp - np_) * qty
        cost += (yp + np_) * qty
        shares += qty
        levels += 1
        yi_rem -= qty
        nj_rem -= qty
        if yi_rem <= 1e-9:
            i += 1
            if i < len(ya):
                yi_rem = ya[i][1]
        if nj_rem <= 1e-9:
            j += 1
            if j < len(na):
                nj_rem = na[j][1]
    if shares <= 0:
        return None
    return {"gross": profit, "shares": shares, "cost": cost, "top_sum": top_sum, "levels": levels}


CLOB_MARKETS = "https://clob.polymarket.com/markets"


async def enumerate_markets(client) -> Tuple[List[dict], Dict[str, list]]:
    """EVERY active, accepting-orders, order-book market on the platform via CLOB cursor
    pagination (Gamma's offset /markets caps at ~2100 — this has no cap). Returns
    (two_outcome_markets, negrisk_groups) where negrisk_groups maps a neg-risk event id to
    its [(yes_tok, no_tok, question)] sub-markets for the multi-outcome arb."""
    out, groups = [], defaultdict(list)
    cursor, pages, scanned = "", 0, 0
    seen_cursor = set()
    while pages < 250:
        try:
            r = await client.get(CLOB_MARKETS, params={"next_cursor": cursor})
            if r.status_code >= 400:
                break
            d = r.json()
        except Exception:
            break
        data = d.get("data", []) or []
        scanned += len(data)
        for m in data:
            if not (m.get("active") and not m.get("closed")
                    and m.get("accepting_orders") and m.get("enable_order_book")):
                continue
            toks = m.get("tokens") or []
            if len(toks) != 2:
                continue
            # YES = the affirmative/first token; for team-vs-team both are "outcomes" (still a
            # 2-way market: buying both locks $1).
            t0, t1 = toks[0], toks[1]
            yes = t0.get("token_id") if t0.get("outcome", "").lower() != "no" else t1.get("token_id")
            no = t1.get("token_id") if yes == t0.get("token_id") else t0.get("token_id")
            rec = {"q": m.get("question", ""), "slug": m.get("market_slug", ""),
                   "ev_title": "", "yes": yes, "no": no,
                   "endDate": m.get("end_date_iso"), "negRisk": bool(m.get("neg_risk")),
                   "feesEnabled": bool(m.get("taker_base_fee") or m.get("maker_base_fee")),
                   "feeSchedule": None, "minSize": m.get("minimum_order_size"),
                   "vol24": 0, "liq": 0, "tags": m.get("tags") or [], "uma": []}
            out.append(rec)
            nrid = m.get("neg_risk_market_id")
            if m.get("neg_risk") and nrid:
                groups[nrid].append((yes, no, m.get("question", "")))
        cursor = d.get("next_cursor")
        pages += 1
        if not cursor or cursor in ("LTE=", "") or cursor in seen_cursor or not data:
            break
        seen_cursor.add(cursor)
    print(f"  (CLOB scanned {scanned} total markets across {pages} pages)")
    return out, groups


def ttr_str(end_iso) -> Tuple[str, float]:
    if not end_iso:
        return "unknown", 1e9
    try:
        end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        days = (end - datetime.now(timezone.utc)).total_seconds() / 86400.0
    except Exception:
        return "unknown", 1e9
    if days < 0:
        return "PAST end (resolving)", days
    if days < 1:
        return f"{days*24:.0f}h", days
    return f"{days:.0f}d", days


def fee_net(gross: float, cost: float, shares: float, yes_no_avg_price: float, rate: float) -> float:
    """Worst-case net after a taker fee = rate * min(p,1-p) * shares on each leg (Polymarket's
    symmetric form). For a YES+NO pair the two legs' min(p,1-p) sum to <= the cheaper side; we
    bound it as rate * shares * (avg min(p,1-p) over both legs)."""
    return gross - rate * shares * yes_no_avg_price


async def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fee-rate", type=float, default=0.0,
                    help="taker fee rate for the NET column (0 = Polymarket's realized standard; try 0.05 worst-case)")
    ap.add_argument("--top", type=int, default=60)
    args = ap.parse_args()

    t0 = time.time()
    async with httpx.AsyncClient(timeout=40.0, headers=HDR) as client:
        markets, groups = await enumerate_markets(client)
    print(f"enumerated {len(markets)} active 2-outcome order-book markets "
          f"({len(groups)} multi-outcome groups) in {time.time()-t0:.0f}s")

    # live books for every token, batched
    tokens = []
    for m in markets:
        tokens += [m["yes"], m["no"]]
    t1 = time.time()
    async with httpx.AsyncClient(timeout=25.0, headers=HDR) as client:
        books = await fetch_books(tokens, client)
    print(f"fetched live books for {len(books)}/{len(tokens)} tokens in {time.time()-t1:.0f}s\n")

    # ---- Category A ----
    opps = []
    sum_hist = defaultdict(int)   # ask-sum bucket -> count (to reveal whether fees bite)
    n_both = 0
    for m in markets:
        by = books.get(m["yes"])
        bn = books.get(m["no"])
        if not by or not bn or not by.asks or not bn.asks:
            continue
        n_both += 1
        s = by.asks[0][0] + bn.asks[0][0] if (by.asks and bn.asks) else None
        if s is not None:
            sum_hist[round(s, 2)] += 1
        res = walk_arb(by.asks, bn.asks)
        if res and res["gross"] > 1e-6:
            cat = categorize(m["q"], m["ev_title"], m["slug"])
            ttr, days = ttr_str(m["endDate"])
            avg_minp = res["cost"] / res["shares"] / 2.0   # rough avg min(p,1-p) proxy per leg
            net = fee_net(res["gross"], res["cost"], res["shares"], avg_minp, args.fee_rate)
            opps.append({**m, "cat": cat, "gross": res["gross"], "net": net,
                         "shares": res["shares"], "cost": res["cost"],
                         "top_sum": res["top_sum"], "levels": res["levels"],
                         "ttr": ttr, "days": days})

    opps.sort(key=lambda o: -o["gross"])
    print(f"{'='*100}\nCATEGORY A — YES+NO ask-sum < $1 (markets with both books quoted: {n_both})\n{'='*100}")
    print(f"  ask-sum distribution near $1 (reveals whether fees keep sums >= 1):")
    for b in sorted(sum_hist):
        if 0.90 <= b <= 1.10:
            bar = "#" * min(60, sum_hist[b])
            mark = "  <-- ARB (<1.00)" if b < 1.0 else ""
            print(f"    sum={b:.2f}  n={sum_hist[b]:>4} {bar}{mark}")

    print(f"\n  {len(opps)} Category-A opportunities found. Top {args.top} by gross $:")
    print(f"  {'gross$':>8} {'net$':>8} {'shares':>8} {'cost$':>8} {'topSum':>7} {'lvls':>4} "
          f"{'cat':>10} {'TTR':>10}  market")
    tot_gross = tot_net = 0.0
    for o in opps:
        tot_gross += o["gross"]
        tot_net += o["net"]
    for o in opps[:args.top]:
        flag = " [<minOrder]" if o["cost"] < 5 else ("" if not o["negRisk"] else " [negRisk]")
        q = (o["q"] or o["slug"])[:46]
        print(f"  {o['gross']:>8.2f} {o['net']:>8.2f} {o['shares']:>8.0f} {o['cost']:>8.0f} "
              f"{o['top_sum']:>7.3f} {o['levels']:>4} {o['cat']:>10} {o['ttr']:>10}  {q}{flag}")

    print(f"\n  CATEGORY A TOTALS: {len(opps)} opps, gross ${tot_gross:,.2f}, "
          f"net@fee={args.fee_rate} ${tot_net:,.2f}")
    bycat = defaultdict(lambda: [0, 0.0])
    for o in opps:
        bycat[o["cat"]][0] += 1
        bycat[o["cat"]][1] += o["gross"]
    print(f"  by market type:")
    for cat in sorted(bycat, key=lambda c: -bycat[c][1]):
        print(f"    {cat:14} {bycat[cat][0]:>4} opps   ${bycat[cat][1]:>9.2f} gross")

    # ---- Category A2: multi-outcome (negRisk) — sum of YES asks across exclusive outcomes ----
    print(f"\n{'='*100}\nCATEGORY A2 — multi-outcome arb (mutually-exclusive negRisk events)\n{'='*100}")
    mo = []
    for gid, subs in groups.items():
        n = len(subs)
        if n < 2:
            continue
        yes_asks, no_asks, ysz, nsz = [], [], [], []
        for yes, no, q in subs:
            by, bn = books.get(yes), books.get(no)
            yes_asks.append(by.asks[0][0] if by and by.asks else None)
            ysz.append(by.asks[0][1] if by and by.asks else 0)
            no_asks.append(bn.asks[0][0] if bn and bn.asks else None)
            nsz.append(bn.asks[0][1] if bn and bn.asks else 0)
        q0 = subs[0][2]
        if all(a is not None for a in yes_asks):
            ysum = sum(yes_asks)
            if ysum < 1.0 - 1e-6:    # buy 1 YES of each -> exactly one pays $1 (if exhaustive)
                mo.append(("YES-sum", gid, n, ysum, (1.0 - ysum) * min(ysz), q0))
        if all(a is not None for a in no_asks):
            nsum = sum(no_asks)
            if nsum < (n - 1) - 1e-6:  # buy 1 NO of each -> N-1 pay $1
                mo.append(("NO-sum", gid, n, nsum, ((n - 1) - nsum) * min(nsz), q0))
    mo.sort(key=lambda x: -x[4])
    if mo:
        print(f"  {'type':>8} {'N':>3} {'sum':>7} {'profit$/set':>11}  event")
        for typ, gid, n, s, prof, q in mo:
            print(f"  {typ:>8} {n:>3} {s:>7.3f} {prof:>11.2f}  {q[:60]}")
    else:
        print("  0 multi-outcome opportunities (YES-ask-sums >= $1 and NO-ask-sums >= N-1 "
              "across all 199 exclusive events — the negRisk AMM keeps these tight)")

    # ---- Category B (weather + resolution-known), best-effort, verified sources only ----
    await category_b(markets, books)

    print(f"\n[scan wall-clock {time.time()-t0:.0f}s]")


async def category_b(markets, books):
    print(f"\n{'='*100}\nCATEGORY B — locked/impossible outcomes (verified sources only)\n{'='*100}")

    # B2: UMA resolution already proposed/known but market still tradable
    known = [m for m in markets if m.get("uma")]
    print(f"  B2 resolution-known-but-tradable: {len(known)} active markets carry a non-empty "
          f"umaResolutionStatuses")
    shown = 0
    for m in known:
        by = books.get(m["yes"])
        bn = books.get(m["no"])
        # if a side can still be bought below $1 while its outcome is known -> locked
        info = m["uma"]
        if shown < 15:
            print(f"    {str(info)[:80]:80}  yesAsk={by.asks[0][0] if by and by.asks else None} "
                  f"noAsk={bn.asks[0][0] if bn and bn.asks else None}  {m['q'][:40]}")
            shown += 1
    if not known:
        print("    (none — markets halt trading at resolution, so this is typically empty live)")

    # B1: weather impossible-bucket (reuse the verified live probe)
    print(f"\n  B1 weather impossible-bucket probe (US ASOS-verified cities):")
    try:
        from backend.data.freemoney_scan import live_depth_probe
        from backend.config import settings
        cities = set(c.strip() for c in settings.WEATHER_CITIES.split(",") if c.strip())
        # ASOS-verified-eligible US cities (others have provider mismatch -> not trustworthy)
        await live_depth_probe(cities & {"nyc", "chicago", "denver"}, [7, 10, 13, 16, 18, 20])
    except Exception as e:
        print(f"    (weather probe skipped: {e})")


if __name__ == "__main__":
    asyncio.run(main())
