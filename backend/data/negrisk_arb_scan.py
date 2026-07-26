"""Depth-honest negative-risk (multi-outcome) arbitrage scanner — Plan B / phase B0+B1.

WHY THIS EXISTS
---------------
A daily-temperature event is N mutually-exclusive, exhaustive buckets (N=11 in practice:
two open tails plus nine 2°F bands). Exactly one settles YES. That gives two pure arbs that
need **no weather knowledge at all** — they are order-book identities, not forecasts:

  SHORT-THE-BOARD  buy 1 NO on every bucket. Exactly N-1 of them pay $1.
                   profit/set = (N-1) - sum(NO_ask)      [equivalently: sum(YES_bid) - 1]

  BUY-THE-BOARD    buy 1 YES on every bucket. Exactly 1 pays $1.
                   profit/set = 1 - sum(YES_ask)

The platform-wide scan (``arb_scan.py``) already checks both, but ONLY at top-of-book, which
is precisely the mirage this module exists to kill: a board quoting 1.03 with $8 of depth
behind it is not an opportunity, it is a rounding error. So here every leg is **walked to
depth in tandem** — we solve for the number of sets k that maximises total profit subject to
every leg actually having k shares available, and report the capital that requires.

It also must NOT read Gamma's cached ``bestBid``/``bestAsk``: those are stale to the point of
uselessness on thin weather books (observed None/0.001 on a live Denver market; the repo has
measured ~20c errors). Every price here comes from the live CLOB book.

Each sweep appends one JSONL row per event so that replaying the log answers the phase-B1
question — *how long does a window survive?* — which is what decides whether faster
infrastructure pays for itself. Sub-$5 legs are flagged, never silently counted:
``orderMinSize`` is 5 shares, so k < 5 is not executable however good the math looks.

Run:
  PYTHONPATH=. venv/bin/python -m backend.data.negrisk_arb_scan --once
  PYTHONPATH=. venv/bin/python -m backend.data.negrisk_arb_scan --loop 60
"""
import argparse
import asyncio
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import httpx

from backend.data.orderbook import fetch_books, LiveBook

HDR = {"User-Agent": "Mozilla/5.0"}
GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
LOG_PATH = "logs/negrisk_arb.jsonl"
TOKEN_CACHE = "logs/negrisk_tokens.json"

MIN_ORDER_SHARES = 5.0      # Polymarket orderMinSize on these markets
TICK = 0.01


# --------------------------------------------------------------------------- enumeration
async def enumerate_events(client: httpx.AsyncClient, tag: str, max_events: int) -> List[str]:
    """Every open event slug carrying ``tag`` (the listing endpoint is paginated)."""
    slugs: List[str] = []
    for off in range(0, max_events, 100):
        try:
            r = await client.get(GAMMA_EVENTS, params={
                "tag_slug": tag, "closed": "false", "limit": 100, "offset": off})
            batch = r.json() or []
        except Exception:
            break
        if not batch:
            break
        slugs += [e.get("slug") for e in batch if e.get("slug")]
    return [s for s in slugs if s]


async def fetch_event_tokens(client: httpx.AsyncClient, slug: str) -> Optional[dict]:
    """Resolve one event to its per-bucket (yes_token, no_token).

    The *listing* endpoint strips ``clobTokenIds``; fetching the event by slug returns it.
    Token ids are immutable, so callers cache this — it is the only slow part of a sweep.
    """
    try:
        r = await client.get(GAMMA_EVENTS, params={"slug": slug})
        evs = r.json() or []
    except Exception:
        return None
    if not evs:
        return None
    ev = evs[0]
    if not ev.get("negRisk"):
        return None
    legs = []
    for m in ev.get("markets", []):
        toks = m.get("clobTokenIds")
        if isinstance(toks, str):
            try:
                toks = json.loads(toks)
            except Exception:
                toks = None
        if not (isinstance(toks, list) and len(toks) >= 2):
            return None            # incomplete board -> not a valid exhaustive set
        outcomes = m.get("outcomes")
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except Exception:
                outcomes = None
        # Polymarket orders tokens to match `outcomes`; default is [Yes, No].
        yi = 0
        if isinstance(outcomes, list) and len(outcomes) >= 2:
            yi = 0 if str(outcomes[0]).strip().lower() in ("yes", "y") else 1
        legs.append({"q": m.get("question", ""),
                     "yes": str(toks[yi]), "no": str(toks[1 - yi])})
    if len(legs) < 3:
        return None
    return {"slug": slug, "legs": legs}


# --------------------------------------------------------------------------- depth math
def cost_for_shares(asks: List[Tuple[float, float]], k: float) -> Optional[float]:
    """USDC to buy exactly ``k`` shares by walking the ask ladder. None if depth < k."""
    need, cost = k, 0.0
    for price, size in asks:
        if need <= 1e-9:
            break
        take = min(need, size)
        cost += take * price
        need -= take
    if need > 1e-9:
        return None
    return cost


def best_set_size(ladders: List[List[Tuple[float, float]]], payout_per_set: float) -> Optional[dict]:
    """Maximise profit over the number of complete sets k.

    Profit(k) = k*payout - sum_i cost_i(k) is concave in k (each ladder's marginal cost is
    non-decreasing), so the optimum is the largest k whose *marginal* set is still profitable.
    We walk the union of level boundaries rather than a fixed grid, so the answer is exact
    rather than a sampling artefact.
    """
    if not ladders or any(not l for l in ladders):
        return None
    # candidate k values: every point where some ladder's marginal price changes
    caps = []
    for lad in ladders:
        tot, cum = 0.0, []
        for _, size in lad:
            tot += size
            cum.append(tot)
        caps.append(cum)
    max_k = min(c[-1] for c in caps)
    if max_k <= 0:
        return None
    cands = {max_k}
    for cum in caps:
        for c in cum:
            if c <= max_k:
                cands.add(c)
    best = None
    for k in sorted(cands):
        if k <= 0:
            continue
        costs = [cost_for_shares(lad, k) for lad in ladders]
        if any(c is None for c in costs):
            continue
        total_cost = sum(costs)
        profit = k * payout_per_set - total_cost
        if best is None or profit > best["profit"]:
            best = {"k": k, "cost": total_cost, "profit": profit,
                    "roc": (profit / total_cost) if total_cost > 0 else 0.0}
    if best is None or best["profit"] <= 0:
        return None
    return best


def evaluate_event(legs: List[dict], books: Dict[str, LiveBook]) -> Optional[dict]:
    """Both arb directions for one board, at depth. None if the board isn't fully quoted."""
    n = len(legs)
    yes_lad, no_lad = [], []
    for leg in legs:
        by, bn = books.get(leg["yes"]), books.get(leg["no"])
        if not by or not by.asks or not bn or not bn.asks:
            return None            # a leg we cannot buy => no executable arb
        yes_lad.append(by.asks)
        no_lad.append(bn.asks)

    top_yes_ask = sum(l[0][0] for l in yes_lad)
    top_no_ask = sum(l[0][0] for l in no_lad)

    out = {"n": n, "top_yes_ask_sum": round(top_yes_ask, 4),
           "top_no_ask_sum": round(top_no_ask, 4),
           # the headline "is there anything here at all" numbers, top-of-book:
           "top_buy_edge": round(1.0 - top_yes_ask, 4),
           "top_short_edge": round((n - 1) - top_no_ask, 4)}

    buy = best_set_size(yes_lad, 1.0)
    short = best_set_size(no_lad, float(n - 1))
    for label, res in (("buy", buy), ("short", short)):
        if res:
            out[label] = {"k": round(res["k"], 2), "cost": round(res["cost"], 2),
                          "profit": round(res["profit"], 4),
                          "roc": round(res["roc"], 5),
                          "executable": res["k"] >= MIN_ORDER_SHARES}
    return out


# --------------------------------------------------------------------------- sweep
async def load_tokens(client, tag: str, max_events: int, refresh: bool) -> List[dict]:
    cache = {}
    if os.path.exists(TOKEN_CACHE) and not refresh:
        try:
            with open(TOKEN_CACHE) as f:
                cache = json.load(f)
        except Exception:
            cache = {}
    slugs = await enumerate_events(client, tag, max_events)
    missing = [s for s in slugs if s not in cache]
    if missing:
        sem = asyncio.Semaphore(8)

        async def one(s):
            async with sem:
                return await fetch_event_tokens(client, s)
        for rec in await asyncio.gather(*[one(s) for s in missing]):
            if rec:
                cache[rec["slug"]] = rec
        os.makedirs(os.path.dirname(TOKEN_CACHE), exist_ok=True)
        with open(TOKEN_CACHE, "w") as f:
            json.dump(cache, f)
    return [cache[s] for s in slugs if s in cache]


async def sweep(client, events: List[dict], log_fh) -> dict:
    """One full platform sweep.

    Logging is deliberately asymmetric: a compact ``sweep`` row EVERY time (so the replay
    knows the scanner was alive and looking, which is what turns "no row" into the positive
    fact "no edge existed" rather than the ambiguity "we weren't watching"), and a ``board``
    row only when a board actually shows profit. At a 2s cadence, writing every quoted board
    every sweep would be ~45k rows/hour of almost entirely "nothing here".
    """
    tokens = []
    for ev in events:
        for leg in ev["legs"]:
            tokens += [leg["yes"], leg["no"]]
    t0 = time.time()
    books = await fetch_books(tokens, client)
    fetch_s = time.time() - t0
    ts = datetime.now(timezone.utc).isoformat()

    hits, quoted = [], 0
    for ev in events:
        res = evaluate_event(ev["legs"], books)
        if not res:
            continue
        quoted += 1
        if res.get("buy") or res.get("short"):
            row = {"type": "board", "ts": ts, "slug": ev["slug"], **res}
            hits.append(row)
            if log_fh:
                log_fh.write(json.dumps(row) + "\n")
    if log_fh:
        log_fh.write(json.dumps({"type": "sweep", "ts": ts, "events": len(events),
                                 "quoted": quoted, "hits": len(hits),
                                 "fetch_s": round(fetch_s, 3)}) + "\n")
        log_fh.flush()
    return {"ts": ts, "events": len(events), "quoted": quoted, "hits": hits,
            "tokens": len(tokens), "fetch_s": fetch_s}


def print_sweep(r: dict, top: int):
    hits = sorted(r["hits"], key=lambda h: -max(h.get("buy", {}).get("profit", 0),
                                                h.get("short", {}).get("profit", 0)))
    ex = [h for h in hits
          if (h.get("buy", {}).get("executable") or h.get("short", {}).get("executable"))]
    print(f"\n[{r['ts']}] boards={r['events']} fully-quoted={r['quoted']} "
          f"tokens={r['tokens']} book-fetch={r['fetch_s']:.1f}s")
    print(f"  positive-profit boards: {len(hits)}   of which executable (k>={MIN_ORDER_SHARES:.0f} "
          f"shares/leg): {len(ex)}")
    if not hits:
        print("  no board is buyable below its guaranteed payout at ANY depth.")
        return
    print(f"  {'dir':>5} {'k':>7} {'cost$':>9} {'profit$':>8} {'ROC':>7} {'exec':>5}  board")
    for h in hits[:top]:
        for d in ("buy", "short"):
            if d in h:
                b = h[d]
                print(f"  {d:>5} {b['k']:>7.1f} {b['cost']:>9.2f} {b['profit']:>8.2f} "
                      f"{b['roc']*100:>6.2f}% {str(b['executable']):>5}  {h['slug'][:52]}")
    tot = sum(max(h.get("buy", {}).get("profit", 0), h.get("short", {}).get("profit", 0))
              for h in ex)
    print(f"  EXECUTABLE PROFIT THIS SWEEP: ${tot:.2f}")


async def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tag", default="daily-temperature", help="Gamma tag_slug to scan")
    ap.add_argument("--loop", type=float, default=0,
                    help="seconds between sweeps (0 = one shot). A full 3.3k-token sweep takes "
                         "~1s, so 2s is comfortable; the interval is the resolution limit on "
                         "how short a window we can even detect.")
    ap.add_argument("--quiet", action="store_true",
                    help="only print sweeps that found something (for long fast runs)")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--max-events", type=int, default=600)
    ap.add_argument("--refresh-tokens", action="store_true")
    ap.add_argument("--refresh-every", type=float, default=1800,
                    help="seconds between re-enumerating the event universe (new boards "
                         "are created through the day; 0 disables)")
    ap.add_argument("--no-log", action="store_true")
    args = ap.parse_args()

    os.makedirs("logs", exist_ok=True)
    log_fh = None if args.no_log else open(LOG_PATH, "a")
    limits = httpx.Limits(max_connections=20)
    async with httpx.AsyncClient(timeout=30, headers=HDR, limits=limits) as client:
        events = await load_tokens(client, args.tag, args.max_events, args.refresh_tokens)
        print(f"resolved {len(events)} negRisk boards under tag '{args.tag}' "
              f"({sum(len(e['legs']) for e in events)} buckets)")
        n, errs, t_start, t_refresh = 0, 0, time.time(), time.time()
        while True:
            t0 = time.time()

            # Re-enumerate periodically. Polymarket creates the next day's boards while we
            # run, so a long session that resolved its universe once at startup would spend
            # the night scanning an ageing, shrinking set and silently miss every new board
            # — the exact opposite of the coverage an overnight run is for.
            if args.loop and args.refresh_every > 0 and (t0 - t_refresh) >= args.refresh_every:
                try:
                    fresh = await load_tokens(client, args.tag, args.max_events, False)
                    if fresh:
                        added = {e["slug"] for e in fresh} - {e["slug"] for e in events}
                        events = fresh
                        print(f"[{datetime.now(timezone.utc).isoformat()}] re-enumerated: "
                              f"{len(events)} boards (+{len(added)} new)", flush=True)
                except Exception as e:
                    print(f"  re-enumerate failed ({e}), keeping current set", flush=True)
                t_refresh = t0

            try:
                r = await sweep(client, events, log_fh)
                errs = 0
            except Exception as e:
                # An overnight run must survive a transient network blip, not die at 3am
                # and leave us with four hours of data and no idea why it stopped.
                errs += 1
                print(f"[{datetime.now(timezone.utc).isoformat()}] sweep failed ({e}) "
                      f"[{errs} consecutive]", flush=True)
                if not args.loop:
                    break
                await asyncio.sleep(min(60.0, 2.0 * errs))
                continue

            n += 1
            if not args.quiet:
                print_sweep(r, args.top)
            elif n % 900 == 0:      # ~every 30min at 2s cadence
                tot = sum(max(h.get("buy", {}).get("profit", 0),
                              h.get("short", {}).get("profit", 0)) for h in r["hits"])
                print(f"[{r['ts']}] sweep {n} ({(time.time()-t_start)/3600:.1f}h) "
                      f"boards={r['events']} quoted={r['quoted']} hits={len(r['hits'])} "
                      f"(${tot:.2f}) [fetch {r['fetch_s']:.2f}s]", flush=True)
            if not args.loop:
                break
            await asyncio.sleep(max(0.0, args.loop - (time.time() - t0)))
    if log_fh:
        log_fh.close()


if __name__ == "__main__":
    asyncio.run(main())
