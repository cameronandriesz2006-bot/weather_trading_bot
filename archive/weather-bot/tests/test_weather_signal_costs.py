"""Tests for cost-aware signal gating (Phase 6).

`passes_threshold` must require the edge to clear the minimum AFTER costs, and
the effective entry price to be within the cap.
"""
from datetime import date

from backend.config import settings
from backend.core.weather_signals import WeatherTradingSignal
from backend.data.weather_markets import WeatherMarket


def _market(liquidity=5000.0, volume=5000.0, event_market_mean=None):
    return WeatherMarket(
        slug="s", market_id="1", platform="polymarket", title="t",
        city_key="nyc", city_name="New York City", target_date=date(2026, 6, 12),
        metric="high", low_f=82, high_f=83, bucket_label="82-83°F",
        yes_price=0.5, no_price=0.5, spread=0.02, liquidity=liquidity,
        volume=volume, event_market_mean=event_market_mean,
    )


def _signal(net_edge, entry_price, liquidity=5000.0, rel_spread=0.04,
            volume=5000.0, event_market_mean=None, market_gap=None):
    return WeatherTradingSignal(
        market=_market(liquidity, volume, event_market_mean),
        net_edge=net_edge, entry_price=entry_price,
        rel_spread=rel_spread, market_gap=market_gap,
    )


def test_actionable_when_net_edge_and_entry_ok():
    assert _signal(net_edge=0.10, entry_price=0.50).passes_threshold is True


def test_filtered_when_volume_below_min():
    # Real resting liquidity but almost no actual trading -> phantom book, skip.
    assert _signal(net_edge=0.20, entry_price=0.50, volume=70).passes_threshold is False


def test_filtered_when_market_gap_too_large():
    # Our forecast mean far from the market-implied mean -> we're miscalibrated.
    big = settings.WEATHER_MAX_MARKET_GAP_F + 1.0
    assert _signal(net_edge=0.20, entry_price=0.50, market_gap=big).passes_threshold is False
    # A small gap still trades.
    assert _signal(net_edge=0.20, entry_price=0.50, market_gap=0.5).passes_threshold is True


def test_filtered_when_net_edge_below_threshold():
    # Gross edge might clear 8%, but after costs it doesn't.
    assert _signal(net_edge=0.05, entry_price=0.50).passes_threshold is False


def test_filtered_when_entry_above_cap():
    assert settings.WEATHER_MAX_ENTRY_PRICE < 0.80
    assert _signal(net_edge=0.20, entry_price=0.80).passes_threshold is False


def test_filtered_when_no_entry_price():
    assert _signal(net_edge=0.20, entry_price=0.0).passes_threshold is False


def test_filtered_when_liquidity_too_low():
    # Healthy edge/price/spread, but the book is too thin to trade.
    assert settings.WEATHER_MIN_LIQUIDITY > 100.0
    assert _signal(net_edge=0.20, entry_price=0.50, liquidity=100.0).passes_threshold is False


def test_filtered_when_rel_spread_too_wide():
    # A 2c spread on a 4c contract is a 50% mirage even though it "looks" tiny.
    assert settings.WEATHER_MAX_REL_SPREAD < 0.50
    assert _signal(net_edge=0.20, entry_price=0.50, rel_spread=0.50).passes_threshold is False


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("All weather-signal cost-gating tests passed.")
