"""Tests for exact order-book fills (walking the real book).

These are pure-math tests on ``walk_asks_for_cash`` — no network. They lock in
the slippage behaviour that makes the simulation realistic:

  - a deep/liquid book fills near the best ask (≈ no slippage);
  - a thin book drags the average price up as the order walks levels;
  - the fill is platform-generic — it operates on (price, size) levels and a
    cash budget, with no coupling to any city, temperature unit, or specific
    market, so it works for the current weather buckets AND any more-liquid /
    international market we scan later.

Run with the repo root on PYTHONPATH; runnable as a script (pytest optional).
"""
from backend.data.orderbook import walk_asks_for_cash


def test_liquid_book_no_slippage():
    # One deep level far exceeds the order: VWAP == best ask.
    asks = [(0.21, 100000.0)]
    fill = walk_asks_for_cash(asks, 500.0)
    assert fill is not None
    assert abs(fill.vwap - 0.21) < 1e-9
    assert fill.fully_filled is True
    assert fill.levels == 1
    # $500 / $0.21 ≈ 2381 contracts
    assert abs(fill.contracts - 500.0 / 0.21) < 1e-6


def test_thin_book_walks_and_slips():
    # Thin top of book: $75 can't fill at 6c, it walks up.
    asks = [(0.06, 18.93), (0.07, 26.91), (0.10, 32.0), (0.50, 5000.0)]
    fill = walk_asks_for_cash(asks, 75.0)
    assert fill is not None
    assert fill.best_ask == 0.06
    # Realized VWAP is far worse than the 6c best ask.
    assert fill.vwap > 0.20
    assert fill.fully_filled is True
    # cash spent ≈ requested (within a cent)
    assert abs(fill.cash - 75.0) < 0.01
    # VWAP is internally consistent: cash / contracts
    assert abs(fill.vwap - fill.cash / fill.contracts) < 1e-9


def test_partial_fill_when_book_too_thin():
    # Total book depth is only 0.06*10 + 0.07*10 = $1.30 < $75 requested.
    asks = [(0.06, 10.0), (0.07, 10.0)]
    fill = walk_asks_for_cash(asks, 75.0)
    assert fill is not None
    assert fill.fully_filled is False
    assert abs(fill.cash - 1.30) < 1e-9
    assert abs(fill.contracts - 20.0) < 1e-9
    assert abs(fill.book_depth_cash - 1.30) < 1e-9


def test_empty_book_returns_none():
    assert walk_asks_for_cash([], 75.0) is None
    assert walk_asks_for_cash([(0.5, 100.0)], 0.0) is None


def test_partial_level_consumption():
    # $5 budget against a 0.10 level with 100 contracts: take exactly 50.
    asks = [(0.10, 100.0)]
    fill = walk_asks_for_cash(asks, 5.0)
    assert fill is not None
    assert abs(fill.contracts - 50.0) < 1e-9
    assert abs(fill.vwap - 0.10) < 1e-9
    assert fill.fully_filled is True


if __name__ == "__main__":
    test_liquid_book_no_slippage()
    test_thin_book_walks_and_slips()
    test_partial_fill_when_book_too_thin()
    test_empty_book_returns_none()
    test_partial_level_consumption()
    print("All order-book fill tests passed.")
