"""Depth-honest negative-risk (multi-outcome) arbitrage scanner — Plan B / phase B0+B1.

WHY THIS EXISTS
---------------
A daily-temperature event is N mutually-exclusive, exhaustive buckets (N=11 in practice:
two open tails plus nine 2°F bands). Exactly one settles YES. That gives two pure arbs that
need **no weather knowledge at all** — they are order-book identities, not forecasts:

  SHORT-THE-BOARD  buy 1 NO on every bucket. Exactly N-1 of them pay $1.
                   gross/set = (N-1) - sum(NO_ask)       [equivalently: sum(YES_bid) - 1]

  BUY-THE-BOARD    buy 1 YES on every bucket. Exactly 1 pays $1.
                   gross/set = 1 - sum(YES_ask)

Both identities are correct and both were, in the first version of this scanner, MEASURED WRONG:
it charged no fee, and Polymarket charges a taker fee on exactly these markets (see
TAKER_FEE_RATE below). The gross number is a mirage roughly 5x the size of the real edge, so this
module now optimises and reports NET throughout. `short_gross_illusion` records what the old
scorer would have claimed, so the log keeps showing the size of the mistake instead of hiding it.

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
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import httpx

from backend.data.orderbook import fetch_books, LiveBook
from backend.data.weather_markets import parse_bucket_label

HDR = {"User-Agent": "Mozilla/5.0"}
GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
LOG_PATH = "logs/negrisk_arb.jsonl"
TOKEN_CACHE = "logs/negrisk_tokens_v2.json"   # v2: carries groupItemTitle for the partition check

MIN_ORDER_SHARES = 5.0      # Polymarket orderMinSize on these markets (verified: 5 SHARES, per leg)

# Polymarket charges a TAKER fee on these markets and the first version of this scanner charged
# NOTHING — which is why it reported an edge that does not exist. Verified live on all 1,639
# markets in the universe: feeType "weather_fees", feeSchedule
# {"exponent":1,"rate":0.05,"rebateRate":0.25,"takerOnly":true}; fee = shares * rate * p * (1-p).
#
# This fee is fatal to the naive version of this trade for a structural reason worth stating: it
# is a PER-LEG tax while the arb edge is not. Per set, fee = rate * (sum(q) - sum(q^2)) where
# q_i = 1 - NO_ask_i, and since the arb needs sum(q) slightly over 1, fee ~= rate * (1 - sum(q^2)).
# Shorting 9 legs to collect 0.7c of edge pays nine legs of fee (~3.4c) and loses. A 2-leg crossed
# pair collecting 10c of edge pays at most 2.5c and wins. So the ONLY fee-viable shape is a small,
# genuinely crossed subset — the opposite of what maximising GROSS profit selects for. Hence
# `best_subset_net` optimises NET and lets the leg count fall out of that.
TAKER_FEE_RATE = 0.05


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
        legs.append({"q": m.get("question", ""), "t": m.get("groupItemTitle", ""),
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


def _prefix(lad: List[Tuple[float, float]]) -> Tuple[List[float], List[float], List[float]]:
    """Cumulative (shares, cost) plus the price at each level, for O(log n) depth walks.

    The old code re-walked every ladder from the top for every candidate k, which made the sweep
    CPU-bound (measured: 1.98s of the 3.14s cycle was Python, not network). Prefix sums turn each
    cost lookup into a bisect, and the fee-aware optimiser needs strictly more lookups than the
    gross one did — without this it would push the cycle past 4s.
    """
    cs, cc, pr = [], [], []
    ts = tc = 0.0
    for price, size in lad:
        ts += size
        tc += price * size
        cs.append(ts)
        cc.append(tc)
        pr.append(price)
    return cs, cc, pr


def _cost_at(pfx, k: float) -> Optional[float]:
    """USDC to buy exactly ``k`` shares, via the prefix arrays. None if depth < k."""
    cs, cc, pr = pfx
    if not cs or k > cs[-1] + 1e-9:
        return None
    i = bisect_left(cs, k - 1e-9)
    if i >= len(cs):
        i = len(cs) - 1
    prev_s = cs[i - 1] if i else 0.0
    prev_c = cc[i - 1] if i else 0.0
    return prev_c + (k - prev_s) * pr[i]


def best_subset_net(ladders: List[Tuple[int, List[Tuple[float, float]]]],
                    rate: float = TAKER_FEE_RATE) -> Optional[dict]:
    """Best SHORT-the-board trade over any subset of legs, maximising profit NET OF TAKER FEES.

    Net profit for a subset S at k sets is additive across legs exactly as the gross version is:

        net(S, k) = sum_{i in S} (k - cost_i(k) - fee_i(k))  -  k

    so for a fixed k the optimal subset is still "include leg i iff its marginal contribution is
    positive" — only now the test is ``k - cost_i - fee_i > 0`` rather than ``cost_i < k``. That
    matters: the old gross test was mathematically unreachable (it required an average NO price
    >= $1.00, and no such level exists), so it admitted EVERY quoted leg, including 99c legs that
    contribute 0.1c of edge while consuming 99c of capital and paying fee on all of it. The net
    test throws those out, which is why it sizes down ~40x and reports far fewer, far smaller,
    genuinely positive trades.

    Candidates are every cumulative-depth boundary. Unlike the previous version this ALWAYS
    includes max_k: the old subsampler computed ``cands[int(i*len/160)]``, whose largest index is
    strictly below len-1, so it silently discarded the deepest candidate on every board with more
    than 160 boundaries (a constructed case lost $800 of $800.32).
    """
    if len(ladders) < 2:
        return None
    pfxs = [(i, _prefix(lad)) for i, lad in ladders]
    bounds = set()
    for _, (cs, _, _) in pfxs:
        for c in cs:
            bounds.add(round(c, 6))
    cands = sorted(b for b in bounds if b >= MIN_ORDER_SHARES)
    if not cands:
        return None

    best = None
    for k in cands:
        inc, cost, fee = [], 0.0, 0.0
        for i, pfx in pfxs:
            c = _cost_at(pfx, k)
            if c is None:
                continue
            p = c / k
            f = k * rate * p * (1.0 - p)
            if k - c - f <= 0:          # this leg costs more than the $1/share it can return
                continue
            inc.append(i)
            cost += c
            fee += f
        m = len(inc)
        if m < 2:
            continue
        gross = k * (m - 1) - cost
        net = gross - fee
        if net <= 0:
            continue
        if best is None or net > best["net"]:
            best = {"k": k, "cost": cost, "gross": gross, "fee": fee, "net": net,
                    "legs": m, "roc": net / cost if cost > 0 else 0.0}
    return best


def best_set_size_net(ladders: List[List[Tuple[float, float]]], payout_per_set: float,
                      rate: float = TAKER_FEE_RATE) -> Optional[dict]:
    """BUY-the-board (or all-legs short), net of taker fees. Must stay exhaustive — see caller."""
    if not ladders or any(not l for l in ladders):
        return None
    pfxs = [_prefix(l) for l in ladders]
    max_k = min(cs[-1] for cs, _, _ in pfxs)
    if max_k < MIN_ORDER_SHARES:
        return None
    cands = {round(max_k, 6)}
    for cs, _, _ in pfxs:
        for c in cs:
            if MIN_ORDER_SHARES <= c <= max_k:
                cands.add(round(c, 6))
    best = None
    for k in sorted(cands):
        costs = [_cost_at(p, k) for p in pfxs]
        if any(c is None for c in costs):
            continue
        cost = sum(costs)
        fee = sum(k * rate * (c / k) * (1.0 - c / k) for c in costs)
        gross = k * payout_per_set - cost
        net = gross - fee
        if best is None or net > best["net"]:
            best = {"k": k, "cost": cost, "gross": gross, "fee": fee, "net": net,
                    "roc": net / cost if cost > 0 else 0.0}
    if best is None or best["net"] <= 0:
        return None
    return best


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


def best_subset_short(ladders: List[Tuple[int, List[Tuple[float, float]]]],
                      max_candidates: int = 160) -> Optional[dict]:
    """Best SHORT-the-board trade over any SUBSET of legs, walked to depth.

    Buying NO on a subset S of a mutually-exclusive, exhaustive board pays m-1 in the worst
    case (the winner is inside S) and m otherwise, where m=|S| — verified by brute force over
    20k random boards. So:

        guaranteed profit = (m-1) - sum(NO_ask over S) = sum_{i in S}(1 - NO_ask_i) - 1

    which means a leg belongs in the trade exactly when its NO costs less than $1, and legs
    that are unquoted or too thin simply drop out. Requiring all N legs — as the first version
    of this scanner did — was needless strictness that discarded 125 of 149 boards, since a
    missing leg costs you only the *bonus* case, never the guarantee.

    The BUY direction has no such relaxation and is handled separately: skipping a leg there
    means the trade can pay nothing at all, so it must stay exhaustive.
    """
    if len(ladders) < 2:
        return None
    # Candidate set sizes: every point where some leg's marginal price changes. Subsampled if
    # huge, since this runs across ~150 boards every 2 seconds.
    bounds = set()
    for _, lad in ladders:
        tot = 0.0
        for _, size in lad:
            tot += size
            bounds.add(round(tot, 6))
    cands = sorted(b for b in bounds if b > 0)
    if len(cands) > max_candidates:
        step = len(cands) / max_candidates
        cands = [cands[int(i * step)] for i in range(max_candidates)]

    best = None
    for k in cands:
        inc, cost = [], 0.0
        for i, lad in ladders:
            c = cost_for_shares(lad, k)
            if c is None or c >= k:      # no depth, or its NO averages >= $1 -> excludes itself
                continue
            inc.append(i)
            cost += c
        m = len(inc)
        if m < 2:
            continue
        profit = k * (m - 1) - cost
        if profit <= 0:
            continue
        if best is None or profit > best["profit"]:
            best = {"k": k, "cost": cost, "profit": profit, "legs": m,
                    "roc": profit / cost if cost > 0 else 0.0}
    return best


def board_sanity(legs: List[dict]) -> dict:
    """Do this board's buckets actually form a partition? Computed once, at token-load time.

    The two arb directions need DIFFERENT structural properties, and the old code checked
    neither — it trusted the event-level ``negRisk`` flag and a ``len(legs) >= 3`` guard:

      SHORT a subset needs MUTUAL EXCLUSIVITY only. "At least m-1 of m NOs pay" follows from
        "at most one leg in S resolves YES". Exhaustiveness is irrelevant; if nothing on the
        board wins, every NO pays and you do better.
      BUY needs EXHAUSTIVENESS only. Skip a leg and the winner may be the one you skipped, in
        which case every YES you bought pays zero and you lose the entire stake.

    Both failures are silent and catastrophic, and both are reachable: boards are created with
    ``createdAt`` spanning 2-5 seconds, so Gamma can return a partial market list, and the token
    cache never re-validated what it captured. A 3-leg fragment with a gap at 83-84F would have
    been reported as a guaranteed $10 on $90 — and lost the $90 whenever the high was 83F.
    """
    rngs = []
    for leg in legs:
        pb = parse_bucket_label(leg.get("t") or leg.get("q") or "")
        if pb is None:
            return {"exclusive": False, "exhaustive": False, "why": "unparseable bucket"}
        rngs.append(pb)
    # Sort with open tails at the ends; a partition is then strictly adjacent, lo == prev_hi + 1.
    lows = [r for r in rngs if r[0] is None]
    highs = [r for r in rngs if r[1] is None]
    mid = sorted((r for r in rngs if r[0] is not None and r[1] is not None), key=lambda r: r[0])
    if len(lows) != 1 or len(highs) != 1:
        return {"exclusive": False, "exhaustive": False, "why": "tails != 1 each"}
    for a, b in zip(mid, mid[1:]):
        if b[0] <= a[1]:
            return {"exclusive": False, "exhaustive": False, "why": f"overlap {a}/{b}"}
    exhaustive = True
    if mid:
        if lows[0][1] != mid[0][0] - 1 or highs[0][0] != mid[-1][1] + 1:
            exhaustive = False
        for a, b in zip(mid, mid[1:]):
            if b[0] != a[1] + 1:
                exhaustive = False
    return {"exclusive": True, "exhaustive": exhaustive,
            "why": "" if exhaustive else "gap between buckets"}


def evaluate_event(legs: List[dict], books: Dict[str, LiveBook],
                   sanity: Optional[dict] = None) -> Optional[dict]:
    """Both arb directions for one board, at depth, NET OF TAKER FEES. None if nothing quoted."""
    n = len(legs)
    yes_lad, no_sub = [], []
    complete_yes, complete_no = True, True
    for i, leg in enumerate(legs):
        by, bn = books.get(leg["yes"]), books.get(leg["no"])
        if by and by.asks:
            yes_lad.append(by.asks)
        else:
            complete_yes = False
        if bn and bn.asks:
            no_sub.append((i, bn.asks))
        else:
            complete_no = False
    if not no_sub:
        return None

    sanity = sanity or {"exclusive": False, "exhaustive": False, "why": "unchecked"}
    out = {"n": n, "legs_quoted": len(no_sub), "complete": complete_no}

    # SHORT any subset — needs mutual exclusivity, which board_sanity proves. Optimised on NET,
    # so 99c filler legs that only ever added capital and fee now exclude themselves.
    if sanity["exclusive"]:
        net = best_subset_net(no_sub)
        if net:
            out["short"] = {"k": round(net["k"], 2), "cost": round(net["cost"], 2),
                            "gross": round(net["gross"], 4), "fee": round(net["fee"], 4),
                            "profit": round(net["net"], 4), "roc": round(net["roc"], 5),
                            "legs": net["legs"],
                            "executable": net["k"] >= MIN_ORDER_SHARES}
        # The gross-optimal number the old scanner reported, kept ONLY so the log records how
        # large the fee illusion was on each board. Never trade on it.
        gross_only = best_subset_short(no_sub)
        if gross_only:
            out["short_gross_illusion"] = round(gross_only["profit"], 4)
    # BUY must be exhaustive — skipping a leg means the trade can pay nothing at all.
    if complete_yes and sanity["exhaustive"]:
        out["top_yes_ask_sum"] = round(sum(l[0][0] for l in yes_lad), 4)
        buy = best_set_size_net(yes_lad, 1.0)
        if buy:
            out["buy"] = {"k": round(buy["k"], 2), "cost": round(buy["cost"], 2),
                          "gross": round(buy["gross"], 4), "fee": round(buy["fee"], 4),
                          "profit": round(buy["net"], 4), "roc": round(buy["roc"], 5),
                          "executable": buy["k"] >= MIN_ORDER_SHARES}
    if not sanity["exclusive"] or not sanity["exhaustive"]:
        out["sanity"] = sanity.get("why") or "ok"
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
    out = [cache[s] for s in slugs if s in cache]
    # Structural check per board, once. Cheap (label parsing, no network) and it is the only
    # thing standing between the identity and a silent 100%-of-stake loss on a partial board.
    for ev in out:
        if "sanity" not in ev:
            ev["sanity"] = board_sanity(ev["legs"])
    return out


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

    hits, quoted, illusion = [], 0, 0.0
    for ev in events:
        res = evaluate_event(ev["legs"], books, ev.get("sanity"))
        if not res:
            continue
        quoted += 1
        illusion += max(0.0, res.get("short_gross_illusion", 0.0))
        if res.get("buy") or res.get("short"):
            row = {"type": "board", "ts": ts, "slug": ev["slug"], **res}
            hits.append(row)
            if log_fh:
                log_fh.write(json.dumps(row) + "\n")
    if log_fh:
        # `illusion` = what the OLD gross-optimal scorer would have claimed this sweep. Logged so
        # the record shows, continuously, the size of the fee mirage rather than us having to
        # remember that the first phase of this experiment was measuring one.
        log_fh.write(json.dumps({"type": "sweep", "ts": ts, "events": len(events),
                                 "quoted": quoted, "hits": len(hits),
                                 "illusion": round(illusion, 3),
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
    print(f"  {'dir':>6} {'legs':>5} {'k':>7} {'cost$':>9} {'gross$':>7} {'fee$':>7} "
          f"{'NET$':>8} {'ROC':>7} {'exec':>5}  board")
    for h in hits[:top]:
        for d in ("buy", "short"):
            if d in h:
                b = h[d]
                legs = b.get("legs", h.get("n", 0))
                print(f"  {d:>6} {legs:>2}/{h.get('n',0):<2} {b['k']:>7.1f} {b['cost']:>9.2f} "
                      f"{b.get('gross',0):>7.3f} {b.get('fee',0):>7.3f} {b['profit']:>8.3f} "
                      f"{b['roc']*100:>6.2f}% {str(b['executable']):>5}  {h['slug'][:40]}")
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
