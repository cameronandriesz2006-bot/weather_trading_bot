"""Tests for per-station bias correction.

Bias = mean(forecast - actual). We SUBTRACT it from the forecast mean, so a
positive (warm) bias should pull the priced mean DOWN. The gate must return 0
when disabled, under-sampled, or missing, and clamp to +/- MAX_SHIFT.
"""
from datetime import date

from backend.config import settings
from backend.data import weather
from backend.data.weather import EnsembleForecast, get_station_bias


def _set(bias_f, samples=50, enabled=True, min_samples=10, max_shift=4.0):
    weather._bias_cache = {"nyc": {"high": {"bias_f": bias_f, "samples": samples}}}
    settings.WEATHER_BIAS_ENABLED = enabled
    settings.WEATHER_BIAS_MIN_SAMPLES = min_samples
    settings.WEATHER_BIAS_MAX_SHIFT_F = max_shift


def test_returns_bias_when_sampled():
    _set(2.0)
    assert get_station_bias("nyc", "high") == 2.0


def test_zero_when_disabled():
    _set(2.0, enabled=False)
    assert get_station_bias("nyc", "high") == 0.0


def test_zero_when_under_min_samples():
    _set(2.0, samples=5, min_samples=10)
    assert get_station_bias("nyc", "high") == 0.0


def test_zero_when_station_missing():
    _set(2.0)
    assert get_station_bias("denver", "high") == 0.0  # not in the injected cache


def test_clamped_to_max_shift():
    _set(10.0, max_shift=4.0)
    assert get_station_bias("nyc", "high") == 4.0
    _set(-10.0, max_shift=4.0)
    assert get_station_bias("nyc", "high") == -4.0


def test_correction_shifts_probability_the_right_way():
    fc = EnsembleForecast(
        city_key="nyc", city_name="NYC", target_date=date.today(),
        member_highs=[80.0] * 31, member_lows=[60.0] * 31,
    )
    # No bias: priced mean = 80
    _set(0.0)
    up_nobias = fc.probability_high_in_range(82, 84)   # bucket above the mean
    down_nobias = fc.probability_high_in_range(75, 77)  # bucket below the mean

    # Warm bias +3: priced mean pulled down to 77
    _set(3.0)
    assert fc.corrected_mean("high") == 77.0
    up_bias = fc.probability_high_in_range(82, 84)
    down_bias = fc.probability_high_in_range(75, 77)

    assert up_bias < up_nobias      # warm correction -> less likely to land high
    assert down_bias > down_nobias  # ...and more likely to land low


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    weather.reload_station_bias()  # drop the test cache
    print("All bias-correction tests passed.")
