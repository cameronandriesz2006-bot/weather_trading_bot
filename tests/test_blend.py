"""Multi-model blend tests — network-free.

Run:  PYTHONPATH=. venv/bin/python tests/test_blend.py   (pytest not installed)

Covers the equal-weight pooling math, model-key matching, the is_blend σ-inflation
switch, and that the flag-OFF path is unchanged (GFS-only). The live fetch / end-to-end
blend skill is validated separately by backend/data/blend_validate.py (needs the API).
"""
import statistics
from datetime import date

from backend.config import settings
from backend.data import weather as W


def test_model_key_matching():
    models = ["gfs_seamless", "ecmwf_ifs025", "icon_seamless"]
    assert W._match_blend_model("temperature_2m_max_member03_ncep_gefs_seamless", models) == "gfs_seamless"
    assert W._match_blend_model("temperature_2m_max_ecmwf_ifs025_ensemble", models) == "ecmwf_ifs025"
    assert W._match_blend_model("temperature_2m_min_icon_seamless_eps", models) == "icon_seamless"
    assert W._match_blend_model("time", models) is None


def test_equal_model_weight():
    # 5 members @0 (model A) + 1 member @10 (model B): EQUAL MODEL weight => 5, not 1.67.
    mu, _ = W._mixture_stats([[0, 0, 0, 0, 0], [10]])
    pool = W._equal_weight_pool([[0, 0, 0, 0, 0], [10]])
    assert abs(mu - 5.0) < 1e-9
    assert abs(statistics.mean(pool) - 5.0) < 0.2
    # empty models are skipped, not weighted
    assert W._equal_weight_pool([[], [5, 5, 5]])           # non-empty result
    assert W._mixture_stats([[], []]) == (0.0, 0.0)        # nothing present


def test_mixture_stats():
    mu, sd = W._mixture_stats([[10, 12, 14], [20, 22, 24], [30, 32, 34]])
    assert abs(mu - 22.0) < 1e-9
    # between-model variance (pvariance([12,22,32]) = 66.7) dominates within (2.7)
    assert 8.0 < sd < 8.7


def test_flag_off_is_unchanged():
    assert settings.WEATHER_BLEND_ENABLED is False              # SHIPS OFF
    f = W.EnsembleForecast("nyc", "NYC", date.today(), [80, 82, 84], [60, 62, 64], unit="F")
    assert f.is_blend is False
    assert W._bias_path().name == "station_bias.json"           # GFS bias file while off


def test_is_blend_sigma_inflation_switch():
    # The is_blend flag must route _effective_sigma to WEATHER_BLEND_SIGMA_INFLATION.
    raw = 3.0
    base = W.EnsembleForecast("nyc", "NYC", date.today(), [80, 82, 84], [60, 62, 64], unit="F")
    blend = W.EnsembleForecast("nyc", "NYC", date.today(), [80, 82, 84], [60, 62, 64], unit="F", is_blend=True)
    old_g, old_b = settings.WEATHER_SIGMA_INFLATION, settings.WEATHER_BLEND_SIGMA_INFLATION
    try:
        settings.WEATHER_SIGMA_INFLATION = 1.0
        settings.WEATHER_BLEND_SIGMA_INFLATION = 5.0           # distinct, so we can SEE which is used
        sg = base._effective_sigma(raw)                        # uses 1.0
        sb = blend._effective_sigma(raw)                       # must use 5.0
        assert sb > sg
    finally:
        settings.WEATHER_SIGMA_INFLATION, settings.WEATHER_BLEND_SIGMA_INFLATION = old_g, old_b


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"  ok  {_name}")
    print("All blend tests passed.")
