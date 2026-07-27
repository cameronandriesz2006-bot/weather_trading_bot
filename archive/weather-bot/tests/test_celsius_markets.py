"""Tests for international (Celsius) weather-market support.

The international Polymarket daily-temperature markets resolve in whole/▒-decimal
degrees CELSIUS with SINGLE-degree buckets ("18°C"), unlike the US °F markets'
two-degree ranges ("82-83°F"). We handle each market entirely in its native unit
— no temperature is ever converted — so these tests assert:

  1. the bucket parser reads °C single-degree labels + open tails, and still
     reads the US °F ranges;
  2. the international event slugs map to city keys;
  3. a °C bucket "N°C" prices the rounding interval [N-0.5, N+0.5) in °C, so a
     forecast centred on N peaks on bucket N (a unit mismatch would give ~0);
  4. the σ-floor constants (defined in °F) are scaled to °C by exactly 1/1.8.

Run with the repo root on PYTHONPATH; runnable as a script (pytest optional).
"""
from datetime import date

from backend.config import settings
# These assert the pure °C bucket MATH (rounding interval, symmetry, σ scaling),
# which must hold independent of any per-station bias. Disable the live station
# bias so a real (e.g. London −0.16°C) correction doesn't shift the distribution
# and break the symmetry assertions — bias correctness is tested separately in
# tests/test_bias_correction.py.
settings.WEATHER_BIAS_ENABLED = False
from backend.data.weather import EnsembleForecast, CITY_CONFIG
from backend.data.weather_markets import parse_bucket_label, parse_event_slug


# --- 1. bucket parsing -------------------------------------------------------
def test_parse_celsius_single_degree():
    assert parse_bucket_label("18°C") == (18.0, 18.0)
    assert parse_bucket_label("21°C") == (21.0, 21.0)
    # bare number (unit already stripped upstream) still reads as a single degree
    assert parse_bucket_label("9") == (9.0, 9.0)


def test_parse_celsius_open_tails():
    assert parse_bucket_label("17°C or below") == (None, 17.0)
    assert parse_bucket_label("27°C or higher") == (27.0, None)
    assert parse_bucket_label("23°C or below") == (None, 23.0)


def test_parse_fahrenheit_ranges_still_work():
    assert parse_bucket_label("82-83°F") == (82.0, 83.0)
    assert parse_bucket_label("81°F or below") == (None, 81.0)
    assert parse_bucket_label("100°F or higher") == (100.0, None)


# --- 2. slug -> city key -----------------------------------------------------
def test_intl_slugs_map_to_known_cities():
    for slug, key in [
        ("highest-temperature-in-london-on-june-14-2026", "london"),
        ("highest-temperature-in-tokyo-on-june-14-2026", "tokyo"),
        ("highest-temperature-in-hong-kong-on-june-14-2026", "hong_kong"),
        ("lowest-temperature-in-paris-on-june-14-2026", "paris"),
    ]:
        parsed = parse_event_slug(slug)
        assert parsed is not None, slug
        assert parsed[0] == key
        assert key in CITY_CONFIG
        assert CITY_CONFIG[key]["unit"] == "C"


# --- 3. native-unit probability (no conversion) ------------------------------
def _forecast(mean: float, unit: str, spread: float = 0.4) -> EnsembleForecast:
    # A tight cluster of members around `mean`, in the given unit.
    members = [mean - spread, mean, mean + spread] * 7
    return EnsembleForecast(
        city_key="london" if unit == "C" else "nyc",
        city_name="Test",
        target_date=date.today(),
        member_highs=members,
        member_lows=members,
        unit=unit,
    )


def test_celsius_bucket_peaks_on_the_forecast_degree():
    # Forecast high 21°C: bucket "21" must be the most likely, symmetric around it,
    # and clearly non-zero (a °C/°F mismatch would collapse these to ~0).
    f = _forecast(21.0, "C")
    p19 = f.probability_high_in_range(19, 19)
    p20 = f.probability_high_in_range(20, 20)
    p21 = f.probability_high_in_range(21, 21)
    p22 = f.probability_high_in_range(22, 22)
    p23 = f.probability_high_in_range(23, 23)
    assert p21 > 0.2
    assert p21 > p20 > p19
    assert p21 > p22 > p23
    assert abs(p20 - p22) < 0.05  # roughly symmetric around the mean


def test_celsius_rounding_interval_is_half_degree():
    # P("21°C") must equal CDF(21.5) - CDF(20.5) under the fitted Normal — i.e.
    # the bucket covers exactly [20.5, 21.5) in °C.
    f = _forecast(21.0, "C")
    sigma = f._effective_sigma(f.std_high)
    mean = f.corrected_mean("high")
    expected = (EnsembleForecast._normal_cdf(21.5, mean, sigma)
                - EnsembleForecast._normal_cdf(20.5, mean, sigma))
    assert abs(f.probability_high_in_range(21, 21) - expected) < 1e-9


# --- 4. sigma-floor unit scaling --------------------------------------------
def test_sigma_floor_scaled_to_celsius():
    # With a near-zero raw spread the floor dominates. The °C floor must be the
    # °F floor / 1.8 (a temperature *spread* converts by ratio, no +32 offset).
    f_c = _forecast(21.0, "C", spread=0.0)
    f_f = _forecast(70.0, "F", spread=0.0)
    eff_c = f_c._effective_sigma(0.0)
    eff_f = f_f._effective_sigma(0.0)
    assert abs(eff_f - settings.WEATHER_SIGMA_FLOOR_F) < 1e-9
    assert abs(eff_c - settings.WEATHER_SIGMA_FLOOR_F / 1.8) < 1e-9


if __name__ == "__main__":
    test_parse_celsius_single_degree()
    test_parse_celsius_open_tails()
    test_parse_fahrenheit_ranges_still_work()
    test_intl_slugs_map_to_known_cities()
    test_celsius_bucket_peaks_on_the_forecast_degree()
    test_celsius_rounding_interval_is_half_degree()
    test_sigma_floor_scaled_to_celsius()
    print("All Celsius-market tests passed.")
