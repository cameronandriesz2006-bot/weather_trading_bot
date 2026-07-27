"""Faithfulness tests for the limit-order execution layer (backend/core/execution.py).

Network-free: a FakeMarketData scripts the book + trade tape and a fake clock drives expiry, so
every Polymarket limit-order behaviour is asserted deterministically — crossing with price
improvement, price-time (queue) priority, partial fills, GTD auto-expiry with the +60 security
threshold, cancel, and the FOK/FAK semantics. Also asserts the live executor refuses to act in
SIMULATION_MODE.

Run:  PYTHONPATH=. venv/bin/python tests/test_execution.py
"""
import asyncio

from backend.core.execution import (
    SimulatedExecutor, LivePolymarketExecutor, OrderManager, OrderSide, TimeInForce, OrderStatus,
    BookSnapshot, TradePrint, shares_for_cash, _gtd_expiration, GTD_SECURITY_THRESHOLD_S)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeMarketData:
    def __init__(self, book=None, trades=None):
        self._book = book
        self._trades = list(trades or [])

    def set_book(self, book):
        self._book = book

    def add_trades(self, *trades):
        self._trades.extend(trades)

    async def book(self, token_id):
        return self._book

    async def trades_since(self, token_id, since_ts):
        return [t for t in self._trades if t.ts > since_ts]


def _book(bids, asks):
    return BookSnapshot(bids=sorted(bids, key=lambda x: -x[0]),
                        asks=sorted(asks, key=lambda x: x[0]))


async def test_crossing_with_price_improvement():
    md = FakeMarketData(_book(bids=[(0.28, 50)], asks=[(0.30, 100), (0.32, 200)]))
    ex = SimulatedExecutor(md, clock=Clock())
    o = await ex.place_order("T", OrderSide.BUY, price=0.31, size=150)
    # crosses only the 0.30 ask (0.32 > limit), at the BOOK price (0.30, not our 0.31)
    assert len(o.fills) == 1 and o.fills[0].taker and abs(o.fills[0].price - 0.30) < 1e-9
    assert abs(o.filled_size - 100) < 1e-9 and abs(o.remaining_size - 50) < 1e-9
    assert o.status == OrderStatus.PARTIALLY_FILLED
    assert abs(o.avg_fill_price - 0.30) < 1e-9
    print("PASS crossing_with_price_improvement")


async def test_sell_crossing():
    md = FakeMarketData(_book(bids=[(0.32, 40), (0.31, 30), (0.28, 100)], asks=[(0.40, 50)]))
    ex = SimulatedExecutor(md, clock=Clock())
    o = await ex.place_order("T", OrderSide.SELL, price=0.30, size=100)
    # crosses bids >= 0.30, highest first: 40@0.32 then 30@0.31; 0.28 < limit so 30 rests
    assert [round(f.price, 2) for f in o.fills] == [0.32, 0.31]
    assert abs(o.filled_size - 70) < 1e-9 and abs(o.remaining_size - 30) < 1e-9
    print("PASS sell_crossing")


async def test_resting_fill_from_flow():
    md = FakeMarketData(_book(bids=[(0.28, 10)], asks=[(0.35, 100)]))  # no cross (asks>limit)
    clk = Clock()
    ex = SimulatedExecutor(md, clock=clk)
    o = await ex.place_order("T", OrderSide.BUY, price=0.31, size=50)
    assert o.status == OrderStatus.OPEN and o._queue_ahead == 0  # nothing resting at/above 0.31
    md.add_trades(TradePrint(0.31, 30, clk.t + 1), TradePrint(0.29, 40, clk.t + 2))
    clk.t += 5
    await ex.poll_order(o)
    # both prints <= 0.31 hit us; maker fills at OUR price 0.31; 30 then 20 -> full
    assert o.status == OrderStatus.FILLED and abs(o.filled_size - 50) < 1e-9
    assert all(not f.taker and abs(f.price - 0.31) < 1e-9 for f in o.fills)
    print("PASS resting_fill_from_flow")


async def test_queue_priority():
    md = FakeMarketData(_book(bids=[(0.31, 80)], asks=[(0.35, 100)]))  # 80 resting AT our price
    clk = Clock()
    ex = SimulatedExecutor(md, clock=clk)
    o = await ex.place_order("T", OrderSide.BUY, price=0.31, size=50)
    assert abs(o._queue_ahead - 80) < 1e-9
    md.add_trades(TradePrint(0.31, 60, clk.t + 1))
    clk.t += 2
    await ex.poll_order(o)
    assert o.filled_size == 0 and abs(o._queue_ahead - 20) < 1e-9  # 60 ate queue, none for us
    md.add_trades(TradePrint(0.31, 50, clk.t + 1))
    clk.t += 2
    await ex.poll_order(o)
    # 50 flow: 20 finishes queue, 30 fills us
    assert abs(o.filled_size - 30) < 1e-9 and o.status == OrderStatus.PARTIALLY_FILLED
    print("PASS queue_priority")


async def test_gtd_expiry_keeps_partial():
    md = FakeMarketData(_book(bids=[], asks=[(0.35, 100)]))
    clk = Clock()
    ex = SimulatedExecutor(md, clock=clk)
    o = await ex.place_order("T", OrderSide.BUY, price=0.31, size=50,
                             tif=TimeInForce.GTD, ttl_seconds=300)
    assert abs(o.expiration_ts - (clk.t + GTD_SECURITY_THRESHOLD_S + 300)) < 1e-9
    md.add_trades(TradePrint(0.30, 20, clk.t + 10))   # partial fill before expiry
    clk.t += 100
    await ex.poll_order(o)
    assert abs(o.filled_size - 20) < 1e-9 and not o.is_terminal
    clk.t += 400                                       # now past expiration
    await ex.poll_order(o)
    assert o.status == OrderStatus.EXPIRED and abs(o.filled_size - 20) < 1e-9
    print("PASS gtd_expiry_keeps_partial")


async def test_cancel():
    md = FakeMarketData(_book(bids=[], asks=[(0.35, 100)]))
    ex = SimulatedExecutor(md, clock=Clock())
    o = await ex.place_order("T", OrderSide.BUY, price=0.31, size=50)
    assert await ex.cancel_order(o) is True and o.status == OrderStatus.CANCELLED
    assert await ex.cancel_order(o) is False        # already terminal
    print("PASS cancel")


async def test_fok():
    md = FakeMarketData(_book(bids=[], asks=[(0.30, 100)]))
    ex = SimulatedExecutor(md, clock=Clock())
    o = await ex.place_order("T", OrderSide.BUY, price=0.31, size=150, tif=TimeInForce.FOK)
    assert o.status == OrderStatus.REJECTED and o.filled_size == 0   # couldn't fully fill -> nothing
    o2 = await ex.place_order("T", OrderSide.BUY, price=0.31, size=80, tif=TimeInForce.FOK)
    assert o2.status == OrderStatus.FILLED and abs(o2.filled_size - 80) < 1e-9
    print("PASS fok")


async def test_fak():
    md = FakeMarketData(_book(bids=[], asks=[(0.30, 100)]))
    ex = SimulatedExecutor(md, clock=Clock())
    o = await ex.place_order("T", OrderSide.BUY, price=0.31, size=150, tif=TimeInForce.FAK)
    assert o.status == OrderStatus.CANCELLED and abs(o.filled_size - 100) < 1e-9  # partial kept, rest killed
    print("PASS fak")


async def test_cash_sizing():
    assert abs(shares_for_cash(100, 0.25) - 400) < 1e-9
    assert _gtd_expiration(1000.0, 120) == 1000.0 + 60 + 120
    md = FakeMarketData(_book(bids=[], asks=[(0.35, 100)]))
    ex = SimulatedExecutor(md, clock=Clock())
    o = await ex.place_limit_for_cash("T", OrderSide.BUY, price=0.25, cash=50, ttl_seconds=120)
    assert o.tif == TimeInForce.GTD and abs(o.size - 200) < 1e-9   # 50 / 0.25
    print("PASS cash_sizing")


async def test_order_manager_flow_and_hard_ttl():
    md = FakeMarketData(_book(bids=[(0.28, 5)], asks=[(0.35, 100)]))
    clk = Clock()
    mgr = OrderManager(SimulatedExecutor(md, clock=clk), clock=clk)
    o = await mgr.submit_limit("T", OrderSide.BUY, price=0.31, cash=15.5)  # ~50 shares
    assert o.order_id in mgr.open
    md.add_trades(TradePrint(0.31, 1000, clk.t + 1))
    clk.t += 2
    await mgr.poll_all()
    assert o.status == OrderStatus.FILLED and o.order_id in {d.order_id for d in mgr.done}
    assert o.order_id not in mgr.open
    # hard-ttl backstop cancels a still-open GTC order
    o2 = await mgr.submit_limit("T", OrderSide.BUY, price=0.31, cash=15.5)
    clk.t += 999
    await mgr.poll_all(hard_ttl_seconds=120)
    assert o2.status == OrderStatus.CANCELLED and o2.order_id not in mgr.open
    print("PASS order_manager_flow_and_hard_ttl")


async def test_live_executor_gated():
    from backend.config import settings
    assert settings.SIMULATION_MODE is True
    ex = LivePolymarketExecutor(private_key="0xdeadbeef")
    raised = False
    try:
        await ex.place_order("T", OrderSide.BUY, price=0.5, size=10)
    except RuntimeError as e:
        raised = "SIMULATION_MODE" in str(e)
    assert raised, "live executor must refuse to place orders while SIMULATION_MODE is True"
    print("PASS live_executor_gated")


async def _main():
    for fn in [test_crossing_with_price_improvement, test_sell_crossing, test_resting_fill_from_flow,
               test_queue_priority, test_gtd_expiry_keeps_partial, test_cancel, test_fok, test_fak,
               test_cash_sizing, test_order_manager_flow_and_hard_ttl, test_live_executor_gated]:
        await fn()
    print("\nALL EXECUTION TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(_main())
