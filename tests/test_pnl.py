"""Tests for calculate_pnl with the cash-staked convention.

`size` is the cash put down. A winning trade pays the prediction-market net odds
(size * (1-p)/p); a losing trade loses the full stake (-size). Fees subtracted.
"""
from types import SimpleNamespace

from backend.core.settlement import calculate_pnl


def _trade(direction, entry_price, size, fee=0.0):
    return SimpleNamespace(direction=direction, entry_price=entry_price, size=size, fee=fee)


def test_yes_win_pays_net_odds():
    # stake $100 at 25c -> win pays 100 * 0.75/0.25 = 300
    assert calculate_pnl(_trade("yes", 0.25, 100), 1.0) == 300.0


def test_yes_loss_loses_full_stake():
    assert calculate_pnl(_trade("yes", 0.25, 100), 0.0) == -100.0


def test_no_win_pays_net_odds():
    # NO bought at 40c, NO wins (settlement 0.0): 100 * 0.6/0.4 = 150
    assert calculate_pnl(_trade("no", 0.40, 100), 0.0) == 150.0


def test_no_loss_loses_full_stake():
    assert calculate_pnl(_trade("no", 0.40, 100), 1.0) == -100.0


def test_cheap_longshot_win_is_large():
    # stake $75 at 5c -> win pays 75 * 0.95/0.05 = 1425
    assert calculate_pnl(_trade("yes", 0.05, 75), 1.0) == 1425.0


def test_fee_is_subtracted():
    assert calculate_pnl(_trade("yes", 0.50, 100, fee=2.0), 1.0) == 98.0   # 100*0.5/0.5 - 2
    assert calculate_pnl(_trade("yes", 0.50, 100, fee=2.0), 0.0) == -102.0 # -100 - 2


def test_legacy_up_down_vocab_still_works():
    assert calculate_pnl(_trade("up", 0.25, 100), 1.0) == 300.0
    assert calculate_pnl(_trade("down", 0.25, 100), 0.0) == calculate_pnl(_trade("no", 0.25, 100), 0.0)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("All P&L tests passed.")
