"""Tests for the fitted/widened forecast distribution (Phase 4).

Verifies the Normal-fit bucket probabilities are honest: a unanimous ensemble
no longer implies ~100% certainty, buckets tile to ~1, mass concentrates near
the mean, and longer lead times spread the distribution wider.
"""
from datetime import date, timedelta

from backend.config import settings
# These assert the pure distribution MATH; keep them independent of any live
# per-station bias (tested separately in tests/test_bias_correction.py).
settings.WEATHER_BIAS_ENABLED = False
from backend.data.weather import EnsembleForecast


def _fc(member_highs, target_date=None):
    return EnsembleForecast(
        city_key="nyc", city_name="New York City",
        target_date=target_date or date.today(),
        member_highs=list(member_highs), member_lows=list(member_highs),
    )


def test_unanimous_ensemble_is_not_certain():
    """All members agree on 85F, but the floor sigma keeps us humble."""
    fc = _fc([85.0] * 31)
    p = fc.probability_high_in_range(85, 86)   # the bucket the point estimate sits in
    assert 0.2 < p < 0.6, p                     # NOT ~1.0
    # Raw member-counting would have said 1.0 here:
    assert EnsembleForecast._fraction_in_range([85.0] * 31, 85, 86) == 1.0


def test_full_range_is_one():
    fc = _fc([80, 82, 84, 85, 86, 88, 90])
    assert abs(fc.probability_high_in_range(None, None) - 1.0) < 1e-9


def test_contiguous_buckets_sum_to_one():
    fc = _fc([83, 84, 85, 85, 86, 87])
    buckets = [(None, 80), (81, 82), (83, 84), (85, 86), (87, 88), (89, None)]
    total = sum(fc.probability_high_in_range(lo, hi) for lo, hi in buckets)
    assert abs(total - 1.0) < 1e-9, total


def test_mass_concentrates_near_mean():
    fc = _fc([85.0] * 31)
    near = fc.probability_high_in_range(85, 86)
    far = fc.probability_high_in_range(95, 96)
    assert near > far
    assert far < 0.05


def test_longer_lead_time_widens_distribution():
    members = [85.0] * 31
    today = _fc(members, target_date=date.today())
    future = _fc(members, target_date=date.today() + timedelta(days=4))
    # Wider sigma spreads mass out of the modal bucket.
    assert future.probability_high_in_range(85, 86) < today.probability_high_in_range(85, 86)


# --- Observed-so-far hard bound (intraday floor/ceiling) ---------------------

def test_observed_floor_zeros_impossible_buckets_and_concentrates():
    # Forecast centered ~86, but the day has ALREADY hit 88 -> final high >= 88.
    fc = _fc([84, 85, 86, 87, 88] * 6)
    # Buckets entirely below the observed high are now physically impossible:
    assert fc.probability_high_in_range(84, 85, floor=88.0) == 0.0
    assert fc.probability_high_in_range(86, 87, floor=88.0) == 0.0
    # The bucket holding the observed high soaks up the piled mass:
    assert fc.probability_high_in_range(88, 89, floor=88.0) > 0.8
    # Buckets above the floor are unchanged:
    assert abs(fc.probability_high_in_range(90, 91, floor=88.0)
               - fc.probability_high_in_range(90, 91)) < 1e-9


def test_observed_floor_buckets_still_sum_to_one():
    fc = _fc([84, 85, 86, 87, 88] * 6)
    buckets = [(None, 83), (84, 85), (86, 87), (88, 89), (90, 91), (92, None)]
    total = sum(fc.probability_high_in_range(lo, hi, floor=88.0) for lo, hi in buckets)
    assert abs(total - 1.0) < 1e-9, total


def test_observed_ceiling_for_lows():
    # Final LOW can't be above the min already recorded today (70).
    fc = _fc([68, 69, 70, 71, 72] * 6)  # _fc sets member_lows == member_highs
    assert fc.probability_low_in_range(72, 73, ceiling=70.0) == 0.0   # impossible
    # The bucket holding the ceiling gains the piled mass:
    assert (fc.probability_low_in_range(70, 71, ceiling=70.0)
            > fc.probability_low_in_range(70, 71))
    # Below the ceiling unchanged:
    assert abs(fc.probability_low_in_range(68, 69, ceiling=70.0)
               - fc.probability_low_in_range(68, 69)) < 1e-9


# --- Intraday σ schedule (Phase 5) -------------------------------------------
# On the in-progress LOCAL day the forecast width should track the empirical residual
# uncertainty curve (wide in the morning, near-zero in the evening) instead of the
# flat σ-floor; future days and missing-curve cities keep the old formula; a hard
# _MIN rail keeps σ from collapsing to ~0.
from backend.data.weather import (  # noqa: E402
    intraday_sigma, intraday_drift, reload_intraday_curve, station_local_hour, station_local_now,
)

reload_intraday_curve()  # load the on-disk curve the asserts below read


def _set_intraday(**overrides):
    """Apply intraday settings, returning the prior values for restoration."""
    old = {k: getattr(settings, k) for k in overrides}
    for k, v in overrides.items():
        setattr(settings, k, v)
    return old


def _restore(old):
    for k, v in old.items():
        setattr(settings, k, v)


def test_intraday_sigma_uses_curve_std():
    old = _set_intraday(WEATHER_INTRADAY_SIGMA_ENABLED=True, WEATHER_INTRADAY_SIGMA_MIN_F=0.3)
    try:
        fc = _fc([85.0] * 31)
        curve7 = intraday_sigma("nyc", "high", 7)
        assert curve7 is not None and curve7 > 1.0           # mornings are genuinely uncertain
        # When the curve applies, the raw ensemble spread is irrelevant: σ == curve std.
        assert abs(fc._effective_sigma(99.0, metric="high", local_hour=7) - curve7) < 1e-9
    finally:
        _restore(old)


def test_intraday_sigma_shrinks_morning_to_evening():
    old = _set_intraday(WEATHER_INTRADAY_SIGMA_ENABLED=True, WEATHER_INTRADAY_SIGMA_MIN_F=0.3)
    try:
        fc = _fc([85.0] * 31)
        morning = fc._effective_sigma(2.0, metric="high", local_hour=7)
        evening = fc._effective_sigma(2.0, metric="high", local_hour=18)
        assert morning > evening, (morning, evening)
        # The sharper evening σ concentrates more mass in the modal bucket.
        sharp = fc.probability_high_in_range(85, 86, local_hour=18)
        diffuse = fc.probability_high_in_range(85, 86, local_hour=7)
        assert sharp > diffuse, (sharp, diffuse)
    finally:
        _restore(old)


def test_intraday_sigma_min_rail_holds():
    # KEY SAFETY RAIL: σ can never collapse below _MIN, even at the most-locked hour
    # (a near-zero σ would turn a tiny edge into an enormous Kelly bet).
    old = _set_intraday(WEATHER_INTRADAY_SIGMA_ENABLED=True, WEATHER_INTRADAY_SIGMA_MIN_F=100.0)
    try:
        fc = _fc([85.0] * 31)  # NYC (°F) -> rail scale 1.0
        assert fc._effective_sigma(2.0, metric="high", local_hour=18) == 100.0
    finally:
        _restore(old)


def test_intraday_disabled_or_no_hour_uses_old_sigma():
    fc = _fc([85.0] * 31)  # same-day, unanimous -> old formula gives the flat floor
    old = _set_intraday(WEATHER_INTRADAY_SIGMA_ENABLED=False)
    try:
        disabled = fc._effective_sigma(0.0, metric="high", local_hour=18)
    finally:
        _restore(old)
    old = _set_intraday(WEATHER_INTRADAY_SIGMA_ENABLED=True)
    try:
        enabled_no_hour = fc._effective_sigma(0.0, metric="high", local_hour=None)
    finally:
        _restore(old)
    # No local_hour (future days / off-day) must bypass the curve entirely...
    assert disabled == enabled_no_hour
    # ...and that fallback is the flat σ-floor, NOT the tiny evening curve std.
    assert abs(disabled - settings.WEATHER_SIGMA_FLOOR_F) < 1e-9


def test_intraday_missing_curve_city_falls_back():
    # Shanghai has no curve -> even with intraday on, σ uses the flat floor (°C-scaled).
    sh = EnsembleForecast(
        city_key="shanghai", city_name="Shanghai", target_date=date.today(),
        member_highs=[30.0] * 31, member_lows=[24.0] * 31, unit="C",
    )
    assert intraday_sigma("shanghai", "high", 14) is None
    old = _set_intraday(WEATHER_INTRADAY_SIGMA_ENABLED=True)
    try:
        eff = sh._effective_sigma(0.0, metric="high", local_hour=14)
    finally:
        _restore(old)
    assert abs(eff - settings.WEATHER_SIGMA_FLOOR_F / 1.8) < 1e-9


def test_station_local_hour_gate():
    # Future date -> None (curve not engaged); the in-progress local day -> a real hour.
    assert station_local_hour("nyc", date.today() + timedelta(days=3)) is None
    today_local = station_local_now("nyc").date()
    h = station_local_hour("nyc", today_local)
    assert isinstance(h, int) and 0 <= h <= 23, h


# --- Observed-anchored center (Fix A: F4) + confidence-scaled gap (Fix B: F3) ----
# Near settlement the distribution's CENTER anchors on observed-so-far + the curve's
# empirical drift, not the stale forecast (so a hot/cold forecast can't be priced
# with high evening confidence on a value that can no longer happen). And the market-
# gap tolerance scales with σ so a sharpened forecast can't sit on the wrong bucket.

def test_pricing_center_anchors_on_observed_plus_drift():
    old = _set_intraday(WEATHER_INTRADAY_SIGMA_ENABLED=True)
    try:
        fc = _fc([85.0] * 31)                       # forecast mean 85 (bias disabled here)
        drift18 = intraday_drift("nyc", "high", 18)
        assert drift18 is not None
        # Evening, the day has already hit 90 -> center anchors on reality, not 85.
        center = fc.pricing_center("high", local_hour=18, observed=90.0)
        assert abs(center - (90.0 + drift18)) < 1e-9, center
        assert abs(center - fc.corrected_mean("high")) > 1.0   # clearly NOT the forecast mean
    finally:
        _restore(old)


def test_pricing_center_without_observation_or_hour_uses_forecast_mean():
    old = _set_intraday(WEATHER_INTRADAY_SIGMA_ENABLED=True)
    try:
        fc = _fc([85.0] * 31)
        # Morning before the gate (no observed value yet) -> forecast mean.
        assert abs(fc.pricing_center("high", local_hour=18, observed=None)
                   - fc.corrected_mean("high")) < 1e-9
        # Future day (no local hour) even WITH an observation -> forecast mean.
        assert abs(fc.pricing_center("high", local_hour=None, observed=90.0)
                   - fc.corrected_mean("high")) < 1e-9
    finally:
        _restore(old)


def test_pricing_center_disabled_uses_forecast_mean():
    old = _set_intraday(WEATHER_INTRADAY_SIGMA_ENABLED=False)
    try:
        fc = _fc([85.0] * 31)
        assert abs(fc.pricing_center("high", local_hour=18, observed=90.0)
                   - fc.corrected_mean("high")) < 1e-9
    finally:
        _restore(old)


def test_gap_threshold_tightens_as_sigma_sharpens():
    """Confidence-scaled gap tolerance: capped at MAX when σ is large (morning),
    tighter toward MIN when σ collapses (evening), never below MIN."""
    old = _set_intraday(WEATHER_INTRADAY_SIGMA_ENABLED=True, WEATHER_INTRADAY_SIGMA_MIN_F=0.3)
    try:
        fc = _fc([85.0] * 31)

        def thr(hour):   # mirrors the clamp in generate_weather_signal (°F -> scale 1)
            s = fc.effective_sigma_for("high", hour)
            return min(settings.WEATHER_MAX_MARKET_GAP_F,
                       max(settings.WEATHER_MIN_MARKET_GAP_F,
                           settings.WEATHER_MARKET_GAP_SIGMA_K * s))

        morning, evening = thr(7), thr(18)
        assert morning == settings.WEATHER_MAX_MARKET_GAP_F    # σ huge -> capped at MAX
        assert evening < morning                               # σ small -> tighter
        assert evening >= settings.WEATHER_MIN_MARKET_GAP_F    # never below MIN
    finally:
        _restore(old)


def test_market_gap_ok_uses_stored_threshold():
    from backend.core.weather_signals import WeatherTradingSignal

    class _M:          # minimal stand-in for a WeatherMarket
        unit = "F"

    assert WeatherTradingSignal(market=_M(), market_gap=1.5,
                                market_gap_threshold=0.6).market_gap_ok is False
    assert WeatherTradingSignal(market=_M(), market_gap=0.4,
                                market_gap_threshold=0.6).market_gap_ok is True
    # No stored threshold -> falls back to the absolute 2.0°F cap.
    assert WeatherTradingSignal(market=_M(), market_gap=1.5,
                                market_gap_threshold=None).market_gap_ok is True


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("All forecast-distribution tests passed.")
