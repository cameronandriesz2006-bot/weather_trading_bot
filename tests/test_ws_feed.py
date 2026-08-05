"""Offline tests for the WS shadow detector. NO NETWORK — every frame here is synthetic or
replayed from tests/fixtures/ws_frames_sample.jsonl (real frames captured 2026-08-05).

What is actually being defended:
  * the book state machine (snapshot / absolute-size delta / removal / list frame / unknown asset
    / malformed frame / gap -> resync),
  * that `score_board` is not a re-implementation that has drifted from the scanner,
  * that the cheap gate can never hide a hit the optimiser would have found,
  * episode new/continuing/ended semantics on the 300s rule the sweep replay uses,
  * heartbeat counter arithmetic.
"""
import json
import os

import pytest

from backend.data import negrisk_arb_scan as S
from backend.data import negrisk_ws_feed as W
from backend.data.orderbook import LiveBook, BookTop

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "ws_frames_sample.jsonl")


# --------------------------------------------------------------------------- helpers
def lvl(p, s):
    return {"price": str(p), "size": str(s)}


def feed(**over):
    """A WSFeed with no network, no log, no file handles."""
    args = W.build_parser().parse_args(["--no-log", "--quiet"])
    for k, v in over.items():
        setattr(args, k, v)
    f = W.WSFeed(args)
    return f


def board(slug="b", n=3, base=1000):
    """A synthetic board: n buckets, yes/no token ids derived from base."""
    legs = []
    for i in range(n):
        legs.append({"q": f"q{i}", "t": f"t{i}",
                     "yes": str(base + 2 * i), "no": str(base + 2 * i + 1)})
    return {"slug": slug, "legs": legs,
            "sanity": {"exclusive": True, "exhaustive": True, "why": ""}}


def wire(f, ev):
    f.by_slug[ev["slug"]] = ev
    f.shards = [[ev["slug"]]]
    f.shard_gen = [0]
    f.slug2shard[ev["slug"]] = 0
    for leg in ev["legs"]:
        for t in (leg["yes"], leg["no"]):
            f.tok2slug[t] = ev["slug"]
            f.books[t] = W.TokenBook()
    return f


def bookframe(asset, bids, asks, ts="1785900000000"):
    return {"event_type": "book", "asset_id": asset, "market": "0xm", "timestamp": ts,
            "hash": "h", "bids": [lvl(*b) for b in bids], "asks": [lvl(*a) for a in asks]}


def pcframe(entries, ts="1785900000001"):
    return {"event_type": "price_change", "market": "0xm", "timestamp": ts,
            "price_changes": [dict(asset_id=a, price=str(p), size=str(s), side=sd, hash="h",
                                   **({"best_bid": str(bb)} if bb is not None else {}),
                                   **({"best_ask": str(ba)} if ba is not None else {}))
                              for a, p, s, sd, bb, ba in entries]}


# --------------------------------------------------------------------------- book state machine
def test_snapshot_sorts_asks_cheapest_first():
    b = W.TokenBook()
    # the exchange sends asks DESCENDING; the optimiser walks them ascending
    b.snapshot(W._levels([lvl(0.9, 5), lvl(0.5, 7)]), W._levels([lvl(0.8, 3), lvl(0.4, 9)]))
    assert b.ladder() == [(0.4, 9.0), (0.8, 3.0)]
    assert b.best_ask() == 0.4
    assert b.best_bid() == 0.9
    assert b.seeded


def test_delta_size_is_absolute_not_cumulative():
    b = W.TokenBook()
    b.snapshot([], [(0.4, 9.0)])
    b.set_level("SELL", 0.4, 3.0)
    assert b.ladder() == [(0.4, 3.0)], "size must REPLACE the level, not add to it"
    b.set_level("SELL", 0.4, 11.0)
    assert b.ladder() == [(0.4, 11.0)]


def test_delta_zero_removes_level_and_moves_top():
    b = W.TokenBook()
    b.snapshot([], [(0.4, 9.0), (0.5, 2.0)])
    b.set_level("SELL", 0.4, 0.0)
    assert b.ladder() == [(0.5, 2.0)]
    assert b.best_ask() == 0.5
    b.set_level("SELL", 0.5, 0.0)
    assert b.ladder() == [] and b.best_ask() is None


def test_bid_delta_does_not_invalidate_ask_ladder():
    b = W.TokenBook()
    b.snapshot([(0.1, 1.0)], [(0.4, 9.0)])
    lad = b.ladder()
    b.set_level("BUY", 0.2, 5.0)
    assert b.ladder() is lad, "bid updates must not force an ask re-sort on the hot path"
    assert b.best_bid() == 0.2


def test_level_cap_is_bounded():
    b = W.TokenBook()
    b.snapshot([], [])
    for i in range(1, W.MAX_LEVELS + 60):
        b.set_level("SELL", i / 10000.0, 1.0)
    assert len(b.asks) == W.MAX_LEVELS
    assert b.best_ask() == 0.0001, "pruning must drop the levels furthest from the touch"


def test_apply_book_then_delta_end_to_end():
    f = wire(feed(), board())
    a = "1000"
    f.apply_frame(1.0, json.dumps(bookframe(a, [(0.1, 5)], [(0.6, 4), (0.5, 10)])))
    assert f.books[a].ladder() == [(0.5, 10.0), (0.6, 4.0)]
    assert f.dirty == {"b": (1.0, 1785900000000)}
    f.apply_frame(2.0, json.dumps(pcframe([(a, 0.5, 0, "SELL", 0.1, 0.6)])))
    assert f.books[a].ladder() == [(0.6, 4.0)]
    assert f.c["book"] == 1 and f.c["pc"] == 1 and f.c["bad"] == 0


def test_ws_recv_ts_keeps_the_earliest_arrival():
    f = wire(feed(), board())
    f.apply_frame(5.0, json.dumps(bookframe("1000", [], [(0.5, 10)])))
    f.apply_frame(9.0, json.dumps(bookframe("1002", [], [(0.5, 10)])))
    f.apply_frame(7.0, json.dumps(bookframe("1004", [], [(0.5, 10)])))
    assert f.dirty["b"][0] == 5.0, "ws_recv_ts is when the information first reached us"


def test_list_frame_is_the_initial_dump_shape():
    """The real initial dump is ONE frame carrying a JSON array of every asset's book."""
    f = wire(feed(), board())
    dump = [bookframe("1000", [(0.1, 5)], [(0.5, 10)]),
            bookframe("1002", [(0.2, 5)], [(0.4, 10)])]
    f.apply_frame(1.0, json.dumps(dump))
    assert f.books["1000"].seeded and f.books["1002"].seeded
    assert f.c["book"] == 2 and f.c["bad"] == 0


def test_unknown_asset_is_counted_not_crashed():
    f = wire(feed(), board())
    f.apply_frame(1.0, json.dumps(bookframe("999999", [], [(0.5, 1)])))
    f.apply_frame(1.0, json.dumps(pcframe([("999999", 0.5, 1, "SELL", None, None)])))
    assert f.c["unknown_asset"] == 2
    assert "999999" not in f.books and not f.dirty


def test_malformed_frames_are_survivable():
    f = wire(feed(), board())
    for raw in ("", "   ", "PONG", "PING", "{not json", "[1,2,3]", '"a string"',
                json.dumps({"event_type": "book", "asset_id": "1000", "bids": "nope",
                            "asks": [{"price": "x", "size": "y"}], "timestamp": "zz"}),
                json.dumps({"event_type": "price_change", "price_changes": [{"asset_id": "1000"}]}),
                json.dumps({"no_event_type": 1}),
                json.dumps({"event_type": "tick_size_change", "asset_id": "1000"})):
        f.apply_frame(1.0, raw)          # must not raise
    assert f.c["other"] == 1             # the tick_size_change
    assert f.c["bad"] >= 4


def test_out_of_order_and_gap_flag_a_resync():
    """No sequence numbers on this feed, so a gap shows up as our top disagreeing with the
    exchange's stated top. Two consecutive strikes force a REST resync."""
    f = wire(feed(), board())
    a = "1000"
    f.apply_frame(1.0, json.dumps(bookframe(a, [(0.1, 5)], [(0.5, 10)])))
    # exchange says best_ask is 0.30 — we never saw the update that put it there
    f.apply_frame(2.0, json.dumps(pcframe([(a, 0.7, 1, "SELL", 0.1, 0.30)])))
    assert f.c["gap_ask"] == 1 and f.c["gap_resyncs"] == 0, "one strike is a race, not a gap"
    f.apply_frame(3.0, json.dumps(pcframe([(a, 0.8, 1, "SELL", 0.1, 0.30)])))
    assert f.c["gap_resyncs"] == 1
    assert a in f.resync_q and f.books[a].pending == []


def test_agreeing_top_of_book_clears_strikes():
    f = wire(feed(), board())
    a = "1000"
    f.apply_frame(1.0, json.dumps(bookframe(a, [(0.1, 5)], [(0.5, 10)])))
    f.apply_frame(2.0, json.dumps(pcframe([(a, 0.7, 1, "SELL", 0.1, 0.30)])))
    assert f.books[a].strikes == 1
    f.apply_frame(3.0, json.dumps(pcframe([(a, 0.5, 4, "SELL", 0.1, 0.5)])))
    assert f.books[a].strikes == 0 and f.c["gap_resyncs"] == 0


def test_empty_side_sentinels_are_not_gaps():
    """MEASURED: an empty side is reported as best_bid "0" / best_ask "1", not null. Reading
    those as prices produced ~70 phantom gaps/s in the first draft of this module."""
    f = wire(feed(), board())
    a = "1000"
    f.apply_frame(1.0, json.dumps(bookframe(a, [], [])))
    f.apply_frame(2.0, json.dumps(pcframe([(a, 0.5, 0, "SELL", 0, 1)])))
    assert f.c["gap_ask"] == 0 and f.c["gap_bid"] == 0
    # ... but a sentinel against a non-empty local book IS a real disagreement
    f.apply_frame(3.0, json.dumps(bookframe(a, [(0.2, 1)], [(0.5, 1)])))
    f.apply_frame(4.0, json.dumps(pcframe([(a, 0.9, 1, "SELL", 0, 1)])))
    assert f.c["gap_ask"] == 1 and f.c["gap_bid"] == 1


def test_disagrees_semantics():
    assert W._disagrees(None, "1") is False        # both empty
    assert W._disagrees(None, "0") is False
    assert W._disagrees(0.5, "1") is True          # they say empty, we have levels
    assert W._disagrees(0.5, "0.5") is False
    assert W._disagrees(0.5, "0.51") is True
    assert W._disagrees(0.5, None) is False        # no opinion
    assert W._disagrees(0.5, "") is False
    assert W._disagrees(0.5, "junk") is False


def test_updates_during_resync_are_buffered_then_replayed():
    f = wire(feed(), board())
    a = "1000"
    f.apply_frame(1.0, json.dumps(bookframe(a, [], [(0.5, 10)])))
    f.mark_resync([a])
    f.apply_frame(2.0, json.dumps(pcframe([(a, 0.5, 0, "SELL", None, None)])))
    f.apply_frame(3.0, json.dumps(pcframe([(a, 0.4, 7, "SELL", None, None)])))
    assert f.books[a].ladder() == [(0.5, 10.0)], "a resyncing book must not take deltas live"
    assert len(f.books[a].pending) == 2
    # what resyncer() does once REST lands: seed, then replay in arrival order
    b = f.books[a]
    pend, b.pending = b.pending, None
    b.snapshot([(0.2, 1.0)], [(0.5, 99.0), (0.9, 1.0)], bid_top_only=True)
    for upd in pend:
        if upd[0] == "snap":
            b.snapshot(upd[1], upd[2])
        else:
            b.set_level(upd[1], upd[2], upd[3])
    assert b.ladder() == [(0.4, 7.0), (0.9, 1.0)]


def test_pending_overflow_rearms_a_resync():
    f = wire(feed(), board())
    a = "1000"
    f.apply_frame(1.0, json.dumps(bookframe(a, [], [(0.5, 10)])))
    f.mark_resync([a])
    f.resync_q.clear()
    for i in range(W.MAX_PENDING + 5):
        f.apply_frame(2.0, json.dumps(pcframe([(a, 0.4, i + 1, "SELL", None, None)])))
    assert len(f.books[a].pending) == W.MAX_PENDING
    assert a in f.resync_q, "a lossy replay buffer must ask for another resync"


def test_drop_oldest_arms_a_full_resync():
    f = feed(queue_max=3)
    for i in range(3):
        f.push(float(i), "x")
    assert f.q.qsize() == 3 and f.c["dropped"] == 0 and f.full_resync is False
    f.push(9.0, "y")
    assert f.c["dropped"] == 1 and f.full_resync is True
    assert f.q.qsize() == 3, "queue stays bounded"
    assert f.q.get_nowait()[0] == 1.0, "the OLDEST frame is the one dropped"


# --------------------------------------------------------------------------- scoring parity
def _real_books():
    """Ask ladders replayed from the captured real frames, as {token: [(price,size)]}."""
    out = {}
    with open(FIX) as fh:
        for line in fh:
            row = json.loads(line)
            d = json.loads(row["raw"])
            for it in (d if isinstance(d, list) else [d]):
                if it.get("event_type") == "book":
                    b = W.TokenBook()
                    b.snapshot(W._levels(it.get("bids")), W._levels(it.get("asks")))
                    out[it["asset_id"]] = b
                elif it.get("event_type") == "price_change":
                    for pc in it.get("price_changes") or []:
                        b = out.get(pc.get("asset_id"))
                        if b is not None:
                            b.set_level("BUY" if pc["side"] == "BUY" else "SELL",
                                        float(pc["price"]), float(pc["size"]))
    return out


@pytest.mark.skipif(not os.path.exists(FIX), reason="fixture not captured")
def test_fixture_has_real_frames():
    books = _real_books()
    assert len(books) >= 4, "fixture should carry at least one real initial-dump array frame"
    assert any(b.ladder() for b in books.values())


def _mkbooks(spec):
    """spec: {token: [(price,size)]} -> asks_of callable + LiveBook dict for the scanner."""
    live = {t: LiveBook(top=BookTop(best_bid=None, best_ask=(lads[0][0] if lads else None),
                                    mid=0.5), asks=list(lads))
            for t, lads in spec.items()}

    def asks_of(t):
        b = live.get(t)
        return (b.asks or None) if b else None
    return asks_of, live


def test_score_board_matches_scanner():
    """score_board must be the scanner's evaluate_event, minus short_gross_illusion only.

    Checked over synthetic boards spanning: profitable buy, profitable short, nothing, a
    missing leg, and an unquoted board.
    """
    cases = []
    # 1. buy-profitable: three legs summing well under $1
    ev = board(n=3)
    cases.append((ev, {ev["legs"][0]["yes"]: [(0.20, 50), (0.25, 50)],
                       ev["legs"][1]["yes"]: [(0.30, 50)],
                       ev["legs"][2]["yes"]: [(0.30, 50)],
                       ev["legs"][0]["no"]: [(0.79, 50)],
                       ev["legs"][1]["no"]: [(0.69, 50)],
                       ev["legs"][2]["no"]: [(0.69, 50)]}))
    # 2. short-profitable: two cheap NOs
    cases.append((ev, {ev["legs"][0]["yes"]: [(0.60, 50)],
                       ev["legs"][1]["yes"]: [(0.60, 50)],
                       ev["legs"][2]["yes"]: [(0.60, 50)],
                       ev["legs"][0]["no"]: [(0.30, 50)],
                       ev["legs"][1]["no"]: [(0.30, 50)],
                       ev["legs"][2]["no"]: [(0.99, 50)]}))
    # 3. nothing doing
    cases.append((ev, {leg[s]: [(0.90, 50)] for leg in ev["legs"] for s in ("yes", "no")}))
    # 4. a YES leg missing -> buy must not be scored
    d = {leg[s]: [(0.20, 50)] for leg in ev["legs"] for s in ("yes", "no")}
    d.pop(ev["legs"][1]["yes"])
    cases.append((ev, d))
    # 5. real captured books, mapped onto a synthetic board
    real = _real_books() if os.path.exists(FIX) else {}
    toks = [t for t, b in real.items() if b.ladder()][:6]
    if len(toks) == 6:
        ev2 = board(slug="real", n=3)
        m = {}
        for i, leg in enumerate(ev2["legs"]):
            m[leg["yes"]] = real[toks[2 * i]].ladder()
            m[leg["no"]] = real[toks[2 * i + 1]].ladder()
        cases.append((ev2, m))

    for i, (e, spec) in enumerate(cases):
        asks_of, live = _mkbooks(spec)
        mine = W.score_board(e["legs"], asks_of, e["sanity"])
        theirs = S.evaluate_event(e["legs"], live, e["sanity"])
        if theirs is not None:
            theirs = {k: v for k, v in theirs.items() if k != "short_gross_illusion"}
        assert mine == theirs, f"case {i}: {mine} != {theirs}"


def test_score_board_uses_the_scanners_optimisers():
    """Not a copy of the maths: it must call through to the scanner's functions."""
    ev = board(n=3)
    spec = {ev["legs"][0]["yes"]: [(0.20, 50)], ev["legs"][1]["yes"]: [(0.30, 50)],
            ev["legs"][2]["yes"]: [(0.30, 50)], ev["legs"][0]["no"]: [(0.30, 50)],
            ev["legs"][1]["no"]: [(0.30, 50)], ev["legs"][2]["no"]: [(0.30, 50)]}
    asks_of, _ = _mkbooks(spec)
    calls = []
    orig_a, orig_b = S.best_subset_net, S.best_set_size_net
    S.best_subset_net = lambda *a, **k: (calls.append("short"), orig_a(*a, **k))[1]
    S.best_set_size_net = lambda *a, **k: (calls.append("buy"), orig_b(*a, **k))[1]
    try:
        W.score_board(ev["legs"], asks_of, ev["sanity"])
    finally:
        S.best_subset_net, S.best_set_size_net = orig_a, orig_b
    assert set(calls) == {"short", "buy"}


def test_gate_never_hides_a_hit():
    """The gate is a necessary condition. Random boards: anything evaluate_event calls profitable
    MUST pass the gate. (A gate that rejects a real hit is a silent lost trade.)"""
    import random
    rnd = random.Random(20260805)
    misses = hits = passes = 0
    for _ in range(3000):
        n = rnd.choice([3, 4, 11])
        ev = board(n=n)
        spec = {}
        for leg in ev["legs"]:
            for s in ("yes", "no"):
                if rnd.random() < 0.06:
                    continue                       # unquoted leg
                p = round(rnd.uniform(0.01, 0.99), 3)
                lads = []
                for j in range(rnd.randint(1, 4)):
                    lads.append((min(0.999, round(p + j * 0.01, 3)),
                                 round(rnd.uniform(1, 200), 2)))
                spec[leg[s]] = lads
        asks_of, live = _mkbooks(spec)
        res = S.evaluate_event(ev["legs"], live, ev["sanity"]) or {}
        gb, gs = W.gate(ev["legs"], asks_of, ev["sanity"])
        if gb or gs:
            passes += 1
        if res.get("buy") or res.get("short"):
            hits += 1
        if res.get("buy") and not gb:
            misses += 1
        if res.get("short") and not gs:
            misses += 1
    assert misses == 0
    assert passes > 0, "the random universe produced no gate passes — test is not exercising it"
    assert hits > 20, f"only {hits} profitable boards generated — the test would pass vacuously"


def test_gate_is_actually_selective():
    ev = board(n=3)
    dear = {leg[s]: [(0.95, 50)] for leg in ev["legs"] for s in ("yes", "no")}
    asks_of, _ = _mkbooks(dear)
    assert W.gate(ev["legs"], asks_of, ev["sanity"]) == (False, False)


# --------------------------------------------------------------------------- episodes
def test_episode_new_continuing_ended():
    tr = W.EpisodeTracker(gap_s=300.0)
    p = {"profit": 1.0}
    assert [k for k, _ in tr.observe("s", "buy", p, 1000.0)] == ["new"]
    assert tr.observe("s", "buy", {"profit": 2.0}, 1001.0) == []      # continuing
    assert tr.eps[("s", "buy")]["peak"] == 2.0
    assert tr.eps[("s", "buy")]["updates"] == 2
    ends = tr.observe("s", "buy", None, 1002.0)
    assert [k for k, _ in ends] == ["end"]
    assert ends[0][1]["last_profit"] == 2.0
    assert tr.observe("s", "buy", None, 1003.0) == [], "end fires once, not every quiet rescore"


def test_reentry_inside_300s_is_the_same_episode():
    tr = W.EpisodeTracker(gap_s=300.0)
    p = {"profit": 1.0}
    (k, ep0), = tr.observe("s", "buy", p, 1000.0)
    tr.observe("s", "buy", None, 1010.0)
    assert tr.observe("s", "buy", p, 1200.0) == [], "199s later is the SAME standing mispricing"
    assert tr.eps[("s", "buy")]["id"] == ep0["id"]
    assert tr.eps[("s", "buy")]["resumes"] == 1


def test_reentry_after_300s_is_a_new_episode():
    tr = W.EpisodeTracker(gap_s=300.0)
    p = {"profit": 1.0}
    (_, ep0), = tr.observe("s", "buy", p, 1000.0)
    tr.observe("s", "buy", None, 1010.0)
    out = tr.observe("s", "buy", p, 1000.0 + 300.1)
    assert [k for k, _ in out] == ["new"]
    assert out[0][1]["id"] != ep0["id"]


def test_exactly_at_the_gap_boundary_is_still_one_episode():
    tr = W.EpisodeTracker(gap_s=300.0)
    p = {"profit": 1.0}
    tr.observe("s", "buy", p, 1000.0)
    tr.observe("s", "buy", None, 1001.0)
    assert tr.observe("s", "buy", p, 1300.0) == [], "gap rule is > gap_s, matching the pnl replay"


def test_sides_are_independent_episodes():
    tr = W.EpisodeTracker(gap_s=300.0)
    p = {"profit": 1.0}
    assert [k for k, _ in tr.observe("s", "buy", p, 1.0)] == ["new"]
    assert [k for k, _ in tr.observe("s", "short", p, 1.0)] == ["new"]
    assert tr.next_id == 3


def test_stale_episode_is_expired_and_then_forgotten():
    tr = W.EpisodeTracker(gap_s=300.0)
    tr.observe("s", "buy", {"profit": 1.0}, 1000.0)
    assert tr.expire(1200.0) == []
    out = tr.expire(1400.0)
    assert len(out) == 1 and out[0]["open"] is False
    assert tr.expire(1500.0) == [] and ("s", "buy") not in tr.eps


def test_drop_closes_open_episodes_for_a_delisted_board():
    tr = W.EpisodeTracker()
    tr.observe("s", "buy", {"profit": 1.0}, 1.0)
    tr.observe("s", "short", {"profit": 1.0}, 1.0)
    tr.observe("s", "short", None, 2.0)
    out = tr.drop("s")
    assert len(out) == 1 and out[0]["side"] == "buy"
    assert tr.eps == {}


def test_detect_and_end_rows_carry_the_sweep_fields():
    f = wire(feed(), board(n=3))
    ev = f.by_slug["b"]
    spec = {ev["legs"][0]["yes"]: [(0.20, 50)], ev["legs"][1]["yes"]: [(0.30, 50)],
            ev["legs"][2]["yes"]: [(0.30, 50)], ev["legs"][0]["no"]: [(0.79, 50)],
            ev["legs"][1]["no"]: [(0.69, 50)], ev["legs"][2]["no"]: [(0.69, 50)]}
    for t, lads in spec.items():
        f.books[t].snapshot([], lads)
    f.dirty["b"] = (100.0, 1785900000000)
    rows = []
    f.emit = rows.append
    f.rescore(100.5)
    det = [r for r in rows if r["type"] == "detect"]
    assert len(det) == 1
    r = det[0]
    for k in ("ts", "ws_recv_ts", "ex_ts", "slug", "side", "ep", "n", "legs_quoted", "complete",
              "k", "cost", "gross", "fee", "profit", "roc", "executable", "lag_ms"):
        assert k in r, k
    assert r["side"] == "buy" and r["profit"] > 0
    assert r["ex_ts"] == 1785900000000
    assert r["lag_ms"] == 500.0
    assert r["ws_recv_ts"] < r["ts"], "processed time must be later than arrival time"
    # now kill it and check the end row
    f.books[ev["legs"][0]["yes"]].snapshot([], [(0.95, 50)])
    f.dirty["b"] = (101.0, None)
    rows.clear()
    f.rescore(101.5)
    end = [r for r in rows if r["type"] == "episode_end"]
    assert len(end) == 1 and end[0]["ep"] == r["ep"] and end[0]["reason"] == "gone"
    assert end[0]["updates"] >= 1


def test_rescore_skips_a_board_that_is_mid_resync():
    """A torn book reads as 'not profitable' and would emit a false episode_end."""
    f = wire(feed(), board(n=3))
    ev = f.by_slug["b"]
    spec = {ev["legs"][0]["yes"]: [(0.20, 50)], ev["legs"][1]["yes"]: [(0.30, 50)],
            ev["legs"][2]["yes"]: [(0.30, 50)], ev["legs"][0]["no"]: [(0.79, 50)],
            ev["legs"][1]["no"]: [(0.69, 50)], ev["legs"][2]["no"]: [(0.69, 50)]}
    for t, lads in spec.items():
        f.books[t].snapshot([], lads)
    rows = []
    f.emit = rows.append
    f.dirty["b"] = (100.0, None)
    f.rescore(100.5)
    assert sum(1 for r in rows if r["type"] == "detect") == 1
    f.mark_resync([ev["legs"][0]["yes"]])
    rows.clear()
    f.dirty["b"] = (101.0, None)
    f.rescore(101.5)
    assert rows == [], "no rows at all while any of the board's books is being resynced"


def test_rescore_skips_a_board_with_no_seeded_books():
    f = wire(feed(), board(n=3))
    rows = []
    f.emit = rows.append
    f.dirty["b"] = (1.0, None)
    f.rescore(2.0)
    assert rows == [] and f.c["boards_scored"] == 0


def test_uncapped_rescore_on_a_hit():
    """The hot path scores capped; a hit must be re-scored at full depth before it is logged."""
    f = wire(feed(), board(n=3))
    f.a.ask_cap = 1
    ev = f.by_slug["b"]
    # leg 0 has 5 shares at 0.20 then a deep 0.21 level: capped at 1 level, k can only reach 5
    spec = {ev["legs"][0]["yes"]: [(0.20, 5), (0.21, 500)],
            ev["legs"][1]["yes"]: [(0.30, 505)], ev["legs"][2]["yes"]: [(0.30, 505)],
            ev["legs"][0]["no"]: [(0.79, 505)], ev["legs"][1]["no"]: [(0.69, 505)],
            ev["legs"][2]["no"]: [(0.69, 505)]}
    for t, lads in spec.items():
        f.books[t].snapshot([], lads)
    rows = []
    f.emit = rows.append
    f.dirty["b"] = (1.0, None)
    f.rescore(2.0)
    det = [r for r in rows if r["type"] == "detect"][0]
    assert det["k"] > 5.0, "logged k must come from the UNCAPPED ladder, not the hot-path cap"


# --------------------------------------------------------------------------- heartbeat
def test_heartbeat_counters_are_windowed_deltas():
    f = feed(hb_every=10.0)
    f.c["frames"] = 100
    f.c["events"] = 250
    f.c["pc"] = 240
    f.c["detects"] = 2
    f.c["rescores"] = 20
    f.c["rescore_ms"] = 40.0
    f.c["qmax"] = 7
    r1 = f.hb_row(f.t_start + 10.0)
    assert r1["frames_s"] == 10.0 and r1["events_s"] == 25.0 and r1["pc_s"] == 24.0
    assert r1["detects"] == 2
    assert r1["rescores_s"] == 2.0 and r1["rescore_ms_mean"] == 2.0
    assert r1["q_max"] == 7
    # second window sees only the increment
    f.c["frames"] += 50
    f.c["detects"] += 1
    f.c["rescores"] += 5
    f.c["rescore_ms"] += 5.0
    r2 = f.hb_row(f.t_start + 20.0)
    assert r2["frames_s"] == 5.0 and r2["detects"] == 1
    assert r2["rescores_s"] == 0.5 and r2["rescore_ms_mean"] == 1.0
    assert r2["q_max"] == 0, "q_max is a per-window high-water mark and must reset"


def test_heartbeat_reports_liveness_fields():
    f = wire(feed(hb_every=60.0), board(n=3))
    f.books["1000"].snapshot([], [(0.5, 1)])
    r = f.hb_row(f.t_start + 60.0)
    assert r["boards"] == 1 and r["tokens"] == 6 and r["books_seeded"] == 1
    assert r["rss_mb"] > 0 and r["rss_peak_mb"] > 0 and r["cpu_pct"] >= 0
    assert r["type"] == "hb" and r["shards"] == 1
    assert set(("dropped", "reconnects", "resyncs", "drift", "gaps")) <= set(r)


def test_heartbeat_zero_rescores_does_not_divide_by_zero():
    f = feed(hb_every=60.0)
    assert f.hb_row(f.t_start + 60.0)["rescore_ms_mean"] == 0.0


def test_emit_stamps_source_ws():
    """Every row must be attributable in the 24h diff against the sweep log."""
    f = feed()
    row = {"type": "detect"}
    f.emit(row)
    assert row["source"] == "ws"


# --------------------------------------------------------------------------- sharding
def test_reshard_is_stable_and_only_touches_changed_shards():
    f = feed()
    evs = [board(slug=f"s{i}", n=11, base=10000 * i) for i in range(30)]
    add, gone, touched = f.reshard(evs)
    assert add == 30 and gone == 0
    assert len(f.shards) == 2, "22 assets/board, 500/shard -> 22 boards per shard"
    assert all(len(f.shard_assets(i)) <= W.SHARD_ASSETS for i in range(len(f.shards)))
    before = dict(f.slug2shard)
    evs2 = evs + [board(slug="new", n=11, base=990000)]
    add, gone, touched = f.reshard(evs2)
    assert add == 1 and gone == 0
    assert all(before[s] == f.slug2shard[s] for s in before), "existing boards must not move"
    assert len(touched) == 1
    # and a removal frees its books
    add, gone, touched = f.reshard([e for e in evs2 if e["slug"] != "s0"])
    assert gone == 1 and "s0" not in f.by_slug
    assert "0" not in f.books and f.tok2slug.get("0") is None


def test_reshard_drops_token_state_for_delisted_boards():
    f = wire(feed(), board(slug="b", n=3))
    f.books["1000"].snapshot([], [(0.5, 1)])
    f.reshard([])
    assert f.books == {} and f.tok2slug == {} and f.by_slug == {}
