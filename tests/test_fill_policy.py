"""Branch-exhaustive tests for the C9 partial-fill policy.

Pure math, no network, no clob client. The arithmetic assertions are the point: every
breakeven number below is recomputed from `sizing.taker_fee_per_share` inside the test, and
also pinned to a hardcoded decimal, so a change to the fee shape breaks this file loudly
instead of quietly re-pricing the arb.

Board used throughout is A2 s2.3's median shape: favourite 66.5c + ten legs at 3.0c, 20 shares.

    venv/bin/python -m pytest tests/test_fill_policy.py -q
"""
import random

import pytest

from backend.core.sizing import taker_fee_per_share
from backend.exec.fill_policy import (
    DEFAULT, Book, ConvertPositions, Hold, Leg, LegOutcome, PolicyConfig, RepostLeg, SetSpec,
    UnwindLeg, all_in_cost, decide, explain, max_price_for_budget,
)

FAV, TAIL, Q = 0.665, 0.030, 20.0
GAS = DEFAULT.redeem_gas          # $0.011/board, measured B5.4


def spread(p):
    """A2 s3 assumption 7: measured medians over 887 live quoted legs."""
    return 0.0035 if p <= 0.01 else 0.01 if p <= 0.05 else 0.02 if p <= 0.60 else 0.0385


BID = {FAV: round(FAV - spread(FAV), 4), TAIL: round(TAIL - spread(TAIL), 4)}


def c(p):
    """c(p) = p + f(p), recomputed from the canonical fee here rather than imported."""
    return p + taker_fee_per_share(p)


def board(prices=None, size=Q):
    prices = prices or [FAV] + [TAIL] * 10
    return SetSpec(kind="BUY_BOARD", market_id="0xboard", legs=tuple(
        Leg(token_id=f"t{i}", side="BUY", price=p, size=size, bucket_index=i)
        for i, p in enumerate(prices)))


def outcomes(spec, killed=()):
    return [LegOutcome(l.token_id, filled=l.token_id not in killed) for l in spec.legs]


def books(spec, asks=None, bids=None):
    asks, bids = asks or {}, bids or {}
    return {l.token_id: Book(best_ask=asks.get(l.token_id, l.price),
                             best_bid=bids.get(l.token_id, round(l.price - spread(l.price), 4)))
            for l in spec.legs}


# --------------------------------------------------------------------------- fee inverse


def test_fee_inverse_is_exact_and_matches_sizing():
    assert taker_fee_per_share(0.5) == pytest.approx(0.0125)          # r/4, r = 0.05
    for r in (0.0, 0.001, 0.04021625, 0.25, 0.5, 0.9, 1.0, 1.5):
        x = max_price_for_budget(r)
        assert 0.0 <= x <= 1.0
        assert all_in_cost(x) == pytest.approx(min(r, 1.0), abs=1e-12)
    assert max_price_for_budget(-1.0) == 0.0


def test_zero_fee_rate_degenerates_to_the_price_itself():
    cfg = PolicyConfig(fee_rate=0.0)
    assert max_price_for_budget(0.04, cfg) == pytest.approx(0.04)


# --------------------------------------------------------------------------- buy: no break


def test_all_eleven_filled_holds_and_sends_nothing():
    spec = board()
    e = explain(spec, outcomes(spec), {})
    acts = e["actions"]
    assert len(acts) == 1 and isinstance(acts[0], Hold)
    assert "locked set" in acts[0].reason
    assert not any(isinstance(a, (RepostLeg, UnwindLeg)) for a in acts)
    # a complete set is not exposed: it pays $1/set whoever wins, so "worst case" is the profit
    assert e["worst_case_loss"] == pytest.approx(Q * (c(FAV) + 10 * c(TAIL)) - 20.0 + GAS)
    assert e["worst_case_loss"] == pytest.approx(-0.1752, abs=5e-4)   # 0.93c/set x 20, - gas


def test_nothing_filled_is_a_free_miss_not_a_repost():
    spec = board()
    acts = decide(spec, outcomes(spec, killed={l.token_id for l in spec.legs}), {})
    assert len(acts) == 1 and isinstance(acts[0], Hold)
    assert "free miss" in acts[0].reason
    assert explain(spec, outcomes(spec, killed={l.token_id for l in spec.legs}),
                   {})["worst_case_loss"] == 0.0


# --------------------------------------------------------------------------- buy: repost


def test_one_killed_cheap_reask_reposts_at_exactly_breakeven():
    spec = board()
    e = explain(spec, outcomes(spec, killed={"t7"}), books(spec))
    assert e["killed"] == ("t7",) and len(e["filled"]) == 10
    (act,) = e["actions"]
    assert isinstance(act, RepostLeg) and act.side == "BUY" and act.leg.token_id == "t7"

    # 1. the closed form, recomputed here from the canonical fee
    paid = Q * (c(FAV) + 9 * c(TAIL))
    budget = Q * 1.0 - paid - GAS
    assert paid == pytest.approx(19.184675, abs=1e-9)
    assert budget == pytest.approx(0.804325, abs=1e-9)
    assert act.max_price == pytest.approx(max_price_for_budget(budget / Q), abs=1e-15)

    # 2. pinned to the decimal in the module docstring (3.84c against a 3.0c intent)
    assert round(act.max_price, 7) == 0.0383713
    assert round(act.max_price, 4) == 0.0384

    # 3. the set is EXACTLY breakeven at that price, fee and gas included, to the cent
    total = paid + Q * c(act.max_price) + GAS
    assert total == pytest.approx(20.0, abs=1e-9)
    assert round(total, 2) == 20.00

    # 4. the fee is load-bearing: the no-fee cap would book a certain loss
    naive_cap = budget / Q                                     # 0.04021625, ignores f(x)
    assert naive_cap > act.max_price
    loss_if_fee_ignored = Q * c(naive_cap) + paid + GAS - 20.0
    assert loss_if_fee_ignored == pytest.approx(Q * taker_fee_per_share(naive_cap), abs=1e-12)
    assert loss_if_fee_ignored == pytest.approx(0.0386, abs=5e-4)


def test_ask_exactly_at_breakeven_reposts_one_tick_above_unwinds():
    spec, killed = board(), {"t7"}
    cap = decide(spec, outcomes(spec, killed), books(spec))[0].max_price

    at = decide(spec, outcomes(spec, killed), books(spec, asks={"t7": cap}))
    assert isinstance(at[0], RepostLeg) and at[0].max_price == pytest.approx(cap)

    above = decide(spec, outcomes(spec, killed), books(spec, asks={"t7": cap + 0.001}))
    assert all(isinstance(a, UnwindLeg) for a in above)
    assert len(above) == 10


def test_multiple_killed_split_one_budget_and_stay_all_or_nothing():
    spec, killed = board(), {"t3", "t7"}
    e = explain(spec, outcomes(spec, killed), books(spec))
    acts = e["actions"]
    assert len(acts) == 2 and all(isinstance(a, RepostLeg) for a in acts)

    paid = Q * (c(FAV) + 8 * c(TAIL))
    assert e["paid"] == pytest.approx(paid, abs=1e-9)
    assert e["budget"] == pytest.approx(Q * 1.0 - paid - GAS, abs=1e-9)
    # the two caps together spend the budget EXACTLY once — no double-counted slack
    spend = sum(Q * c(a.max_price) for a in acts)
    assert paid + spend + GAS == pytest.approx(20.0, abs=1e-9)
    # equal legs, equal asks -> equal caps, each lower than the single-kill cap
    assert acts[0].max_price == pytest.approx(acts[1].max_price, abs=1e-12)
    assert acts[0].max_price < 0.0383713

    # one of the two re-asks above its cap kills the WHOLE completion: 9 of 11 is not a set
    bad = decide(spec, outcomes(spec, killed), books(spec, asks={"t7": 0.20}))
    assert all(isinstance(a, UnwindLeg) for a in bad) and len(bad) == 9


def test_reposts_are_ordered_cheapest_first():
    spec = board(prices=[0.60] + [TAIL] * 9 + [0.08])           # asks sum to 95c
    acts = decide(spec, outcomes(spec, killed={"t7", "t10"}), books(spec))
    assert [a.leg.token_id for a in acts] == ["t7", "t10"]       # 3.0c before 8.0c
    assert acts[0].max_price < acts[1].max_price                # budget split by cost, not size


# --------------------------------------------------------------------------- buy: unwind


def test_expensive_reask_unwinds_every_filled_leg_biggest_first():
    spec = board()
    e = explain(spec, outcomes(spec, killed={"t7"}), books(spec, asks={"t7": 0.05}))
    acts = e["actions"]
    assert len(acts) == 10 and all(isinstance(a, UnwindLeg) for a in acts)
    assert all(a.side == "SELL" for a in acts)
    assert {a.leg.token_id for a in acts} == {f"t{i}" for i in range(11)} - {"t7"}
    assert acts[0].leg.token_id == "t0"                          # the 66.5c favourite first
    notional = [a.leg.size * a.leg.price for a in acts]
    assert notional == sorted(notional, reverse=True)
    assert acts[0].min_price == pytest.approx(BID[FAV])           # sell at what the book bears
    assert "above breakeven" in acts[0].reason

    # the certain cost of leaving, priced with A2's measured spread curve
    recovered = Q * ((BID[FAV] - taker_fee_per_share(BID[FAV]))
                     + 9 * (BID[TAIL] - taker_fee_per_share(BID[TAIL])))
    assert e["unwind_now_loss"] == pytest.approx(Q * (c(FAV) + 9 * c(TAIL)) - recovered, 1e-9)
    assert e["unwind_now_loss"] == pytest.approx(3.4651, abs=5e-4)
    assert e["hold_worst_loss"] == pytest.approx(19.1847, abs=5e-4)
    assert e["worst_case_loss"] == pytest.approx(e["unwind_now_loss"], abs=1e-12)


def test_repost_budget_exhausted_unwinds_instead_of_chasing():
    spec = board()
    outs = [LegOutcome(l.token_id, filled=l.token_id != "t7",
                       repost_attempts=1 if l.token_id == "t7" else 0) for l in spec.legs]
    acts = decide(spec, outs, books(spec))          # ask is CHEAP; only the budget stops us
    assert all(isinstance(a, UnwindLeg) for a in acts) and len(acts) == 10
    assert "repost budget exhausted" in acts[0].reason and "chasing" in acts[0].reason


def test_zero_repost_attempts_config_never_reposts():
    spec = board()
    acts = decide(spec, outcomes(spec, killed={"t7"}), books(spec),
                  PolicyConfig(max_repost_attempts=0))
    assert all(isinstance(a, UnwindLeg) for a in acts)


def test_empty_ask_side_unwinds():
    spec = board()
    bks = books(spec) | {"t7": Book(best_ask=None, best_bid=BID[TAIL])}
    acts = decide(spec, outcomes(spec, killed={"t7"}), bks)
    assert all(isinstance(a, UnwindLeg) for a in acts)
    assert "no-ask" in acts[0].reason


def test_leg_with_no_bid_is_flagged_as_carried_naked():
    spec = board()
    bks = books(spec, asks={"t7": 0.05}) | {"t0": Book(best_ask=FAV, best_bid=None)}
    e = explain(spec, outcomes(spec, killed={"t7"}), bks)
    unwinds = [a for a in e["actions"] if isinstance(a, UnwindLeg)]
    holds = [a for a in e["actions"] if isinstance(a, Hold)]
    assert len(unwinds) == 9 and "t0" not in {a.leg.token_id for a in unwinds}
    assert len(holds) == 1 and holds[0].token_ids == ("t0",) and "no bid" in holds[0].reason
    # the unsellable favourite is valued at $0 recovery, never optimistically
    assert e["unwind_now_loss"] > Q * c(FAV)


def test_no_budget_left_unwinds():
    spec = board(prices=[0.90] + [0.02] * 10)       # 90c + 20c of tails: already over $1
    e = explain(spec, outcomes(spec, killed={"t7"}),
                {l.token_id: Book(best_ask=l.price, best_bid=l.price - 0.01) for l in spec.legs})
    assert e["budget"] < 0
    assert all(isinstance(a, UnwindLeg) for a in e["actions"])
    assert "no repost budget" in e["actions"][0].reason


def test_crossed_book_prefers_the_profitable_exit():
    spec = board()
    bks = {l.token_id: Book(best_ask=l.price, best_bid=min(0.99, l.price * 1.30))
           for l in spec.legs}
    e = explain(spec, outcomes(spec, killed={"t7"}), bks)
    assert e["unwind_now_loss"] < 0                              # leaving is worth money
    assert all(isinstance(a, UnwindLeg) for a in e["actions"])


def test_hold_branch_is_the_documented_tail_tradeoff():
    """`unwind_on_break=False` is A2 s4.3's expected-value choice. It knowingly breaches the
    unwind-now bound — the test exists so that fact is visible, not to bless it."""
    spec = board()
    cfg = PolicyConfig(unwind_on_break=False)
    e = explain(spec, outcomes(spec, killed={"t7"}), books(spec, asks={"t7": 0.05}), cfg)
    (act,) = e["actions"]
    assert isinstance(act, Hold) and len(act.token_ids) == 10
    assert e["worst_case_loss"] == pytest.approx(e["hold_worst_loss"])
    assert e["worst_case_loss"] > e["unwind_now_loss"]           # 19.18 vs 3.47
    # ... while being cheaper in expectation, which is exactly what A2 measured
    expected_hold = Q * (c(FAV) + 9 * c(TAIL)) - Q * (FAV + 9 * TAIL)
    assert expected_hold == pytest.approx(0.4847, abs=5e-4)
    assert expected_hold < e["unwind_now_loss"]


def test_repost_worst_case_under_hold_config_is_the_hold_bound():
    """Audit 2026-08-05 MUST-FIX 1: with unwind_on_break=False a killed repost falls back to
    HOLDING the residue, so a repost plan's worst case is the full-stake hold bound — not the
    unwind-now figure it previously reported (a 6.6x understatement on this board)."""
    spec = board()
    cfg = PolicyConfig(unwind_on_break=False)
    e = explain(spec, outcomes(spec, killed={"t7"}), books(spec, asks={"t7": 0.03}), cfg)
    assert any(isinstance(a, RepostLeg) for a in e["actions"])
    assert e["worst_case_loss"] == pytest.approx(e["hold_worst_loss"])
    assert e["worst_case_loss"] > e["unwind_now_loss"]


# --------------------------------------------------------------------------- short side


def short(m=5, size=Q, filled=None):
    spec = SetSpec(kind="SHORT_SUBSET", market_id="0xmkt", legs=tuple(
        Leg(token_id=f"n{i}", side="BUY", price=0.90, size=size, bucket_index=i)
        for i in range(m)))
    killed = set() if filled is None else {f"n{i}" for i in range(m)} - set(filled)
    return spec, outcomes(spec, killed)


def test_short_full_fill_converts_for_instant_cash():
    spec, outs = short()
    (act,) = decide(spec, outs, {})
    assert isinstance(act, ConvertPositions)
    assert act.market_id == "0xmkt" and act.index_set == 0b11111
    assert act.amount == Q and act.cash == pytest.approx(4 * Q)   # (m-1) * shares
    assert spec.payout_per_set == 4.0


def test_short_partial_fill_is_benign_and_still_converts():
    spec, outs = short(filled=["n0", "n2", "n4"])
    e = explain(spec, outs, {})
    (act,) = e["actions"]
    assert isinstance(act, ConvertPositions)
    assert act.index_set == 0b10101 and act.cash == pytest.approx(2 * Q)
    assert "partial 3/5" in act.reason and "benign" in act.reason
    # never a $0 state: the floor is (m'-1) whatever wins, so the worst case is bounded small
    assert e["worst_case_loss"] == pytest.approx(3 * Q * c(0.90) - 2 * Q + DEFAULT.convert_gas)
    assert not any(isinstance(a, (RepostLeg, UnwindLeg)) for a in e["actions"])


def test_short_single_leg_holds_because_convert_returns_nothing():
    spec, outs = short(filled=["n3"])
    (act,) = decide(spec, outs, {})
    assert isinstance(act, Hold) and act.token_ids == ("n3",)
    assert "one NO already is YES on the complement" in act.reason


def test_short_nothing_filled_holds():
    spec, outs = short(filled=[])
    (act,) = decide(spec, outs, {})
    assert isinstance(act, Hold) and act.token_ids == ()


def test_short_convert_not_worth_the_gas_holds():
    spec, outs = short(size=0.01, filled=["n0", "n1"])           # (2-1)*0.01 < $0.021 gas
    (act,) = decide(spec, outs, {})
    assert isinstance(act, Hold)


def test_short_needs_no_books_at_all():
    spec, outs = short(filled=["n0", "n1"])
    assert isinstance(decide(spec, outs)[0], ConvertPositions)


# --------------------------------------------------------------------------- degenerate input


def _err(spec, outs, bks=None, cfg=DEFAULT):
    with pytest.raises(ValueError) as exc:
        decide(spec, outs, bks, cfg)
    return str(exc.value)


def test_empty_set_raises():
    assert "empty set" in _err(SetSpec(kind="BUY_BOARD", legs=()), [])


def test_zero_and_negative_size_raise():
    for bad in (0.0, -5.0):
        spec = board()
        legs = list(spec.legs)
        legs[4] = Leg("t4", "BUY", TAIL, bad, 4)
        s = SetSpec(kind="BUY_BOARD", legs=tuple(legs))
        assert "size" in _err(s, outcomes(s))


def test_prices_outside_the_open_unit_interval_raise():
    for bad in (0.0, 1.0, -0.1, 1.5):
        spec = board()
        legs = list(spec.legs)
        legs[4] = Leg("t4", "BUY", bad, Q, 4)
        s = SetSpec(kind="BUY_BOARD", legs=tuple(legs))
        assert "price" in _err(s, outcomes(s))


def test_sell_side_leg_raises():
    spec = board()
    legs = list(spec.legs)
    legs[2] = Leg("t2", "SELL", TAIL, Q, 2)
    s = SetSpec(kind="BUY_BOARD", legs=tuple(legs))
    assert "BUY-side" in _err(s, outcomes(s))


def test_duplicate_leg_raises():
    spec = board()
    s = SetSpec(kind="BUY_BOARD", legs=spec.legs[:10] + (spec.legs[0],))
    assert "duplicate leg" in _err(s, outcomes(spec))


def test_non_exhaustive_buy_board_raises():
    spec = board()
    s = SetSpec(kind="BUY_BOARD", legs=spec.legs[:10])
    assert "all 11 buckets" in _err(s, outcomes(s)[:10])


def test_outcome_for_a_leg_that_was_never_posted_raises():
    spec = board()
    outs = outcomes(spec) + [LegOutcome("t99", filled=False)]
    assert "never in the set" in _err(spec, outs, books(spec))


def test_missing_and_duplicate_outcomes_raise():
    spec = board()
    assert "no FOK outcome" in _err(spec, outcomes(spec)[:-1])
    assert "duplicate outcome" in _err(spec, outcomes(spec) + [LegOutcome("t3", filled=True)])


def test_negative_repost_attempts_raise():
    spec = board()
    outs = [LegOutcome(l.token_id, True, -1) for l in spec.legs]
    assert "repost_attempts" in _err(spec, outs)


def test_partial_set_without_books_raises():
    spec = board()
    assert "no book" in _err(spec, outcomes(spec, killed={"t7"}), {})
    partial = {k: v for k, v in books(spec).items() if k != "t3"}
    assert "no book" in _err(spec, outcomes(spec, killed={"t7"}), partial)


def test_out_of_range_book_prices_raise():
    spec = board()
    assert "ask" in _err(spec, outcomes(spec, killed={"t7"}),
                         books(spec) | {"t7": Book(best_ask=0.0, best_bid=0.01)})
    assert "bid" in _err(spec, outcomes(spec, killed={"t7"}),
                         books(spec) | {"t1": Book(best_ask=TAIL, best_bid=1.5)})


def test_short_missing_market_id_or_bucket_index_raises():
    spec, outs = short()
    assert "market_id" in _err(SetSpec(kind="SHORT_SUBSET", legs=spec.legs), outs)
    naked = tuple(Leg(l.token_id, "BUY", l.price, l.size) for l in spec.legs)
    assert "bucket_index" in _err(SetSpec("SHORT_SUBSET", naked, market_id="0x"), outs)
    dupes = tuple(Leg(l.token_id, "BUY", l.price, l.size, 0) for l in spec.legs)
    assert "duplicate bucket_index" in _err(SetSpec("SHORT_SUBSET", dupes, market_id="0x"), outs)


def test_short_with_one_leg_or_too_many_raises():
    spec, outs = short(m=2)
    assert "m >= 2" in _err(SetSpec("SHORT_SUBSET", spec.legs[:1], market_id="0x"), outs[:1])
    big, bouts = short(m=6)
    assert "6 short legs" in _err(SetSpec("SHORT_SUBSET", big.legs, market_id="0x",
                                          board_size=5), bouts)


def test_unknown_kind_and_tiny_board_raise():
    spec = board()
    assert "unknown set kind" in _err(SetSpec(kind="MINT", legs=spec.legs), outcomes(spec))
    assert "board_size" in _err(SetSpec("BUY_BOARD", spec.legs, board_size=1), outcomes(spec))


# --------------------------------------------------------------------------- property


def test_no_plan_is_worse_than_unwinding_now():
    """For ANY partial outcome on ANY book, the plan's worst case is bounded by the cost of
    unwinding immediately. This is the whole safety claim: the policy may forgo profit, but it
    can never leave us with more downside than the certain exit that was available."""
    rng = random.Random(20260805)
    seen = {"repost": 0, "unwind": 0, "hold": 0}
    for _ in range(600):
        # a board the scanner would actually have emitted: 11 asks summing just under $1,
        # one dominant favourite (A2 s2.3 median shape), 5-200 shares
        total = rng.uniform(0.90, 0.9995)
        fav = total * rng.uniform(0.40, 0.92)
        rest = [rng.random() + 0.05 for _ in range(10)]
        prices = [round(fav, 4)] + [round((total - fav) * w / sum(rest), 4) for w in rest]
        prices = [min(0.99, max(0.001, p)) for p in prices]
        rng.shuffle(prices)
        spec = board(prices=prices, size=round(rng.uniform(5, 200), 2))
        n_killed = rng.choice([1, 1, 1, 2, 3, 5, 10])         # partial by construction
        killed = set(rng.sample([l.token_id for l in spec.legs], n_killed))
        bks = {}
        for l in spec.legs:
            ask = min(0.99, max(0.001, round(l.price * rng.uniform(0.75, 1.6), 4)))
            bid = max(0.0, round(ask - rng.choice([0.0035, 0.01, 0.02, 0.0385]), 4))
            bks[l.token_id] = Book(best_ask=ask, best_bid=bid or None)
        outs = [LegOutcome(l.token_id, l.token_id not in killed,
                           rng.choice([0, 0, 0, 0, 1])) for l in spec.legs]
        e = explain(spec, outs, bks)

        assert e["worst_case_loss"] <= e["unwind_now_loss"] + 1e-9
        assert e["worst_case_loss"] <= e["hold_worst_loss"] + 1e-9
        kinds = {type(a) for a in e["actions"]}
        assert not (RepostLeg in kinds and UnwindLeg in kinds)     # never half a plan
        for a in e["actions"]:
            if isinstance(a, RepostLeg):
                seen["repost"] += 1
                assert 0.0 < a.max_price <= 1.0
                assert bks[a.leg.token_id].best_ask <= a.max_price + 1e-12
            elif isinstance(a, UnwindLeg):
                seen["unwind"] += 1
                assert a.min_price == bks[a.leg.token_id].best_bid
            else:
                seen["hold"] += 1
        # a completed set is never negative: the caps spend at most the budget
        reposts = [a for a in e["actions"] if isinstance(a, RepostLeg)]
        if reposts:
            spend = sum(a.leg.size * all_in_cost(a.max_price) for a in reposts)
            assert e["paid"] + spend + GAS <= e["payout"] + 1e-9
    assert min(seen.values()) > 0, seen        # every branch was actually exercised


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
