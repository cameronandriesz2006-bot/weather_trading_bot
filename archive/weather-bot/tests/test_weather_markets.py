"""Tests for Polymarket weather market parsing (Phase 3 rebuild).

Covers the range-bucket label parser, the event-slug parser (city/metric/date),
and the rounding-aware ensemble range-membership math.
"""
from datetime import date

from backend.data.weather_markets import (
    parse_bucket_label,
    parse_event_slug,
    WeatherMarket,
)
from backend.data.weather import EnsembleForecast


def test_parse_bucket_label_ranges():
    assert parse_bucket_label("82-83°F") == (82, 83)
    assert parse_bucket_label("94-95°F") == (94, 95)
    assert parse_bucket_label("81°F or below") == (None, 81)
    assert parse_bucket_label("100°F or higher") == (100, None)


def test_parse_bucket_label_rejects_garbage():
    assert parse_bucket_label("") is None
    assert parse_bucket_label("sometime next week") is None
    assert parse_bucket_label("83-82°F") is None  # inverted range -> skip


def test_parse_event_slug_ok():
    assert parse_event_slug("highest-temperature-in-nyc-on-june-12-2026") == (
        "nyc", "high", date(2026, 6, 12),
    )
    assert parse_event_slug("lowest-temperature-in-los-angeles-on-june-13-2026") == (
        "los_angeles", "low", date(2026, 6, 13),
    )


def test_parse_event_slug_rejects_unknown_or_malformed():
    assert parse_event_slug("highest-temperature-in-tokyo-on-june-12-2026") is None  # untracked city
    assert parse_event_slug("btc-up-or-down-june-12") is None
    assert parse_event_slug("highest-temperature-in-nyc-on-flurpday-12-2026") is None


def test_fraction_in_range_rounds_to_bucket():
    # Settlement rounds to nearest degree: bucket [82,83] covers [81.5, 83.5)
    members = [82.4, 83.4, 81.4, 84.6]
    assert EnsembleForecast._fraction_in_range(members, 82, 83) == 0.5  # 82.4, 83.4
    assert EnsembleForecast._fraction_in_range(members, None, 81) == 0.25  # only 81.4 (<81.5)
    assert EnsembleForecast._fraction_in_range(members, 100, None) == 0.0
    assert EnsembleForecast._fraction_in_range([], 82, 83) == 0.5  # no data -> neutral


def test_weather_market_direction_compat():
    def mk(low, high):
        return WeatherMarket(
            slug="s", market_id="1", platform="polymarket", title="t",
            city_key="nyc", city_name="New York City", target_date=date(2026, 6, 12),
            metric="high", low_f=low, high_f=high, bucket_label="x",
            yes_price=0.5, no_price=0.5,
        )
    assert mk(None, 81).direction == "below"
    assert mk(100, None).direction == "above"
    assert mk(82, 83).direction == "range"
    assert mk(82, 83).threshold_f == 82


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("All weather-market parsing tests passed.")
