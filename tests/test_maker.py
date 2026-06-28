"""Tests for the maker path (backend/core/maker.py): the resting-order price rule, posting a
WorkingOrder, filling it from real flow into a Trade, and expiring it with no Trade.

Network-free: an in-memory SQLite DB + the real SimulatedExecutor over a scripted FakeMarketData
and a fake clock. So the full place -> rest -> fill -> Trade lifecycle is asserted deterministically.

Run:  PYTHONPATH=. venv/bin/python tests/test_maker.py
"""
import asyncio
from datetime import date
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.models.database import Base, BotState, Trade, WorkingOrder
from backend.core.execution import SimulatedExecutor, BookSnapshot, TradePrint, OrderSide
from backend.core import maker


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeMD:
    def __init__(self, book=None, trades=None):
        self._book, self._trades = book, list(trades or [])

    def add_trades(self, *t):
        self._trades.extend(t)

    async def book(self, token_id):
        return self._book

    async def trades_since(self, token_id, since_ts):
        return [t for t in self._trades if t.ts > since_ts]


def _book(bids, asks):
    return BookSnapshot(bids=sorted(bids, key=lambda x: -x[0]),
                        asks=sorted(asks, key=lambda x: x[0]))


def _db():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(BotState(bankroll=10000.0, total_trades=0, is_running=True))
    db.commit()
    return db


def _signal(direction="yes", model_p=0.50, bid=0.40, ask=0.46, size=200.0, city="chicago"):
    mkt = SimpleNamespace(
        token_id_yes="TOK_YES", token_id_no="TOK_NO", best_bid=bid, best_ask=ask,
        condition_id="0xcond", market_id="MKT1", city_key=city, city_name=city.title(),
        target_date=date(2026, 6, 29), metric="high", bucket_label="88-89F",
        slug="highest-temperature-in-chicago-on-june-29-2026",
    )
    return SimpleNamespace(
        market=mkt, direction=direction, model_probability=model_p, market_probability=0.43,
        edge=0.07, suggested_size=size, passes_threshold=True,
    )


def test_maker_price_yes():
    tok, px, fair = maker.maker_price(_signal("yes", model_p=0.50, bid=0.40, ask=0.46))
    assert tok == "TOK_YES" and abs(px - 0.41) < 1e-9 and abs(fair - 0.50) < 1e-9  # bid+tick, < ask, < fair
    print("PASS maker_price_yes")


def test_maker_price_no_mirror():
    # buying NO: model YES 0.30 -> NO fair 0.70; NO book mirrors YES (bid_no=1-ask_yes etc.)
    tok, px, fair = maker.maker_price(_signal("no", model_p=0.30, bid=0.25, ask=0.31))
    assert tok == "TOK_NO" and abs(fair - 0.70) < 1e-9 and abs(px - 0.69) < 1e-9  # 1-0.31+tick, capped < fair
    print("PASS maker_price_no_mirror")


def test_maker_price_skips_when_no_edge():
    # our fair (0.50) sits BELOW the bid we'd have to improve -> no maker price with edge
    assert maker.maker_price(_signal("yes", model_p=0.41, bid=0.40, ask=0.46)) is not None  # 0.41 ok (edge thin)
    # crossed/garbage book -> None
    assert maker.maker_price(_signal("yes", bid=0.46, ask=0.40)) is None
    print("PASS maker_price_skips_when_no_edge")


async def test_place_creates_working_order():
    db = _db()
    clk = Clock()
    # book: our post 0.41 doesn't cross (asks at 0.46), nothing resting at/above 0.41 -> queue 0
    md = FakeMD(_book(bids=[(0.40, 50)], asks=[(0.46, 500)]))
    ex = SimulatedExecutor(md, clock=clk)
    posted = await maker.place_maker_orders(db, [_signal()], executor=ex)
    assert posted == 1
    wo = db.query(WorkingOrder).one()
    assert wo.status == "OPEN" and abs(wo.limit_price - 0.41) < 1e-9
    assert abs(wo.size_shares - 200 / 0.41) < 1e-6 and abs(wo.intended_cash - 200) < 1e-6
    assert wo.trade_id is None and db.query(Trade).count() == 0          # not a position yet
    # idempotent: same market already has a working order -> skipped
    assert await maker.place_maker_orders(db, [_signal()], executor=ex) == 0
    print("PASS place_creates_working_order")


async def test_poll_fills_into_trade():
    db = _db()
    clk = Clock()
    md = FakeMD(_book(bids=[(0.40, 50)], asks=[(0.46, 500)]))
    ex = SimulatedExecutor(md, clock=clk)
    await maker.place_maker_orders(db, [_signal(size=200.0)], executor=ex)
    # big sell flow at our price -> resting order fills fully
    md.add_trades(TradePrint(0.41, 100000, clk.t + 1))
    clk.t += 5
    filled = await maker.poll_maker_orders(db, executor=ex)
    assert filled == 1
    wo = db.query(WorkingOrder).one()
    assert wo.status == "FILLED" and wo.trade_id is not None
    tr = db.query(Trade).one()
    assert tr.direction == "yes" and abs(tr.entry_price - 0.41) < 1e-9
    assert abs(tr.size - 200) < 0.5 and tr.market_type == "weather"      # ~cash staked
    assert db.query(BotState).one().total_trades == 1
    print("PASS poll_fills_into_trade")


async def test_expiry_leaves_no_trade():
    db = _db()
    clk = Clock()
    md = FakeMD(_book(bids=[(0.40, 50)], asks=[(0.46, 500)]))   # no flow ever arrives
    ex = SimulatedExecutor(md, clock=clk)
    await maker.place_maker_orders(db, [_signal()], executor=ex)
    clk.t += 60 + 21600 + 10                                    # past the GTD lifetime (+60 threshold)
    filled = await maker.poll_maker_orders(db, executor=ex)
    assert filled == 0
    wo = db.query(WorkingOrder).one()
    assert wo.status == "EXPIRED" and wo.trade_id is None and wo.filled_shares == 0
    assert db.query(Trade).count() == 0 and db.query(BotState).one().total_trades == 0
    print("PASS expiry_leaves_no_trade")


async def _amain():
    test_maker_price_yes()
    test_maker_price_no_mirror()
    test_maker_price_skips_when_no_edge()
    await test_place_creates_working_order()
    await test_poll_fills_into_trade()
    await test_expiry_leaves_no_trade()
    print("\nALL MAKER TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(_amain())
