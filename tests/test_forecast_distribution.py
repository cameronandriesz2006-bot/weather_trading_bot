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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("All forecast-distribution tests passed.")
