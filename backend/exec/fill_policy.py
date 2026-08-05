"""C9 — what to do when a batch of 11 fill-or-kill legs comes back partial.

Pure logic: no network, no `py-clob-client`, no filesystem. Inputs are plain dataclasses
defined here; an executor built on `backend/exec/order_client.py` translates the returned
actions into orders. The only outside dependency is the canonical fee math in
`backend/core/sizing.py` — `taker_fee_per_share`, never a flat rate.

WHY THIS MODULE EXISTS
----------------------
Execution is fixed by measurement (A2, reviewed 2026-08-02): a buy-the-board entry is 11 BUY
orders sent as ONE `POST /orders`, each order individually FOK. There is no batch-level FOK
(B5.3), so the eleven match independently and the set can come back partial. A partial BUY set
is not a smaller arb — it is an unhedged directional weather bet whose worst case (the missed
leg is the winner) pays $0 on the whole stake. That is the strategy that already lost $477.85
on this box. A partial SHORT set is benign: `NegRiskAdapter.convertPositions` turns NO on m'
buckets into (m'-1) USDC + YES on the complement in one ~$0.021 transaction, no protocol fee
(B5.1), so there is no $0 state to reach. This module encodes exactly that asymmetry.

THE BREAKEVEN BOUND, DERIVED
----------------------------
Fee (canonical, takers only, `feeType: "weather_fees"`, rate r = 0.05):

    f(p) = r * p * (1 - p)                                    [sizing.taker_fee_per_share]

All-in cost of one share bought at p, in price units:

    c(p) = p + f(p) = (1 + r) * p - r * p**2

c is strictly increasing on [0, 1] (c' = 1 + r - 2rp > 0 for r = 0.05), with c(0) = 0, c(1) = 1.

An intended BUY set S has N = 11 legs, leg i at price p_i for q_i shares. Exactly one bucket
resolves $1 (board_sanity + A3: 290/290 resolved boards paid exactly one winner), so a complete
set pays $1 per set and the number of complete sets is Q = min_i q_i. Batch FOK splits S into
filled F and killed K. Completing the set means buying each j in K at some price x_j; that
purchase is a taker fill, so it carries f(x_j) too. Completion is net-positive iff

    Q * $1  >=  SUM_{i in F} q_i * c(p_i)  +  SUM_{j in K} q_j * c(x_j)  +  g

with g = redeem gas ($0.011/board measured, B5.4; taker fills themselves cost us no gas, B4).
Define the repost budget in dollars

    B = Q * $1 - SUM_{i in F} q_i * c(p_i) - g

and split it across the killed legs in proportion to what they will actually cost,
w_j = q_j * ask_j (falling back to w_j = q_j if no leg is quoted), so that the per-leg budgets
sum to exactly B and cannot double-count slack when the reposts go out together:

    B_j = B * w_j / SUM_k w_k              R_j = B_j / q_j        (budget per share)

The maximum price for leg j is then c inverted at R_j. Solving c(x) = R:

    r*x**2 - (1 + r)*x + R = 0    =>    x* = [ (1+r) - sqrt((1+r)**2 - 4*r*R) ] / (2r)

(the smaller root; the other is > 1 for any R <= 1), degenerating to x* = R when r = 0, and
clipped to [0, 1]. Repost leg j only if ask_j <= x*_j. If ANY killed leg fails, the set cannot
be completed and every filled leg is unwound: nine legs of eleven is still a $0-risk position.

WORKED EXAMPLE (the median board of A2 s2.3: favourite 66.5c, ten legs at 3.0c, q = 20)
    c(0.665) = 0.665 + 0.05*0.665*0.335 = 0.67613875
    c(0.030) = 0.030 + 0.05*0.030*0.970 = 0.031455
    SUM over all 11        = 0.99068875   -> edge 0.93c/set (A2's median per-set edge: 0.72c)
    one 3c leg kills; SUM over the 10 filled = 0.95923375
    B = 20*1.00 - 20*0.95923375 - 0.011 = $0.80432500   ->   R = $0.04021625/share
    x* = [1.05 - sqrt(1.1025 - 4*0.05*0.04021625)] / 0.10 = 0.0383713
    Check: 20*0.95923375 + 20*c(0.0383713) + 0.011 = $20.000000 = the payout. Exactly breakeven.
    So the 3.0c leg may be re-bought up to 3.84c — 0.84c of headroom, which is the 0.93c/set
    edge minus the gas minus the extra fee the higher price carries. One tick more and the
    "arb" is a loss booked on purpose.

WHAT WE DO WHEN THE REPOST IS NOT AVAILABLE, AND THE DISCREPANCY THIS CARRIES
-----------------------------------------------------------------------------
Default: unwind every filled leg immediately, at whatever the book bears (SELL FOK at the
current best bid, paying the taker fee again on the way out). Same 20-share example, priced
with A2's measured spread curve (s3, assumption 7: 3.85c > 60c, 1c on 1-5c legs):

    unwind now            certain  -$3.47   (12.9c/set of spread + 4.5c/set of round-trip fee)
    hold the 10 legs      expected -$0.48   (martingale: E[residue] = what we paid for it)
                          worst   -$19.18   (the missed leg wins, the set pays $0)

That reproduces, on one set, exactly what A2 s4.3 measured across the whole window: unwinding
is WORSE in expectation (-$9.17/day abort vs -$4.02/day hold at 171ms sequential; the abort-vs-
hold column is -$5.15/day) and enormously better in the tail (-$9.17/day worst vs -$123.98/day
worst, worst single day -$366.63). A2 s4.3 and the Tier-A/B synthesis therefore say plainly
"abort is not a fix ... if a set breaks, hold the residue; the fix is not breaking", which is a
direct contradiction of the unwind-by-default instruction this module was built to. The measured
report wins on facts and it is quoted above; the default is still unwind, for three reasons that
are themselves in the reports:

  1. A2 s7.3 says the expected case is a CEILING on the expected case — adverse selection means
     the leg we failed to buy has a true win probability above its stale quote, so the -$0.48 is
     optimistic and unquantified, while s7.2 says every break rate in the report is a FLOOR.
  2. The tail is what ruins the account: bankroll is $250, A2 s4.4 measured a max unhedged stake
     of $97.24 on a single broken set (39% of bankroll), and the $5/day bar means one bad set
     costs weeks of edge.
  3. "The fix is not breaking" is advice about SEQUENTIAL legging, which the design already
     rejects. This module only ever runs on the residual case A2 could not measure at all
     (s5.5, per-order rejection inside a batch) — the case where not breaking was not an option.

`PolicyConfig.unwind_on_break = False` selects A2's expected-value-optimal branch instead
(Hold). It is a config, not a hidden assumption, and `worst_case_loss` reports honestly that it
breaches the unwind-now bound. Do not flip it without re-reading A2 s4.3 and s7.3.

ONE REPOST ATTEMPT PER LEG, AND WHY THERE IS NO SECOND
-----------------------------------------------------
`max_repost_attempts = 1`. A second attempt is chasing, and chasing is what A2 measured as
fatal, twice over:

  * every repost round trip is a SEQUENTIAL leg by construction. Sequential legging at this
    project's measured latency breaks 13.6% of executions, carries $127/day of unhedged weather
    exposure to harvest $15.40/day of arb, is already negative in expectation (-$4.02/day) and
    has a worst day of -$366.63 (A2 s4.1, s4.3). Batch mode's 0.0% broken rate is the whole
    reason the strategy is alive.
  * the "chase" assumption itself was tested and self-refutes: the lenient variant (absence =>
    still fillable at last price + one tick) books $34.70/day at 171ms against a $15.40/day
    un-haircut baseline. A2 s4.1: "not a credible outcome ... Do not quote the lenient column."
  * the hazard is measured: 408/593 = 68.8% of episodes are still alive 0.705s later, lambda =
    0.530/s (A2 s5.7). A second round trip is roughly a coin-flip against a leg that just proved
    it is being consumed (s4.4: what stands still is the grind; anything sizable is being eaten
    while we are still filling).

CONSERVATISM WE CHOSE ON PURPOSE (flagged for the auditor)
----------------------------------------------------------
The bound above is the ABSOLUTE breakeven of the completed set, not the marginal one. The filled
legs are sunk at decision time, so a marginal rule would allow paying up to (breakeven + the
unwind loss we avoid) — on the worked example, up to ~$3.47 more, i.e. a much higher cap — and
would knowingly lock in a small loss to dodge a bigger one. We do not do that: the recovery
side of that comparison depends on bids that A2 s3 assumption 7 measured at a random moment and
explicitly labels optimistic during a hit, and a cap that rises with the spread is a chase with
extra steps. The cost is that when a leg re-asks just above breakeven we pay the spread instead
of a cent of completion. Stated so it can be re-litigated with real books in D-tier.

The returned `max_price` is a CEILING, not a target: post at the observed ask, which is what the
gate compared against. Filling at the ceiling is a zero-profit set, not a loss.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Mapping, Sequence

from backend.core.sizing import taker_fee_per_share

Side = Literal["BUY", "SELL"]
SetKind = Literal["BUY_BOARD", "SHORT_SUBSET"]


# --------------------------------------------------------------------------- inputs


@dataclass(frozen=True)
class Leg:
    """One intended order. Both arb identities are BUY-side: YES on 11 buckets, or NO on m."""
    token_id: str
    side: Side
    price: float          # the ask we scored, and the limit we posted
    size: float           # shares
    bucket_index: int | None = None   # 0..board_size-1; required for SHORT_SUBSET (convert mask)


@dataclass(frozen=True)
class SetSpec:
    kind: SetKind
    legs: tuple[Leg, ...]
    market_id: str | None = None      # negRisk marketId; required for SHORT_SUBSET
    board_size: int = 11              # buckets on the board; a buy set must cover all of them

    @property
    def payout_per_set(self) -> float:
        """$ paid by ONE complete set of this kind, at the guaranteed (worst) resolution.

        BUY_BOARD: exhaustive, so exactly one of the 11 pays $1. SHORT_SUBSET: NO on m
        mutually-exclusive buckets pays m-1 if the winner is inside the subset, m if outside —
        the floor is m-1, which is also exactly what convertPositions hands over in cash (B5.1).
        """
        if self.kind == "BUY_BOARD":
            return 1.0
        return float(len(self.legs) - 1)


@dataclass(frozen=True)
class Book:
    """Top of book for one token at decision time. None = that side is empty (a market fact)."""
    best_ask: float | None = None
    best_bid: float | None = None


@dataclass(frozen=True)
class LegOutcome:
    """What `POST /orders` said about one leg. FOK: filled in full at `Leg.price`, or killed."""
    token_id: str
    filled: bool
    repost_attempts: int = 0          # how many times THIS leg has already been reposted


@dataclass(frozen=True)
class PolicyConfig:
    fee_rate: float | None = None     # None -> settings.WEATHER_TAKER_FEE_RATE (0.05)
    redeem_gas: float = 0.011         # $/board, measured B5.4 (median 412k gas)
    convert_gas: float = 0.021        # $/board, measured B5.4 (896k gas, 11-of-11 convert)
    max_repost_attempts: int = 1      # >1 is chasing; see the module docstring
    unwind_on_break: bool = True      # False = A2's expected-value branch (hold). Read s4.3.


DEFAULT = PolicyConfig()


# --------------------------------------------------------------------------- actions


@dataclass(frozen=True)
class RepostLeg:
    """BUY `leg.size` of `leg.token_id`, FOK, at a limit no higher than `max_price`."""
    leg: Leg
    max_price: float
    reason: str = ""
    side: Side = "BUY"


@dataclass(frozen=True)
class UnwindLeg:
    """SELL `leg.size` of `leg.token_id`, FOK, at a limit no lower than `min_price`."""
    leg: Leg
    min_price: float
    reason: str = ""
    side: Side = "SELL"


@dataclass(frozen=True)
class Hold:
    """Send nothing. Carries the legs it is talking about so a log line is self-explaining."""
    reason: str = ""
    token_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConvertPositions:
    """`NegRiskAdapter.convertPositions(market_id, index_set, amount)` — the short-side exit."""
    market_id: str
    index_set: int                    # bitmask over bucket indices, LSB = index 0
    amount: float                     # shares, uniform across the mask
    cash: float                       # (m'-1) * amount USDC returned
    reason: str = ""


Action = RepostLeg | UnwindLeg | Hold | ConvertPositions


# --------------------------------------------------------------------------- fee math


def _fee(price: float, cfg: PolicyConfig) -> float:
    return taker_fee_per_share(price, cfg.fee_rate)


def all_in_cost(price: float, cfg: PolicyConfig = DEFAULT) -> float:
    """c(p) = p + f(p): what one share really costs a taker, in price units."""
    return price + _fee(price, cfg)


def _rate(cfg: PolicyConfig) -> float:
    """The fee rate r, read back OUT of the canonical function rather than re-declared here.

    f(0.5) = r*0.25, so r = 4*f(0.5). This module therefore cannot drift from `sizing.py`, and
    if the fee shape ever stops being r*p*(1-p) the guard in `max_price_for_budget` fires
    instead of this file quoting a silently wrong breakeven.
    """
    return 4.0 * taker_fee_per_share(0.5, cfg.fee_rate)


def max_price_for_budget(budget_per_share: float, cfg: PolicyConfig = DEFAULT) -> float:
    """Largest x with c(x) <= budget_per_share — the breakeven repost price. See docstring."""
    cap = min(budget_per_share, 1.0)
    if cap <= 0.0:
        return 0.0
    r = _rate(cfg)
    if r <= 0.0:
        x = cap
    else:
        disc = (1.0 + r) ** 2 - 4.0 * r * cap
        if disc < 0.0:                                   # unreachable for r=0.05, cap<=1
            raise ValueError(f"no real breakeven price for budget {budget_per_share!r}")
        x = ((1.0 + r) - math.sqrt(disc)) / (2.0 * r)
    x = min(max(x, 0.0), 1.0)
    if abs(all_in_cost(x, cfg) - cap) > 1e-9:
        raise ValueError(
            f"fee inverse disagrees with sizing.taker_fee_per_share: c({x})="
            f"{all_in_cost(x, cfg)} != {cap}. The r*p*(1-p) shape assumed by this module's "
            f"closed form no longer holds — re-derive the bound before trading."
        )
    return x


# --------------------------------------------------------------------------- validation


@dataclass(frozen=True)
class _View:
    """Validated, indexed view of the inputs. Built once; every rule below reads only this."""
    spec: SetSpec
    cfg: PolicyConfig
    filled: tuple[Leg, ...]
    killed: tuple[Leg, ...]
    outcomes: Mapping[str, LegOutcome]
    books: Mapping[str, Book]
    sets: float = 0.0                 # Q = min size over ALL intended legs

    def book(self, leg: Leg) -> Book:
        # Books are mandatory exactly where a decision needs them (see _validate); elsewhere
        # (all filled, or nothing filled) an absent book is simply an empty one.
        return self.books.get(leg.token_id) or Book()


def _validate(spec: SetSpec, outcomes: Sequence[LegOutcome], books: Mapping[str, Book],
              cfg: PolicyConfig) -> _View:
    """Every degenerate input raises. A fill policy that silently passes is a loss generator."""
    if spec.kind not in ("BUY_BOARD", "SHORT_SUBSET"):
        raise ValueError(f"unknown set kind {spec.kind!r}")
    if spec.board_size < 2:
        raise ValueError(f"board_size must be >= 2, got {spec.board_size}")
    if not spec.legs:
        raise ValueError("empty set: nothing was intended, so there is nothing to decide")
    if cfg.max_repost_attempts < 0:
        raise ValueError("max_repost_attempts must be >= 0")

    seen: set[str] = set()
    for leg in spec.legs:
        if not leg.token_id:
            raise ValueError("leg with empty token_id")
        if leg.token_id in seen:
            raise ValueError(f"duplicate leg {leg.token_id!r} in the set")
        seen.add(leg.token_id)
        if leg.side != "BUY":
            raise ValueError(
                f"leg {leg.token_id!r} has side {leg.side!r}: both arb identities are BUY-side "
                f"(YES on 11 buckets, or NO on m). SELL appears only in an UnwindLeg action."
            )
        if not leg.size > 0:
            raise ValueError(f"leg {leg.token_id!r} has size {leg.size!r}; must be > 0")
        if not 0.0 < leg.price < 1.0:
            raise ValueError(f"leg {leg.token_id!r} has price {leg.price!r}; must be in (0,1)")

    if spec.kind == "BUY_BOARD":
        if len(spec.legs) != spec.board_size:
            raise ValueError(
                f"buy-the-board needs all {spec.board_size} buckets, got {len(spec.legs)}: a "
                f"non-exhaustive 'board' can pay $0 and is not the trade that was scored"
            )
    else:
        if len(spec.legs) < 2:
            raise ValueError("short-a-subset needs m >= 2 legs (m-1 = 0 pays nothing)")
        if len(spec.legs) > spec.board_size:
            raise ValueError(f"{len(spec.legs)} short legs on a {spec.board_size}-bucket board")
        if not spec.market_id:
            raise ValueError("SHORT_SUBSET needs market_id for convertPositions")
        idx = [leg.bucket_index for leg in spec.legs]
        if any(i is None or not 0 <= i < spec.board_size for i in idx):
            raise ValueError(f"SHORT_SUBSET needs bucket_index in [0,{spec.board_size}): {idx}")
        if len(set(idx)) != len(idx):
            raise ValueError(f"duplicate bucket_index in the short subset: {idx}")

    by_token: dict[str, LegOutcome] = {}
    for o in outcomes:
        if o.token_id in by_token:
            raise ValueError(f"duplicate outcome for {o.token_id!r}")
        if o.token_id not in seen:
            raise ValueError(f"outcome for {o.token_id!r}, which was never in the set")
        if o.repost_attempts < 0:
            raise ValueError(f"negative repost_attempts on {o.token_id!r}")
        by_token[o.token_id] = o
    missing = seen - set(by_token)
    if missing:
        raise ValueError(f"no FOK outcome reported for {sorted(missing)}")

    filled = tuple(l for l in spec.legs if by_token[l.token_id].filled)
    killed = tuple(l for l in spec.legs if not by_token[l.token_id].filled)

    # Books are needed only when there is a real decision to make: some filled AND some killed
    # on the buy side. A missing entry is an executor bug (raise); an empty side is a fact.
    need_books = spec.kind == "BUY_BOARD" and filled and killed
    if need_books:
        for leg in spec.legs:
            b = books.get(leg.token_id)
            if b is None:
                raise ValueError(f"no book for {leg.token_id!r}; cannot price a partial set")
            if b.best_ask is not None and not 0.0 < b.best_ask <= 1.0:
                raise ValueError(f"book ask {b.best_ask!r} on {leg.token_id!r} outside (0,1]")
            if b.best_bid is not None and not 0.0 <= b.best_bid <= 1.0:
                raise ValueError(f"book bid {b.best_bid!r} on {leg.token_id!r} outside [0,1]")

    return _View(spec=spec, cfg=cfg, filled=filled, killed=killed, outcomes=by_token,
                 books=dict(books), sets=min(l.size for l in spec.legs))


# --------------------------------------------------------------------------- P&L bounds


def _paid(legs: Sequence[Leg], cfg: PolicyConfig) -> float:
    """Cash out of the door for these fills, entry taker fee included."""
    return sum(l.size * all_in_cost(l.price, cfg) for l in legs)


def repost_budget(view: _View) -> float:
    """B = payout of the completed set - what the filled legs already cost - redeem gas.

    Size dispersion is charged at cost and valued at $0: only Q = min size forms complete sets,
    so shares above Q on a fat leg are residue. A2 s2.4's "worst" convention, and the scanner
    sizes uniformly, so it normally binds on nothing.
    """
    spec, cfg = view.spec, view.cfg
    return (view.sets * spec.payout_per_set) - _paid(view.filled, cfg) - cfg.redeem_gas


def breakeven_prices(view: _View) -> dict[str, float]:
    """Per-killed-leg ceiling. B is split proportional to each leg's cost at the observed ask,
    so the per-leg budgets sum to exactly B and simultaneous reposts cannot double-spend it."""
    budget = repost_budget(view)
    if budget <= 0.0 or not view.killed:
        return {l.token_id: 0.0 for l in view.killed}
    weights = {}
    for l in view.killed:
        ask = view.book(l).best_ask
        weights[l.token_id] = l.size * ask if ask else 0.0
    total = sum(weights.values())
    if total <= 0.0:                                   # nothing quoted: split by size
        weights = {l.token_id: l.size for l in view.killed}
        total = sum(weights.values())
    out = {}
    for l in view.killed:
        share = budget * weights[l.token_id] / total
        out[l.token_id] = max_price_for_budget(share / l.size, view.cfg)
    return out


def unwind_now_loss(view: _View) -> float:
    """$ lost by dumping every filled leg into the bid right now (>=0 except on a crossed book).

    Pays the spread AND a second taker fee — A2 s4.3 measures that at ~10c/set against a
    0.72c/set edge. A leg with no bid recovers $0: it cannot be sold, so the bound must not
    pretend it can. No gas: taker fills cost us nothing on-chain (B4).
    """
    cfg = view.cfg
    loss = _paid(view.filled, cfg)
    for leg in view.filled:
        bid = view.book(leg).best_bid
        if bid:
            loss -= leg.size * (bid - _fee(bid, cfg))
    return loss


def hold_worst_loss(view: _View) -> float:
    """$ lost holding a broken BUY set to resolution when the missed leg wins: the whole stake."""
    return _paid(view.filled, view.cfg)


def _short_worst_loss(view: _View) -> float:
    """Short side floor: paid, less the (m'-1) that any filled subset pays no matter who wins."""
    m = len(view.filled)
    if not m:
        return 0.0
    amount = min(l.size for l in view.filled)
    return _paid(view.filled, view.cfg) - max(m - 1, 0) * amount


def worst_case_loss(actions: Sequence[Action], view: _View) -> float:
    """Worst-case $ loss of a plan, on the books we can see. Never negative-by-optimism.

    - repost: filling at the ceiling nets exactly $0 (that is what the ceiling means), and a
      killed repost drops us back to unwinding at these same books. Hence max(0, unwind_now).
      This is the ONE modelling assumption in the bound: the fallback book is unobservable, so
      the number is "as of the quotes in hand", not a guarantee. Under unwind_on_break=False
      the killed-repost fallback is HOLDING the residue, not unwinding it, so the bound is the
      hold worst case (full stake) — the 2026-08-05 audit caught this branch understating 6.6x.
    - unwind: the certain loss.
    - hold on a broken BUY set: the full stake (A2's "worst" convention, the ruin column).
    - hold on a COMPLETE buy set: negative — a locked set pays Q*$1 whoever wins.
    """
    if any(isinstance(a, RepostLeg) for a in actions):
        if not view.cfg.unwind_on_break:
            return hold_worst_loss(view)
        return max(0.0, unwind_now_loss(view))
    if any(isinstance(a, UnwindLeg) for a in actions):
        return unwind_now_loss(view)
    if any(isinstance(a, ConvertPositions) for a in actions):
        return _short_worst_loss(view) + view.cfg.convert_gas
    if view.spec.kind == "SHORT_SUBSET":
        return _short_worst_loss(view)
    if not view.filled:
        return 0.0
    if not view.killed:
        return (hold_worst_loss(view) - view.sets * view.spec.payout_per_set
                + view.cfg.redeem_gas)
    return hold_worst_loss(view)


# --------------------------------------------------------------------------- the policy


def _unwind_plan(view: _View, why: str) -> list[Action]:
    """Dump every filled leg into its bid. Ordered biggest-notional first: each exit order is
    itself a sequential leg exposed to the book moving, so shed the most dollars first. (A2 s5.2
    measured ENTRY ordering only — it found ordering changes the damage up to 4x, not the break
    rate; the exit direction is the mirror argument, not a measurement.)"""
    if not view.cfg.unwind_on_break:
        return [Hold(reason=f"{why}; unwind_on_break=False -> holding the residue, A2 s4.3's "
                            f"expected-value branch (-$0.48 expected, -$19.18 worst on the "
                            f"docstring example). Breaches the unwind-now bound by design.",
                     token_ids=tuple(l.token_id for l in view.filled))]
    out: list[Action] = []
    no_bid: list[str] = []
    for leg in sorted(view.filled, key=lambda l: (-l.size * l.price, l.token_id)):
        bid = view.book(leg).best_bid
        if bid:
            out.append(UnwindLeg(leg=leg, min_price=bid, reason=why))
        else:
            no_bid.append(leg.token_id)
    if no_bid:
        out.append(Hold(reason=f"{why}; no bid — cannot unwind, carrying these naked",
                        token_ids=tuple(no_bid)))
    return out


def _decide_buy(view: _View) -> list[Action]:
    if not view.killed:
        return [Hold(reason="all legs filled — locked set, hold to resolution and redeem the "
                            "winner (buy-side capital is provably locked, B5.2)",
                     token_ids=tuple(l.token_id for l in view.filled))]
    if not view.filled:
        return [Hold(reason="nothing filled — a free miss (41.5% of batch attempts, A2 s4.1). "
                            "No exposure; re-entry is the scanner's decision, not a repost.",
                     token_ids=())]

    chased = [l.token_id for l in view.killed
              if view.outcomes[l.token_id].repost_attempts >= view.cfg.max_repost_attempts]
    if chased:
        return _unwind_plan(view, f"repost budget exhausted on {sorted(chased)} "
                                  f"(max {view.cfg.max_repost_attempts}/leg; chasing is A2's "
                                  f"sequential mode: 13.6% broken, -$4.02/day expected)")

    budget = repost_budget(view)
    if budget <= 0.0:
        payout = view.sets * view.spec.payout_per_set
        return _unwind_plan(view, f"no repost budget left (${budget:.4f}): the filled legs plus "
                                  f"gas already exceed the ${payout:.2f} payout")

    caps = breakeven_prices(view)
    blocked: list[str] = []
    for leg in view.killed:
        ask = view.book(leg).best_ask
        if not ask:
            blocked.append(f"{leg.token_id}:no-ask")
        elif ask > caps[leg.token_id] + 1e-12:
            blocked.append(f"{leg.token_id}:{ask:.4f}>{caps[leg.token_id]:.4f}")
    if blocked:
        return _unwind_plan(view, f"re-ask above breakeven ({', '.join(sorted(blocked))}) — "
                                  f"completing the set would book a loss on purpose")

    # Only reachable on a crossed/moved book: if dumping the partial set is itself profitable it
    # beats a completion whose worst case is $0 profit. Keeps the worst-case bound total.
    if unwind_now_loss(view) < 0.0:
        return _unwind_plan(view, "the bid side now exceeds our all-in cost — exiting pays more "
                                  "than a breakeven completion")

    # Cheap legs first: if the repost batch itself breaks, dollars held are minimised (A2 s5.2 —
    # which also notes this maximises the ODDS of holding a bag while minimising the dollars).
    return [RepostLeg(leg=leg, max_price=caps[leg.token_id],
                      reason=f"complete the set; breakeven cap {caps[leg.token_id]:.4f} incl. "
                             f"fee, ask {view.book(leg).best_ask:.4f}")
            for leg in sorted(view.killed,
                              key=lambda l: (view.book(l).best_ask or 0.0, l.token_id))]


def _decide_short(view: _View) -> list[Action]:
    """Short side: no cliff, so no repost and no unwind — just cash out what filled (B5.1).

    A partial short is a smaller short: m' NO legs pay at least m'-1 whatever happens, and
    convertPositions realises that (m'-1) as USDC immediately, plus YES on the complement, one
    tx, zero protocol fee (feeBips=0, read on-chain). The identity NO_i = SUM_{j!=i} YES_j makes
    the conversion value-neutral, so it is pure acceleration: the $14/day-per-$100 recycle
    column is real for this side only. m'=1 is the exception — (1-1)*$1 = $0 cash for $0.021 of
    gas, and one NO leg already IS YES on the complement, so hold it.
    """
    m = len(view.filled)
    if m == 0:
        return [Hold(reason="nothing filled — free miss, no exposure", token_ids=())]
    amount = min(l.size for l in view.filled)
    cash = (m - 1) * amount
    if m >= 2 and cash > view.cfg.convert_gas:
        mask = 0
        for leg in view.filled:
            mask |= 1 << int(leg.bucket_index)          # validated non-None above
        note = "" if m == len(view.spec.legs) else f" (partial {m}/{len(view.spec.legs)}, benign)"
        return [ConvertPositions(
            market_id=str(view.spec.market_id), index_set=mask, amount=amount, cash=cash,
            reason=f"convertPositions -> ${cash:.2f} cash + YES on the complement{note}; "
                   f"B5.1, ~${view.cfg.convert_gas} gas, no protocol fee")]
    return [Hold(reason=f"{m} NO leg(s) filled: convert would return ${cash:.2f} against "
                        f"${view.cfg.convert_gas} gas — hold, one NO already is YES on the "
                        f"complement (B5.1)",
                 token_ids=tuple(l.token_id for l in view.filled))]


def decide(spec: SetSpec, outcomes: Sequence[LegOutcome], books: Mapping[str, Book] | None = None,
           cfg: PolicyConfig = DEFAULT) -> list[Action]:
    """Ordered actions for one partially-filled set. Never returns an empty list — the "do
    nothing" answer is an explicit `Hold`, so a caller cannot confuse it with a missing decision.

    `books` is top-of-book at decision time for every leg; it is only consulted (and only
    required) when the buy set actually came back partial.
    """
    view = _validate(spec, outcomes, books or {}, cfg)
    return _decide_short(view) if spec.kind == "SHORT_SUBSET" else _decide_buy(view)


def explain(spec: SetSpec, outcomes: Sequence[LegOutcome], books: Mapping[str, Book] | None = None,
            cfg: PolicyConfig = DEFAULT) -> dict:
    """The same decision plus the arithmetic behind it — for logging, tests, and the auditor."""
    view = _validate(spec, outcomes, books or {}, cfg)
    actions = _decide_short(view) if spec.kind == "SHORT_SUBSET" else _decide_buy(view)
    return {
        "actions": actions,
        "sets": view.sets,
        "payout": view.sets * spec.payout_per_set,
        "filled": tuple(l.token_id for l in view.filled),
        "killed": tuple(l.token_id for l in view.killed),
        "paid": _paid(view.filled, cfg),
        "budget": repost_budget(view) if spec.kind == "BUY_BOARD" else 0.0,
        "max_prices": breakeven_prices(view) if spec.kind == "BUY_BOARD" else {},
        "unwind_now_loss": unwind_now_loss(view) if spec.kind == "BUY_BOARD" else 0.0,
        "hold_worst_loss": hold_worst_loss(view) if spec.kind == "BUY_BOARD" else 0.0,
        "worst_case_loss": worst_case_loss(actions, view),
    }
