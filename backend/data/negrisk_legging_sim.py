"""Test A2 — the legging simulation: can we actually GET all 11 legs?

WHY THIS EXISTS
---------------
Everything measured so far (`negrisk_arb_pnl.py`, `negrisk_shadow_report.py`) asks whether the
OPPORTUNITY survives our latency. Nothing asks whether WE can fill it. A buy-the-board execution
is 11 separate taker orders, and the identity "exactly one bucket pays $1" only holds if we own
all 11. Own ten of them and the arb has silently become an unhedged directional weather bet —
the strategy that already lost $477.85 on this machine — whose worst case (the missing leg is the
winner) is a total loss of everything paid.

The asymmetry is the whole point and it is brutal. Over the buy episodes in this log the median
board offers **$0.0072 of net profit per set on $0.9675 of cost**: one set that pays nothing
wipes out ~135 sets that worked.

WHAT IT DOES
------------
Replays every buy-side episode as an actual 11-order execution:

    t0            the scanner's board row. Its prices are already ~1s stale at their own
                  timestamp — `ts` is stamped AFTER the book fetch (median fetch_s 1.02s).
    + reaction    our own decide-and-sign time (--reaction, default 0.53s, measured; see below).
    + k * L       leg k reaches the matching engine, L = per-order round trip (sequential mode).
                  In batch mode all 11 legs land together at t0 + reaction + L.

BATCH IS THE REALISTIC MODE, SEQUENTIAL IS THE PESSIMISTIC BOUND. `POST /orders` takes up to 15
signed orders, cross-market, in ONE round trip (verified at API level, test B6), so a real
implementation puts all 11 legs on the wire together. But that call is accepted, rejected and
MATCHED PER ORDER — there is no cross-order fill-or-kill — so a batch can still come back
ten-for-eleven, and that residual risk is `--batch-leg-fail`, not zero. Each individual order IS
fill-or-kill, which is why --fill-mode defaults to `fok`: a leg fills in full at our limit or not
at all, so a book that shrank below our size is a miss rather than a smaller fill.

SCOPE: buy side only. Every one of the audit's 5,269 buy rows is an 11-leg board and only the
buy identity has the cliff — miss a leg and the set can pay $0. A partial SHORT is benign: the
negRisk adapter's `convertPositions` turns k of 11 NO legs into (k-1)*$1 plus YES on the
complement in one ~$0.02 transaction, so there is no $0 state to reach. The short side is
$3.13/day of the $18.53/day replay and is not modelled here.

A leg counts as FILLED only if an observation of that board AT OR AFTER its landing time still
shows a buyable set, and it then pays that surviving (worse) price — never the arrival price.
Observations come from the log itself:

  * the ~2s sweep grid. A sweep row with NO board row for that slug is the positive fact "this
    board was not net-of-fee buyable at that instant" — the scanner writes a sweep row
    unconditionally so that absence is a measurement rather than a gap.
  * the shadow probe: a re-fetch and re-score of every hit board ~0.7s later (median delay_s
    0.705s over 29,541 probes).

THE CENTRAL LIMITATION, STATED UP FRONT
---------------------------------------
The log's time resolution is ~0.7-2s. Leg timing at L=30ms is ~0.03s. This tool therefore CANNOT
see one leg of eleven being picked off inside a snapshot interval. Two consequences, both making
the fill rates below **upper bounds**:

  * at L=30ms all 11 legs land within 0.33s and share ONE observation, so the model degenerates
    to "did the whole board survive ~0.7s" — board-level, not leg-level;
  * in BATCH mode that is true at every latency, so batch results are all-or-nothing by
    construction and understate real legging risk. `--batch-leg-fail` exists to price that blind
    spot explicitly rather than hide it; it defaults to 0 ("the log cannot see this").

Only L=500ms sequential spans enough snapshots (5.5s ~ 3 sweeps) for genuine leg-by-leg
gradation. This is a data limit, not a modelling choice, and it is why C8/D11 (real fills) can
still overturn the answer.

HOW A SET'S COST IS SPLIT ACROSS ITS 11 LEGS
--------------------------------------------
The log stores board-level aggregates only — k, total cost, total fee — never per-leg prices.
But the fee is not free information: `fee = k * 0.05 * sum p_i(1-p_i)`, so each board row hands
us TWO exact moments of its own price vector:

    S1 = sum p_i = cost/k                 S2 = sum p_i^2 = S1 - (fee/k)/0.05

`board_shape()` fits the one-parameter family that matches both exactly — a favourite at price
`a` and ten legs at `b` — which reproduces the board's logged fee to the cent instead of
approximating it (a uniform 1/11 split overstates the fee by ~60% and would make every set look
like a loss). Fitted on all 27,840 buy rows with zero failures: median favourite 66.5c, median
other leg 3.0c. That is what a nearly-decided same-day board looks like, and it matches the
audit's finding that 95% of P&L comes from same-day post-noon boards.

What the moments cannot tell us is WHICH board index the favourite sits at. `--leg-order naive`
samples it from a live cross-section of 114 open boards (it is centred: 79% of the time the
favourite is index 4-7); `expensive-first` / `cheap-first` bracket the ordering question.

Run:
  venv/bin/python -m backend.data.negrisk_legging_sim --until 2026-08-02T04:35:00+00:00
"""
from __future__ import annotations

import argparse
import bisect
import collections
import datetime
import json
import math
import random
import statistics
from typing import Dict, List, Optional

from backend.core.sizing import taker_fee_per_share

LOG = "logs/negrisk_arb.jsonl"
N_LEGS = 11
FEE_RATE = 0.05          # only used to invert the logged fee back into sum(p^2)

# --------------------------------------------------------------------------- measured constants
# Provenance for all three: reports/tier_ab/A2_legging.md.

# Where the favourite sits in board order, measured on a live cross-section of 114 open 11-bucket
# boards (2026-08-02). Board index 0 is the coldest bucket. Used only by --leg-order naive.
FAVOURITE_INDEX_PMF = [0.009, 0.018, 0.000, 0.088, 0.175, 0.246,
                       0.246, 0.123, 0.070, 0.000, 0.026]

# Absolute ask-minus-bid spread as a function of the ask, same cross-section (887 quoted legs,
# medians). The log has no bid data at all, and two things need one: valuing an unhedged residue
# at the mid rather than at the ask we paid, and the abort rule (unwinding sells into the bid).
SPREAD_BY_PRICE = [(0.01, 0.0035), (0.05, 0.0100), (0.20, 0.0200),
                   (0.60, 0.0200), (1.01, 0.0385)]

TICK = 0.001             # observed price granularity on these books (0.001, 0.002, 0.008, ...)


def spread_at(price: float) -> float:
    """Ask-minus-bid at this price level, capped so a bid can never go negative."""
    for hi, sp in SPREAD_BY_PRICE:
        if price <= hi:
            return min(sp, price)
    return min(SPREAD_BY_PRICE[-1][1], price)


def _ts(s: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(s)


def board_shape(s1: float, s2: float, n: int = N_LEGS) -> List[float]:
    """Per-leg prices matching sum(p)=s1 and sum(p^2)=s2 exactly: one favourite, n-1 equal legs.

    Solving ``n*a^2 - 2*s1*a + s1^2 - (n-1)*s2 = 0`` for the larger root. Returned in descending
    price order (favourite first); the caller places it in execution order. Falls back to a flat
    board when the moments admit no such solution (never observed: 27,840/27,840 rows fit).
    """
    disc = n * (n - 1) * s2 - (n - 1) * s1 * s1
    if disc <= 0:
        return [s1 / n] * n
    a = (s1 + math.sqrt(disc)) / n
    a = min(max(a, s1 / n), 0.999)
    b = max(0.0, (s1 - a) / (n - 1))
    return [a] + [b] * (n - 1)


# --------------------------------------------------------------------------- log loading
class Log:
    """The scanner log, indexed for the question "what did we know about slug S at/after T?"."""

    def __init__(self, path: str, until: Optional[str]):
        cutoff = _ts(until) if until else None
        self.sweeps: List[datetime.datetime] = []
        self.board: Dict[str, Dict[str, dict]] = collections.defaultdict(dict)
        self.shadow: Dict[str, List[dict]] = collections.defaultdict(list)
        for line in open(path):
            if not line.startswith("{"):
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if cutoff and _ts(d["ts"]) > cutoff:
                break
            t = d.get("type")
            if t == "sweep":
                self.sweeps.append(_ts(d["ts"]))
            elif t == "board" and "buy" in d:
                self.board[d["slug"]][d["ts"]] = d
            elif t == "shadow" and "buy" in d:
                self.shadow[d["slug"]].append(d)
        self.sweeps.sort()
        self.sweep_iso = [t.isoformat() for t in self.sweeps]
        for v in self.shadow.values():
            v.sort(key=lambda r: r["ts"])
        self.shadow_ts = {s: [_ts(r["ts"]) for r in v] for s, v in self.shadow.items()}

    def observations(self, slug: str, t0: datetime.datetime, horizon_s: float,
                     fee_ps0: float, use_shadow: bool = True) -> List[dict]:
        """Every observation of ``slug`` in (t0, t0+horizon], time-ordered.

        Shadow rows carry profit and k but NOT cost, so their per-set cost is backed out as
        ``1 - fee/set - net/set`` using the ARRIVAL row's fee-per-set. That is an assumption —
        the fee depends on the board's price shape, which barely moves in 0.7s — and it is the
        only place a shadow row's price is inferred rather than read.
        """
        end = t0 + datetime.timedelta(seconds=horizon_s)
        out = []
        rows = self.board.get(slug, {})
        i = bisect.bisect_right(self.sweeps, t0)
        while i < len(self.sweeps) and self.sweeps[i] <= end:
            r = rows.get(self.sweep_iso[i])
            if r:
                b = r["buy"]
                cost_ps = b["cost"] / b["k"]
                out.append({"t": self.sweeps[i], "alive": True, "k": b["k"], "cost_ps": cost_ps,
                            "fee_ps": b["fee"] / b["k"], "src": "sweep"})
            else:
                out.append({"t": self.sweeps[i], "alive": False, "src": "sweep"})
            i += 1
        sts = self.shadow_ts.get(slug, []) if use_shadow else []
        if sts:
            j = bisect.bisect_right(sts, t0)
            while j < len(sts) and sts[j] <= end:
                b = self.shadow[slug][j]["buy"]
                if b["now"] > 0 and b["k_now"] > 0:
                    out.append({"t": sts[j], "alive": True, "k": b["k_now"],
                                "cost_ps": max(0.0, 1.0 - fee_ps0 - b["now"] / b["k_now"]),
                                "fee_ps": fee_ps0, "src": "shadow"})
                else:
                    out.append({"t": sts[j], "alive": False, "src": "shadow"})
                j += 1
            out.sort(key=lambda o: o["t"])
        return out


# --------------------------------------------------------------------------- episodes
def buy_episodes(log: Log, gap_s: float, warmup_s: float) -> List[List[dict]]:
    """Buy-side episodes, grouped exactly as ``negrisk_arb_pnl.executions`` groups them.

    One episode = one standing mispricing = ONE execution attempt, fired at its first row.
    Note what changes versus the P&L replay: the replay DROPS episodes shorter than its
    --credit-row as "missed, no position, no cost". Here they are fired at like any other,
    because that free-miss assumption is exactly what this test exists to price.
    """
    t0 = log.sweeps[0]
    eps: List[List[dict]] = []
    for _slug, rows in log.board.items():
        rr = sorted(rows.values(), key=lambda r: r["ts"])
        cur: List[dict] = []
        last = None
        for r in rr:
            t = _ts(r["ts"])
            if last and (t - last).total_seconds() > gap_s:
                eps.append(cur)
                cur = []
            cur.append(r)
            last = t
        if cur:
            eps.append(cur)
    eps = [e for e in eps if (_ts(e[0]["ts"]) - t0).total_seconds() > warmup_s]
    eps.sort(key=lambda e: e[0]["ts"])
    return eps


# --------------------------------------------------------------------------- the simulation
def favourite_slot(order: str, rnd: random.Random) -> int:
    if order == "expensive-first":
        return 0
    if order == "cheap-first":
        return N_LEGS - 1
    r, acc = rnd.random(), 0.0
    for i, p in enumerate(FAVOURITE_INDEX_PMF):
        acc += p
        if r <= acc:
            return i
    return N_LEGS - 1


def prices_in_exec_order(cost_ps: float, fee_ps: float, slot: int) -> List[float]:
    """The 11 per-leg prices, arranged so the favourite sits at execution position ``slot``."""
    s1 = cost_ps
    s2 = max(0.0, s1 - fee_ps / FEE_RATE)
    shape = board_shape(s1, s2)
    out = shape[1:]
    out.insert(min(max(slot, 0), N_LEGS - 1), shape[0])
    return out


def simulate_execution(log: Log, ep: List[dict], reaction: float, latency: float, mode: str,
                       variant: str, order: str, gas: float, tick: float,
                       leg_fail: float, seed: int, use_shadow: bool = True,
                       fill_mode: str = "fok") -> dict:
    """One 11-leg execution attempt; returns the accounting under every outcome convention."""
    row0 = ep[0]["buy"]
    slug = ep[0]["slug"]
    t0 = _ts(ep[0]["ts"])
    k_target = row0["k"]
    cost_ps0 = row0["cost"] / k_target
    fee_ps0 = row0["fee"] / k_target

    rnd = random.Random(f"{seed}|{slug}|{ep[0]['ts']}")
    slot = favourite_slot(order, rnd)

    horizon = reaction + (N_LEGS + 1) * latency + 8.0
    obs = log.observations(slug, t0, horizon, fee_ps0, use_shadow)

    last_alive = {"cost_ps": cost_ps0, "fee_ps": fee_ps0}
    legs = []
    for j in range(N_LEGS):
        land = t0 + datetime.timedelta(
            seconds=reaction + (latency if mode == "batch" else (j + 1) * latency))
        for x in obs:                      # freshest live quote strictly before this leg lands
            if x["t"] < land and x["alive"]:
                last_alive = x
        o = next((x for x in obs if x["t"] >= land), None)
        if o is not None and o["alive"] and not (fill_mode == "fok" and o["k"] < k_target - 1e-9):
            # FOK (the real API behaviour, B6): the order fills in full at our limit or not at
            # all, so a book that shrank below our size is a MISS, not a smaller fill.
            q = k_target if fill_mode == "fok" else min(k_target, o["k"])
            price = prices_in_exec_order(o["cost_ps"], o["fee_ps"], slot)[j]
        elif variant == "lenient":
            # Absence is ambiguous: the scanner logs only net-POSITIVE boards, so "no row" can
            # equally mean the legs are still there a shade dearer. The lenient reading buys
            # anyway, one tick above the last price seen. This is the "chase" policy, and it is
            # the reason a tick matters: 11 legs * $0.001 = $0.011 per set against a median
            # per-set edge of $0.0072.
            q = k_target
            price = prices_in_exec_order(last_alive["cost_ps"], last_alive["fee_ps"], slot)[j] \
                + tick
        else:
            q, price = 0.0, 0.0
        if q > 0 and leg_fail > 0 and rnd.random() < leg_fail:
            # Independent per-order rejection. `POST /orders` takes all 11 in ONE round trip but
            # accepts/rejects and matches EACH ORDER SEPARATELY — there is no cross-order FOK — so
            # a batch can still come back ten-for-eleven. Nothing in a 2s board-level log can
            # measure how often; this knob prices it instead of hiding it.
            q, price = 0.0, 0.0
        legs.append({"q": q, "price": price})

    qs = [l["q"] for l in legs]
    complete = min(qs)
    cost = sum(l["q"] * l["price"] for l in legs)
    fee = sum(l["q"] * taker_fee_per_share(l["price"]) for l in legs)
    touched = any(q > 0 for q in qs)
    g = gas if touched else 0.0

    residue = [(l["q"] - complete, l["price"]) for l in legs]
    residue_cost = sum(q * p for q, p in residue)
    # Expected-case value of an unhedged residue: each leg at the MID, not at the ask we paid.
    # Read risk-neutrally a leg's price IS its win probability, so a residue is worth roughly
    # what it cost — the damage is the spread, the fee and the forgone arb, not the notional.
    # Using mid rather than ask keeps that side conservative; see the report on why even the mid
    # is optimistic (adverse selection: legs vanish exactly when informed flow is hitting them).
    residue_mid = sum(q * max(0.0, p - spread_at(p) / 2) for q, p in residue)
    residue_bid = sum(q * max(0.0, p - spread_at(p)) for q, p in residue)
    residue_sell_fee = sum(q * taker_fee_per_share(max(0.0, p - spread_at(p)))
                           for q, p in residue)

    # ABORT = "never carry an unhedged position". A leg that fills at a uniformly smaller size is
    # NOT a failure — the set is simply smaller and still fully hedged — so the trigger is a leg
    # that returns NOTHING: at that point no complete set can be formed, we stop sending the rest
    # and dump everything already held into the bid, paying taker fee again on the way out. If
    # every leg returned something, we keep the complete sets and unwind only the residue.
    first_zero = next((j for j in range(N_LEGS) if qs[j] <= 0), None)
    if first_zero is None:
        pnl_abort = complete + residue_bid - residue_sell_fee - cost - fee - g
    else:
        held = legs[:first_zero]
        a_cost = sum(l["q"] * l["price"] for l in held)
        a_fee = sum(l["q"] * taker_fee_per_share(l["price"]) for l in held)
        a_bid = sum(l["q"] * max(0.0, l["price"] - spread_at(l["price"])) for l in held)
        a_sfee = sum(l["q"] * taker_fee_per_share(max(0.0, l["price"] - spread_at(l["price"])))
                     for l in held)
        pnl_abort = (a_bid - a_cost - a_fee - a_sfee
                     - (gas if any(l["q"] > 0 for l in held) else 0.0))

    n_filled = sum(1 for q in qs if q > 0)
    full = all(q >= k_target - 1e-9 for q in qs)
    broken = residue_cost > 1e-9
    return {
        "ts": t0, "slug": slug, "k": k_target, "rows": len(ep),
        "arrival_cost": row0["cost"], "arrival_net": row0["profit"],
        "n_filled": n_filled, "zero": n_filled == 0, "full": full, "broken": broken,
        "complete_sets": complete, "cost": cost, "fee": fee, "gas": g,
        "residue_cost": residue_cost,
        "pnl_worst": complete - cost - fee - g,
        "pnl_expected": complete + residue_mid - cost - fee - g,
        "pnl_abort": pnl_abort,
    }


# --------------------------------------------------------------------------- reporting
def summarise(res: List[dict], hours: float) -> dict:
    per_day = lambda x: x / hours * 24.0
    n = len(res) or 1
    zero = [r for r in res if r["zero"]]
    broken = [r for r in res if r["broken"]]
    full = [r for r in res if r["full"] and not r["broken"]]
    reduced = [r for r in res if not r["zero"] and not r["broken"] and not r["full"]]
    day = collections.defaultdict(float)
    for r in res:
        day[r["ts"].date()] += r["pnl_worst"]
    return {
        "n": len(res),
        "full_pct": 100.0 * len(full) / n,
        "reduced_pct": 100.0 * len(reduced) / n,
        "broken_pct": 100.0 * len(broken) / n,
        "zero_pct": 100.0 * len(zero) / n,
        "worst_day": min(day.values()) if day else 0.0,
        "d_worst": per_day(sum(r["pnl_worst"] for r in res)),
        "d_expected": per_day(sum(r["pnl_expected"] for r in res)),
        "d_abort": per_day(sum(r["pnl_abort"] for r in res)),
        "d_hedged_only": per_day(sum(r["pnl_worst"] for r in res if not r["broken"])),
        "broken_stake": sum(r["residue_cost"] for r in broken),
        "capital_day": per_day(sum(r["cost"] for r in res)),
    }


def baseline(log: Log, eps: List[List[dict]], hours: float, gas: float) -> dict:
    """The number this test is measured against: `negrisk_arb_pnl.py`'s buy side, same window,
    same episodes, row-2 crediting, no legging risk at all."""
    tot = sum(e[1]["buy"]["profit"] - gas for e in eps if len(e) >= 2)
    n = sum(1 for e in eps if len(e) >= 2)
    return {"n": n, "d": tot / hours * 24.0}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", default=LOG)
    ap.add_argument("--until", default=None, help="ISO ts; ignore rows after it (reproducibility)")
    ap.add_argument("--episode-gap", type=float, default=300.0, help="as in negrisk_arb_pnl.py")
    ap.add_argument("--warmup", type=float, default=120.0)
    ap.add_argument("--gas", type=float, default=0.02, help="per-execution on-chain cost, $")
    ap.add_argument("--reaction", type=float, default=0.53,
                    help="seconds from the board row's ts to our FIRST order reaching the book, "
                         "EXCLUDING that order's own round trip. Default 0.53 = the scanner's "
                         "own measured turnaround (median shadow delay_s 0.705s) minus this "
                         "box's measured 0.171s RTT.")
    ap.add_argument("--latency", default="30,171,500",
                    help="comma-separated per-order round trips, ms. 30 = a colocated VPS "
                         "(~15ms measured) plus margin; 171 = this box's measured RTT to the "
                         "CLOB (~174ms); 500 = pessimistic.")
    ap.add_argument("--mode", default="both", choices=["sequential", "batch", "both"])
    ap.add_argument("--variant", default="both", choices=["conservative", "lenient", "both"])
    ap.add_argument("--leg-order", default="naive",
                    choices=["naive", "expensive-first", "cheap-first", "all"])
    ap.add_argument("--fill-mode", default="fok", choices=["fok", "partial"],
                    help="fok (default, and what the API actually does): a leg fills in full at "
                         "our limit or not at all. partial: allow a smaller fill when the book "
                         "shrank, which keeps the set hedged at a smaller size.")
    ap.add_argument("--batch-leg-fail", type=float, default=0.0,
                    help="independent per-ORDER rejection probability, applied on top of the "
                         "snapshot evidence. A batch is one round trip but 11 independently "
                         "matched orders, and no board-level 2s log can see one of them missing. "
                         "Default 0 = report only what is measured; sweep it for the real answer.")
    ap.add_argument("--ignore-shadow", action="store_true",
                    help="use only the 2s sweep grid as evidence. The shadow probe only exists "
                         "from 2026-07-27T08:06Z, so this makes the evidence uniform across the "
                         "whole window — and it is the mode in which this tool can be checked "
                         "arithmetically against negrisk_arb_pnl.py's row-2 crediting.")
    ap.add_argument("--tick", type=float, default=TICK)
    ap.add_argument("--seed", type=int, default=20260802)
    ap.add_argument("--detail", action="store_true", help="list the worst broken executions")
    ap.add_argument("--json", default=None, help="write the result rows here")
    args = ap.parse_args()

    log = Log(args.log, args.until)
    if not log.sweeps:
        print("no sweeps in log")
        return
    t0, t1 = log.sweeps[0], log.sweeps[-1]
    hours = (t1 - t0).total_seconds() / 3600.0
    eps = buy_episodes(log, args.episode_gap, args.warmup)
    base = baseline(log, eps, hours, args.gas)

    print(f"log      {args.log}")
    print(f"window   {t0:%Y-%m-%d %H:%M} -> {t1:%Y-%m-%d %H:%M} UTC  ({hours:.2f}h)")
    print(f"params   reaction={args.reaction}s gas=${args.gas}/exec tick=${args.tick} "
          f"fill={args.fill_mode} leg-reject={args.batch_leg_fail} seed={args.seed}"
          f"{' IGNORE-SHADOW' if args.ignore_shadow else ''}")
    print(f"fired    {len(eps)} buy-side executions, of which "
          f"{sum(1 for e in eps if len(e) == 1)} are single-row episodes that the P&L replay "
          f"drops as a free miss")

    ps_net = statistics.median([e[0]["buy"]["profit"] / e[0]["buy"]["k"] for e in eps])
    ps_cost = statistics.median([e[0]["buy"]["cost"] / e[0]["buy"]["k"] for e in eps])
    print(f"stakes   median per-set net ${ps_net:.5f} on ${ps_cost:.4f} of cost "
          f"-> one dead set costs {ps_cost / ps_net:.0f} live ones")
    print(f"BASELINE {base['n']} row-2-credited executions = ${base['d']:.2f}/day buy-side, "
          f"legging risk assumed away")

    lats = [float(x) / 1000.0 for x in args.latency.split(",")]
    modes = ["sequential", "batch"] if args.mode == "both" else [args.mode]
    variants = ["conservative", "lenient"] if args.variant == "both" else [args.variant]
    orders = (["naive", "expensive-first", "cheap-first"] if args.leg_order == "all"
              else [args.leg_order])

    print(f"\n{'order':>15} {'mode':>10} {'L':>7} {'variant':>13} | {'full%':>6} {'red%':>6} "
          f"{'BROKEN%':>8} {'none%':>6} | {'$/day worst':>11} {'$/day exp':>10} "
          f"{'$/day abort':>11} | {'worst day':>9}")
    print("-" * 130)
    dump, first = [], None
    for order in orders:
        for mode in modes:
            for lat in lats:
                for var in variants:
                    res = [simulate_execution(log, e, args.reaction, lat, mode, var, order,
                                              args.gas, args.tick, args.batch_leg_fail,
                                              args.seed, not args.ignore_shadow,
                                              args.fill_mode) for e in eps]
                    s = summarise(res, hours)
                    s.update({"order": order, "mode": mode, "latency_ms": lat * 1000,
                              "variant": var, "baseline_day": base["d"]})
                    dump.append(s)
                    if first is None:
                        first = (s, res)
                    print(f"{order:>15} {mode:>10} {lat*1000:>6.0f}ms {var:>13} | "
                          f"{s['full_pct']:>5.1f}% {s['reduced_pct']:>5.1f}% "
                          f"{s['broken_pct']:>7.1f}% {s['zero_pct']:>5.1f}% | "
                          f"{s['d_worst']:>11.2f} {s['d_expected']:>10.2f} "
                          f"{s['d_abort']:>11.2f} | {s['worst_day']:>9.2f}")

    print(f"\nlegend  full = all 11 legs at the size we asked for (a locked arb)")
    print(f"        red  = all 11 legs but at reduced size (still a locked arb, smaller)")
    print(f"        BROKEN = we hold an unhedged residue. This is legging risk.")
    print(f"        none = nothing filled: the board was already gone. Costs nothing.")
    print(f"        worst = residue pays $0 | exp = residue valued at the mid | "
          f"abort = unwind at the bid")

    if args.detail and first:
        s, res = first
        bad = sorted((r for r in res if r["broken"]), key=lambda r: r["pnl_worst"])[:15]
        print(f"\nworst broken executions ({s['order']} {s['mode']} {s['latency_ms']:.0f}ms "
              f"{s['variant']}):")
        for r in bad:
            print(f"  {r['ts']:%m-%d %H:%M:%S} {r['slug'][:44]:<44} legs {r['n_filled']:>2}/11 "
                  f"k={r['k']:<7.1f} unhedged ${r['residue_cost']:>7.2f} "
                  f"worst ${r['pnl_worst']:>8.2f} exp ${r['pnl_expected']:>7.2f}")

    if args.json:
        with open(args.json, "w") as f:
            json.dump(dump, f, indent=1, default=str)


if __name__ == "__main__":
    main()
