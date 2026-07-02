"""Weather data fetcher using Open-Meteo Ensemble API and NWS observations."""
import httpx
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List, Optional
import statistics
import time

logger = logging.getLogger("trading_bot")

# City configurations. lat/lon point at the exact station each market SETTLES on
# (Polymarket daily-temperature markets, per their resolution descriptions), not
# the city centre — airport vs downtown can differ by several degrees. The forecast
# must target the settlement station to be comparable to the resolved value.
#
# "unit" is the NATIVE unit the market prices/resolves in: US markets resolve in
# Fahrenheit (whole-degree, 2-degree buckets like "82-83°F"); the international
# markets resolve in Celsius (single-degree buckets like "18°C"). We forecast and
# price each city ENTIRELY in its native unit — no temperature is ever converted,
# so there is no conversion error to make. Polymarket settles from its own market
# outcome (price-based), so the unit never enters settlement either.
#
# "tz" is the station's IANA timezone. Markets settle on the station's LOCAL calendar
# day, so we need the local clock to (a) gate the observed-so-far floor and (b) look up
# the intraday σ curve at the current local hour. zoneinfo is DST-aware (a real fix over
# the longitude approximation, which is ~1h off under summer daylight time); we fall
# back to the longitude estimate if the tz database is unavailable.
# NOTE: Kalshi may settle on different stations; verify separately before enabling it.
CITY_CONFIG: Dict[str, dict] = {
    "nyc": {
        "name": "New York City",
        # Settles on LaGuardia Airport (KLGA), NOT Central Park (KNYC).
        "lat": 40.7792,
        "lon": -73.8800,
        "nws_station": "KLGA",
        "unit": "F",
        "tz": "America/New_York",
    },
    "chicago": {
        "name": "Chicago",
        # Chicago O'Hare Intl (KORD).
        "lat": 41.9950,
        "lon": -87.9336,
        "nws_station": "KORD",
        "unit": "F",
        "tz": "America/Chicago",
    },
    "miami": {
        "name": "Miami",
        # Miami Intl (KMIA).
        "lat": 25.7906,
        "lon": -80.3164,
        "nws_station": "KMIA",
        "unit": "F",
        "tz": "America/New_York",
    },
    "los_angeles": {
        "name": "Los Angeles",
        # Los Angeles Intl (KLAX) — coastal, much cooler than downtown.
        "lat": 33.9381,
        "lon": -118.3889,
        "nws_station": "KLAX",
        "unit": "F",
        "tz": "America/Los_Angeles",
    },
    "denver": {
        "name": "Denver",
        # Settles on Buckley Space Force Base (KBKF) in Aurora, NOT Denver Intl (KDEN).
        "lat": 39.7172,
        "lon": -104.7517,
        "nws_station": "KBKF",
        "unit": "F",
        "tz": "America/Denver",
    },
    "atlanta": {
        "name": "Atlanta",
        # Settles on Hartsfield-Jackson Intl (KATL) per the market resolution text
        # (Wunderground KATL). Added 2026-06-30 — OOS-confirmed H=16 edge (see edge2_oos_backtest).
        "lat": 33.6407,
        "lon": -84.4277,
        "nws_station": "KATL",
        "unit": "F",
        "tz": "America/New_York",
    },
    # --- International cities (resolve in °C; high-liquidity books) --------------
    # Coordinates are the exact settlement station named in each market's
    # Polymarket resolution description. Open-Meteo snaps to its nearest grid cell,
    # and timezone=auto aggregates the high/low over the station's LOCAL day.
    "london": {
        "name": "London",
        # London City Airport (EGLC) — Wunderground.
        "lat": 51.5048,
        "lon": 0.0495,
        "nws_station": None,
        "unit": "C",
        "tz": "Europe/London",
    },
    "tokyo": {
        "name": "Tokyo",
        # Tokyo Haneda Airport (RJTT) — Wunderground.
        "lat": 35.5523,
        "lon": 139.7816,
        "nws_station": None,
        "unit": "C",
        "tz": "Asia/Tokyo",
    },
    "seoul": {
        "name": "Seoul",
        # Seoul market settles on Incheon Intl Airport (RKSI), NOT central Seoul.
        "lat": 37.4692,
        "lon": 126.4505,
        "nws_station": None,
        "unit": "C",
        "tz": "Asia/Seoul",
    },
    "paris": {
        "name": "Paris",
        # Paris-Le Bourget Airport (LFPB) — Wunderground.
        "lat": 48.9694,
        "lon": 2.4414,
        "nws_station": None,
        "unit": "C",
        "tz": "Europe/Paris",
    },
    "shanghai": {
        "name": "Shanghai",
        # Shanghai Pudong Intl Airport (ZSPD) — Wunderground.
        "lat": 31.1443,
        "lon": 121.8083,
        "nws_station": None,
        "unit": "C",
        "tz": "Asia/Shanghai",
    },
    "hong_kong": {
        "name": "Hong Kong",
        # Hong Kong Observatory HQ (Tsim Sha Tsui) — resolves on the HKO
        # "Absolute Daily Max (deg. C)", NOT an airport.
        "lat": 22.3019,
        "lon": 114.1742,
        "nws_station": None,
        "unit": "C",
        "tz": "Asia/Hong_Kong",
    },
}


# Meteostat station id per city = the realized-observation source for the market's
# settlement station (resolved via Meteostat's nearby endpoint, matched to the
# named ICAO). None = no usable obs station near the settlement point (Shanghai/
# Pudong's nearest is ~35km). Shared by bias_backfill.py and the observed-high
# floor below — keep it the single source of truth.
METEOSTAT_STATION: Dict[str, Optional[str]] = {
    "nyc": "72503",          # KLGA LaGuardia
    "chicago": "72530",      # KORD O'Hare
    "miami": "72202",        # KMIA
    "los_angeles": "72295",  # KLAX
    "denver": "KBKF0",       # Buckley SFB
    "atlanta": "72219",      # KATL Hartsfield-Jackson
    "london": "EGLC0",       # London City
    "tokyo": "47671",        # Haneda
    "seoul": "47113",        # Incheon
    "paris": "07150",        # Le Bourget
    "shanghai": None,        # no station within ~35km of ZSPD Pudong
    "hong_kong": "45007",    # HKO HQ
}


# --- Per-station bias correction (see backend/data/bias_backfill.py) ----------
# station_bias.json holds bias_f = mean(forecast - actual) per station+metric.
# We SUBTRACT it from the forecast mean before pricing buckets.
_BIAS_FILE = Path(__file__).with_name("station_bias.json")
# When the multi-model blend is enabled, the GFS-fitted biases would MIS-correct it
# (each model has its own offset), so the blend reads its OWN re-fit table. Produced by
# `python -m backend.data.bias_backfill --blend`.
_BIAS_BLEND_FILE = Path(__file__).with_name("station_bias_blend.json")
_bias_cache: Optional[Dict[str, dict]] = None


def _bias_path() -> Path:
    """The station-bias file to read: the blend's re-fit table when the blend is on,
    else the GFS table. The flag is fixed at startup, so the cache below stays valid."""
    from backend.config import settings
    return _BIAS_BLEND_FILE if settings.WEATHER_BLEND_ENABLED else _BIAS_FILE


def _load_bias() -> Dict[str, dict]:
    """Load (and memoise) the station-bias table; {} if absent/unreadable."""
    global _bias_cache
    if _bias_cache is None:
        try:
            _bias_cache = json.loads(_bias_path().read_text()).get("stations", {}) or {}
        except Exception:
            _bias_cache = {}
    return _bias_cache


def reload_station_bias() -> Dict[str, dict]:
    """Drop the cache so a freshly-written station_bias.json is picked up."""
    global _bias_cache
    _bias_cache = None
    return _load_bias()


def get_station_bias(city_key: str, metric: str) -> float:
    """
    Signed correction to SUBTRACT from the model mean for this station, in the
    city's NATIVE unit (°F for US, °C for international). Positive => the model
    runs warm. Returns 0.0 when disabled, missing, or under-sampled, and is
    clamped to +/- WEATHER_BIAS_MAX_SHIFT_F (scaled 1/1.8 for °C, since the cap is
    defined in °F and a bias is a temperature *spread*).
    """
    from backend.config import settings
    if not settings.WEATHER_BIAS_ENABLED:
        return 0.0
    entry = _load_bias().get(city_key, {}).get(metric)
    if not entry or entry.get("samples", 0) < settings.WEATHER_BIAS_MIN_SAMPLES:
        return 0.0
    bias = float(entry.get("bias_f", 0.0))
    unit = CITY_CONFIG.get(city_key, {}).get("unit", "F")
    cap = settings.WEATHER_BIAS_MAX_SHIFT_F * ((1.0 / 1.8) if unit == "C" else 1.0)
    return max(-cap, min(cap, bias))


def is_bias_corrected(city_key: str, metric: str) -> bool:
    """True iff a per-station bias was actually APPLIED for this city+metric.

    Mirrors the gate in get_station_bias: bias enabled, an entry exists, and it
    cleared the min-samples bar. Cities whose bias was SKIPPED (coastal source-
    inconsistency or no nearby station — currently LA, Tokyo, Seoul, Hong Kong,
    Shanghai) return False. Used to tag each trade so the scoreboard can compare
    bias-corrected vs uncorrected cohorts; recorded at trade time so a later
    backfill that corrects a city doesn't retroactively relabel old trades.
    """
    from backend.config import settings
    if not settings.WEATHER_BIAS_ENABLED:
        return False
    entry = _load_bias().get(city_key, {}).get(metric)
    return bool(entry and entry.get("samples", 0) >= settings.WEATHER_BIAS_MIN_SAMPLES)


# --- Intraday σ schedule (see backend/data/intraday_backtest.py) --------------
# intraday_curve.json holds, per city -> metric (high/low) -> LOCAL hour, the
# {mean, std, n} of (final daily extreme − extreme so far) from 10y of station obs.
# The `std` is the empirical RESIDUAL uncertainty at that hour, in the city's native
# unit — exactly the σ the forecast should carry on the in-progress local day
# (wide in the morning, near-zero in the evening). Replaces the flat σ-floor there.
_INTRADAY_FILE = Path(__file__).with_name("intraday_curve.json")
_intraday_cache: Optional[Dict[str, dict]] = None


def _load_intraday_curve() -> Dict[str, dict]:
    """Load (and memoise) the intraday σ curve; {} if absent/unreadable."""
    global _intraday_cache
    if _intraday_cache is None:
        try:
            _intraday_cache = json.loads(_INTRADAY_FILE.read_text()).get("cities", {}) or {}
        except Exception:
            _intraday_cache = {}
    return _intraday_cache


def reload_intraday_curve() -> Dict[str, dict]:
    """Drop the cache so a freshly-rebuilt intraday_curve.json is picked up."""
    global _intraday_cache
    _intraday_cache = None
    return _load_intraday_curve()


def intraday_sigma(city_key: str, metric: str, local_hour: int) -> Optional[float]:
    """Empirical residual σ (native unit) of the still-to-come move in the daily
    ``metric`` extreme at this station-LOCAL ``hour``, from intraday_curve.json.

    Returns None when the city/metric/hour is absent (e.g. Shanghai has no curve, or
    an hour with too few samples) so the caller falls back to the flat-floor σ. The
    value is already in the city's native unit (no scaling) — it's a measured spread.
    """
    metric_curve = _load_intraday_curve().get(city_key, {}).get(metric)
    if not isinstance(metric_curve, dict):
        return None
    entry = metric_curve.get(str(local_hour))
    if not entry:
        return None
    std = entry.get("std")
    return float(std) if std is not None else None


def intraday_drift(city_key: str, metric: str, local_hour: int) -> Optional[float]:
    """Empirical MEAN remaining move (native unit) in the daily ``metric`` extreme at
    this station-LOCAL ``hour`` — how much further the high is still expected to rise
    (>= 0) or the low to fall (<= 0), on average, from what's been seen so far. The
    companion to ``intraday_sigma``, used to ANCHOR the in-progress-day center on
    observed-so-far + this drift. None when absent (Shanghai / missing hour)."""
    metric_curve = _load_intraday_curve().get(city_key, {}).get(metric)
    if not isinstance(metric_curve, dict):
        return None
    entry = metric_curve.get(str(local_hour))
    if not entry:
        return None
    mean = entry.get("mean")
    return float(mean) if mean is not None else None


@dataclass
class EnsembleForecast:
    """Ensemble weather forecast with per-member data."""
    city_key: str
    city_name: str
    target_date: date
    member_highs: List[float]  # Daily max temps per member, in `unit`
    member_lows: List[float]   # Daily min temps per member, in `unit`
    unit: str = "F"            # native unit of every temperature here ("F" or "C")
    mean_high: float = 0.0
    std_high: float = 0.0
    mean_low: float = 0.0
    std_low: float = 0.0
    num_members: int = 0
    is_blend: bool = False   # True when built from the multi-model (GFS+ECMWF+ICON) blend
    fetched_at: datetime = field(default_factory=datetime.utcnow)

    def __post_init__(self):
        if self.member_highs:
            self.mean_high = statistics.mean(self.member_highs)
            self.std_high = statistics.stdev(self.member_highs) if len(self.member_highs) > 1 else 0.0
            self.num_members = len(self.member_highs)
        if self.member_lows:
            self.mean_low = statistics.mean(self.member_lows)
            self.std_low = statistics.stdev(self.member_lows) if len(self.member_lows) > 1 else 0.0

    def probability_high_above(self, threshold_f: float) -> float:
        """Fraction of ensemble members with daily high above threshold."""
        if not self.member_highs:
            return 0.5
        count = sum(1 for h in self.member_highs if h > threshold_f)
        return count / len(self.member_highs)

    def probability_high_below(self, threshold_f: float) -> float:
        """Fraction of ensemble members with daily high below threshold."""
        return 1.0 - self.probability_high_above(threshold_f)

    def probability_low_above(self, threshold_f: float) -> float:
        """Fraction of ensemble members with daily low above threshold."""
        if not self.member_lows:
            return 0.5
        count = sum(1 for l in self.member_lows if l > threshold_f)
        return count / len(self.member_lows)

    def probability_low_below(self, threshold_f: float) -> float:
        """Fraction of ensemble members with daily low below threshold."""
        return 1.0 - self.probability_low_above(threshold_f)

    @staticmethod
    def _fraction_in_range(members: List[float], low_f: Optional[float], high_f: Optional[float]) -> float:
        """
        Raw fraction of members whose value rounds into the integer bucket [low_f, high_f].

        Settlement rounds the observed temperature to the nearest degree before
        bucketing, so a bucket "82-83" covers raw values in [81.5, 83.5). Open
        bounds (None) extend the range to -/+ infinity. Kept as a reference; the
        traded probability uses the fitted distribution below.
        """
        if not members:
            return 0.5
        lo = (low_f - 0.5) if low_f is not None else float("-inf")
        hi = (high_f + 0.5) if high_f is not None else float("inf")
        count = sum(1 for m in members if lo <= m < hi)
        return count / len(members)

    @staticmethod
    def _normal_cdf(x: float, mean: float, sigma: float) -> float:
        """Standard Normal CDF at x for N(mean, sigma)."""
        if sigma <= 0:
            return 1.0 if x >= mean else 0.0
        return 0.5 * (1.0 + math.erf((x - mean) / (sigma * math.sqrt(2.0))))

    def _effective_sigma(self, raw_sigma: float, metric: Optional[str] = None,
                         local_hour: Optional[int] = None) -> float:
        """
        Widen the (under-dispersed) ensemble spread into an honest forecast sigma.

        Default (future days, or intraday disabled/missing):
            sigma_eff = max(raw_sigma * INFLATION, FLOOR) + lead_days * PER_LEAD_DAY

        Intraday σ schedule (Phase 5): on the IN-PROGRESS local day, the empirical
        residual-uncertainty curve IS the truth, so when a ``local_hour`` is supplied
        (the caller passes one only on the station-local today) and the curve has a
        value for this city/metric/hour, we use it directly:
            sigma_eff = max(WEATHER_INTRADAY_SIGMA_MIN, intraday_std)
        and SKIP the flat-floor/inflation/lead terms (the lead term is 0 today anyway;
        the floor would only make us wrongly UNSURE in the evening — the bug this fixes).
        The _MIN rail is a hard floor so σ can never collapse to ~0 and turn a tiny
        edge into an enormous Kelly bet.

        The FLOOR / PER_LEAD_DAY / _MIN config constants are expressed in °F. For a °C
        market every temperature here (raw_sigma and the curve std included) is already
        in °C, so we scale those constants by 1/1.8 — the exact conversion for a
        temperature *spread* (a difference, no +32 offset). INFLATION is unitless. The
        curve std is stored in native unit, so it is NOT scaled.
        """
        from backend.config import settings
        scale = (1.0 / 1.8) if self.unit == "C" else 1.0

        if (settings.WEATHER_INTRADAY_SIGMA_ENABLED and metric is not None
                and local_hour is not None):
            curve_std = intraday_sigma(self.city_key, metric, local_hour)
            if curve_std is not None:
                sigma_min = settings.WEATHER_INTRADAY_SIGMA_MIN_F * scale
                return max(sigma_min, curve_std)

        floor = settings.WEATHER_SIGMA_FLOOR_F * scale
        per_lead_day = settings.WEATHER_SIGMA_PER_LEAD_DAY_F * scale
        # The blend's ensemble is naturally better-dispersed, so it gets its own (lower,
        # once validated) inflation factor; the GFS path is unchanged.
        inflation = (settings.WEATHER_BLEND_SIGMA_INFLATION if self.is_blend
                     else settings.WEATHER_SIGMA_INFLATION)
        base = max(raw_sigma * inflation, floor)
        lead_days = max(0, (self.target_date - date.today()).days)
        return base + lead_days * per_lead_day

    def _clamped_cdf(self, x: float, mean: float, sigma: float,
                     floor: Optional[float], ceiling: Optional[float]) -> float:
        """Normal CDF, censored at the observed-so-far hard bound.

        floor   (highs): the final high can't end below it -> P(final <= x) = 0 below it.
        ceiling (lows):  the final low can't end above it  -> P(final <= x) = 1 at/above it.
        (final_high = max(floor, X); final_low = min(ceiling, X).)
        """
        if floor is not None and x < floor:
            return 0.0
        if ceiling is not None and x >= ceiling:
            return 1.0
        return self._normal_cdf(x, mean, sigma)

    def _fitted_bucket_prob(self, mean: float, raw_sigma: float,
                            low_f: Optional[float], high_f: Optional[float],
                            floor: Optional[float] = None,
                            ceiling: Optional[float] = None,
                            metric: Optional[str] = None,
                            local_hour: Optional[int] = None) -> float:
        """
        P(temperature in bucket) under a fitted, widened Normal, optionally censored
        at the observed-so-far hard bound (``floor`` for highs / ``ceiling`` for lows).

        Integrates N(mean, sigma_eff) over the bucket's rounding interval
        [low-0.5, high+0.5); open bounds extend to -/+ infinity. With a floor/ceiling,
        buckets that are now physically impossible (entirely past the observed extreme)
        correctly collapse to ~0 and the surviving mass piles at the observed value.

        ``metric``/``local_hour`` feed the intraday σ schedule (see _effective_sigma):
        on the in-progress local day the curve sets the width; the floor/ceiling above
        still bounds the locked side, and the two compose cleanly.
        """
        sigma = self._effective_sigma(raw_sigma, metric=metric, local_hour=local_hour)
        lo = (low_f - 0.5) if low_f is not None else None
        hi = (high_f + 0.5) if high_f is not None else None
        p_lo = self._clamped_cdf(lo, mean, sigma, floor, ceiling) if lo is not None else 0.0
        p_hi = self._clamped_cdf(hi, mean, sigma, floor, ceiling) if hi is not None else 1.0
        return max(0.0, p_hi - p_lo)

    def corrected_mean(self, metric: str) -> float:
        """Forecast mean with the per-station bias removed (the mean we price on)."""
        raw = self.mean_high if metric == "high" else self.mean_low
        return raw - get_station_bias(self.city_key, metric)

    def effective_sigma_for(self, metric: str, local_hour: Optional[int] = None) -> float:
        """Public accessor for the effective forecast sigma for ``metric`` at a given
        station-local hour (the same sigma the bucket integral uses). Lets callers —
        e.g. the market-gap guardrail — scale their tolerance to our confidence."""
        raw = self.std_high if metric == "high" else self.std_low
        return self._effective_sigma(raw, metric=metric, local_hour=local_hour)

    def pricing_center(self, metric: str, local_hour: Optional[int] = None,
                       observed: Optional[float] = None) -> float:
        """The CENTER the distribution is priced on.

        Off the in-progress day (or when an observation / curve is missing) this is the
        bias-corrected forecast mean. On the in-progress LOCAL day, once the day's
        extreme has been observed so far, we ANCHOR on reality instead of the stale
        forecast: center = observed-so-far + the empirical remaining drift (the curve
        MEAN at this hour). The observed bound alone only stops the final extreme
        finishing past it on ONE side; without this, a forecast that ran hot (or cold)
        would be priced with high evening confidence on a value that can no longer
        happen. Anchoring fixes both sides; the market-gap guardrail still vetoes if
        the resulting center is far from the market."""
        from backend.config import settings
        base = self.corrected_mean(metric)
        if (settings.WEATHER_INTRADAY_SIGMA_ENABLED and local_hour is not None
                and observed is not None):
            drift = intraday_drift(self.city_key, metric, local_hour)
            if drift is not None:
                return observed + drift
        return base

    def probability_high_in_range(self, low_f: Optional[float], high_f: Optional[float],
                                  floor: Optional[float] = None,
                                  local_hour: Optional[int] = None) -> float:
        """Probability the daily HIGH falls in the bucket (fitted, widened, bias-corrected).
        ``floor`` = observed max so far today: the final high can't end below it.
        ``local_hour`` (station-local, in-progress day only) engages the intraday σ curve."""
        if not self.member_highs:
            return 0.5
        center = self.pricing_center("high", local_hour=local_hour, observed=floor)
        return self._fitted_bucket_prob(center, self.std_high,
                                        low_f, high_f, floor=floor,
                                        metric="high", local_hour=local_hour)

    def probability_low_in_range(self, low_f: Optional[float], high_f: Optional[float],
                                 ceiling: Optional[float] = None,
                                 local_hour: Optional[int] = None) -> float:
        """Probability the daily LOW falls in the bucket (fitted, widened, bias-corrected).
        ``ceiling`` = observed min so far today: the final low can't end above it.
        ``local_hour`` (station-local, in-progress day only) engages the intraday σ curve."""
        if not self.member_lows:
            return 0.5
        center = self.pricing_center("low", local_hour=local_hour, observed=ceiling)
        return self._fitted_bucket_prob(center, self.std_low,
                                        low_f, high_f, ceiling=ceiling,
                                        metric="low", local_hour=local_hour)

    @property
    def ensemble_agreement(self) -> float:
        """How one-sided the ensemble is (0.5 = split, 1.0 = unanimous)."""
        if not self.member_highs:
            return 0.5
        median = statistics.median(self.member_highs)
        above = sum(1 for h in self.member_highs if h > median)
        frac = above / len(self.member_highs)
        return max(frac, 1 - frac)


# Simple cache: (city_key, target_date_str) -> (timestamp, EnsembleForecast-or-None)
# Failures are cached too, briefly, so a single API error (e.g. a 429 rate-limit)
# does NOT cascade into one re-fetch per bucket (~280/scan) that hammers the API
# and never recovers. Successful forecasts are held for _CACHE_TTL; failures only
# for _FAIL_TTL, so a recovered API is retried within the minute.
_forecast_cache: Dict[str, tuple] = {}
# Open-Meteo's free tier is ~10,000 requests/DAY (per IP). Daily cost is roughly
# ~33 unique (city, date) forecasts * (1440 / cache_minutes) * models_per_call.
# The BLEND fetches 3 models per call (gfs+ecmwf+icon) — ~3x the per-call cost of the
# old GFS-only path. So the 30-min cache that was a comfy ~1,600/day (6x margin) became
# ~4,800/day under the blend, and a day of blend validation + restarts blew past 10k and
# exhausted the quota until the next UTC reset (the 2026-06-22 lockout). Bumping the cache
# 30 -> 90 min cancels the blend's 3x: 33 * 16 * 3 ~= 1,600/day again (~6x margin restored).
_CACHE_TTL = 5400      # 90 minutes (successful forecast). Daily high/low barely move in
                       # 90 min; Open-Meteo refreshes the ensembles only every ~6h, and the
                       # observed-floor adds intraday freshness near settlement.
# When a fetch FAILS (e.g. the daily limit is hit), back off for a while instead
# of retrying every scan — that both saves pointless load and avoids keeping a
# rolling-window limit pegged. Recovery is still noticed within this interval.
_FAIL_TTL = 600        # 10 minutes (negative cache / backoff for a failed fetch)


def _celsius_to_fahrenheit(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


# --- Multi-model blend helpers (config.WEATHER_BLEND_ENABLED; built 2026-06-21) -------
# ensemble-api suffixes each model's member keys distinctively, e.g.
#   temperature_2m_max_member03_ncep_gefs_seamless / _ecmwf_ifs025_ensemble / _icon_seamless_eps
# Map each configured model id to a distinctive substring of its response keys.
_BLEND_MODEL_SUFFIX = {
    "gfs_seamless": "gefs_seamless",
    "ecmwf_ifs025": "ecmwf_ifs025",
    "icon_seamless": "icon_seamless",
}


def _match_blend_model(key: str, models: List[str]) -> Optional[str]:
    """Which configured model a response key belongs to (by distinctive substring)."""
    for m in models:
        if _BLEND_MODEL_SUFFIX.get(m, m) in key:
            return m
    return None


def _quantile_sorted(s: List[float], q: float) -> float:
    """Linear-interpolated quantile of an ALREADY-SORTED list."""
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(s) - 1)
    return s[lo] * (1 - (pos - lo)) + s[hi] * (pos - lo)


def _equal_weight_pool(per_model_members: List[List[float]], k: int = 64) -> List[float]:
    """Pool several models' member lists with EQUAL MODEL WEIGHT by resampling each to k
    quantile points (so a model with more members can't dominate). Empty models skipped.
    The result is a representative member sample of the blended distribution (it feeds the
    secondary member-fraction/agreement helpers; the exact pricing mean/std come from
    _mixture_stats)."""
    pool: List[float] = []
    for members in per_model_members:
        if not members:
            continue
        s = sorted(members)
        if len(s) == 1:
            pool.extend(s * k)
        else:
            pool.extend(_quantile_sorted(s, (i + 0.5) / k) for i in range(k))
    return pool


def _mixture_stats(per_model_members: List[List[float]]) -> tuple:
    """Exact EQUAL-WEIGHT mixture (mean, std): mean of the per-model means, and
    sqrt(mean within-model variance + variance of the means) — the law of total variance
    with equal weights. This is the blend's pricing mean/std."""
    present = [m for m in per_model_members if m]
    if not present:
        return 0.0, 0.0
    means = [statistics.mean(m) for m in present]
    within = [statistics.pvariance(m) if len(m) > 1 else 0.0 for m in present]
    mu = statistics.mean(means)
    between = statistics.pvariance(means) if len(means) > 1 else 0.0
    return mu, math.sqrt(statistics.mean(within) + between)


async def fetch_ensemble_forecast(city_key: str, target_date: Optional[date] = None,
                                  cache_only: bool = False) -> Optional[EnsembleForecast]:
    """
    Fetch ensemble forecast from Open-Meteo Ensemble API (free, 31-member GFS).

    Returns per-member daily max/min temperatures in the city's NATIVE unit
    (Fahrenheit for US markets, Celsius for the international ones) — matching the
    unit the market resolves in, so no conversion is ever needed downstream.

    cache_only=True returns the cached forecast (or None) WITHOUT any network call —
    for read-only paths like the dashboard that must never block on the API.
    """
    if city_key not in CITY_CONFIG:
        logger.warning(f"Unknown city key: {city_key}")
        return None

    if target_date is None:
        target_date = date.today()

    cache_key = f"{city_key}_{target_date.isoformat()}"
    now = time.time()
    if cache_key in _forecast_cache:
        cached_time, cached_forecast = _forecast_cache[cache_key]
        # Held longer when it succeeded, only briefly when it failed (negative cache).
        ttl = _CACHE_TTL if cached_forecast is not None else _FAIL_TTL
        if now - cached_time < ttl:
            return cached_forecast

    if cache_only:
        return None  # no fresh cache entry and caller forbids a network fetch

    city = CITY_CONFIG[city_key]
    unit = city.get("unit", "F")
    api_temp_unit = "celsius" if unit == "C" else "fahrenheit"

    from backend.config import settings   # local import (module convention; avoids cycles)
    blend_on = settings.WEATHER_BLEND_ENABLED
    blend_models = [m.strip() for m in settings.WEATHER_BLEND_MODELS.split(",") if m.strip()]

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Open-Meteo Ensemble API — GFS ensemble (31 members) by default, or the
            # equal-weight GFS+ECMWF+ICON blend when WEATHER_BLEND_ENABLED.
            params = {
                "latitude": city["lat"],
                "longitude": city["lon"],
                "daily": "temperature_2m_max,temperature_2m_min",
                "temperature_unit": api_temp_unit,
                "start_date": target_date.isoformat(),
                "end_date": target_date.isoformat(),
                "models": ",".join(blend_models) if blend_on else "gfs_seamless",
                # Aggregate the daily high/low over the station's LOCAL day, not a
                # UTC day — markets settle on the local calendar day.
                "timezone": "auto",
            }

            response = await client.get(
                "https://ensemble-api.open-meteo.com/v1/ensemble",
                params=params,
            )
            response.raise_for_status()
            data = response.json()

            daily = data.get("daily", {})

            highs_by_model: List[List[float]] = []
            lows_by_model: List[List[float]] = []
            if blend_on:
                # Group members by model, then pool with EQUAL MODEL WEIGHT.
                ph = {m: [] for m in blend_models}
                pl = {m: [] for m in blend_models}
                for key, values in daily.items():
                    if not isinstance(values, list) or not values:
                        continue
                    val = values[0]
                    if val is None:
                        continue
                    m = _match_blend_model(key, blend_models)
                    if m is None:
                        continue
                    if "temperature_2m_max" in key:
                        ph[m].append(float(val))
                    elif "temperature_2m_min" in key:
                        pl[m].append(float(val))
                highs_by_model = [ph[m] for m in blend_models]
                lows_by_model = [pl[m] for m in blend_models]
                member_highs = _equal_weight_pool(highs_by_model)
                member_lows = _equal_weight_pool(lows_by_model)
            else:
                # Open-Meteo returns each ensemble member as a separate key:
                #   temperature_2m_max (control), temperature_2m_max_member01, ..., _member30
                # Collect all member values for highs and lows
                member_highs = []
                member_lows = []
                for key, values in daily.items():
                    if not isinstance(values, list) or not values:
                        continue
                    val = values[0]
                    if val is None:
                        continue
                    if "temperature_2m_max" in key:
                        member_highs.append(float(val))
                    elif "temperature_2m_min" in key:
                        member_lows.append(float(val))

            if not member_highs:
                logger.warning(f"No ensemble data for {city_key} on {target_date}")
                _forecast_cache[cache_key] = (now, None)   # negative cache
                return None

            forecast = EnsembleForecast(
                city_key=city_key,
                city_name=city["name"],
                target_date=target_date,
                member_highs=member_highs,
                member_lows=member_lows,
                unit=unit,
                is_blend=blend_on,
            )
            if blend_on:
                # Price on the EXACT equal-weight mixture mean/std (the pooled member list
                # is a resampled approximation; these are exact).
                forecast.mean_high, forecast.std_high = _mixture_stats(highs_by_model)
                forecast.mean_low, forecast.std_low = _mixture_stats(lows_by_model)

            _forecast_cache[cache_key] = (now, forecast)
            logger.info(f"{'Blend' if blend_on else 'Ensemble'} forecast for {city['name']} on "
                        f"{target_date}: High {forecast.mean_high:.1f}{unit} +/- "
                        f"{forecast.std_high:.1f}{unit} ({forecast.num_members} members)")

            return forecast

    except Exception as e:
        logger.warning(f"Failed to fetch ensemble forecast for {city_key}: {e}")
        _forecast_cache[cache_key] = (now, None)   # negative cache: don't re-hammer
        return None


async def fetch_nws_observed_temperature(city_key: str, target_date: Optional[date] = None) -> Optional[Dict[str, float]]:
    """
    Fetch observed temperature from NWS API for settlement.
    Returns dict with 'high' and 'low' in Fahrenheit, or None if not available.
    """
    if city_key not in CITY_CONFIG:
        return None

    city = CITY_CONFIG[city_key]
    if target_date is None:
        target_date = date.today()

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # NWS observations endpoint
            station = city["nws_station"]
            url = f"https://api.weather.gov/stations/{station}/observations"
            headers = {"User-Agent": "(trading-bot, contact@example.com)"}

            # Get observations for the target date
            start = datetime.combine(target_date, datetime.min.time()).isoformat() + "Z"
            end = datetime.combine(target_date + timedelta(days=1), datetime.min.time()).isoformat() + "Z"

            response = await client.get(url, params={"start": start, "end": end}, headers=headers)
            response.raise_for_status()
            data = response.json()

            features = data.get("features", [])
            if not features:
                return None

            temps = []
            for obs in features:
                props = obs.get("properties", {})
                temp_c = props.get("temperature", {}).get("value")
                if temp_c is not None:
                    temps.append(_celsius_to_fahrenheit(temp_c))

            if not temps:
                return None

            return {
                "high": max(temps),
                "low": min(temps),
            }

    except Exception as e:
        logger.warning(f"Failed to fetch NWS observations for {city_key}: {e}")
        return None


# --- Observed-so-far floor/ceiling (intraday hard bound) ----------------------
# The day's final HIGH cannot finish below the max already recorded today, nor the
# final LOW above the min already recorded. That's a fact, not a forecast — so we
# censor the forecast distribution at it (see EnsembleForecast._fitted_bucket_prob).
# Near settlement this collapses our (otherwise too-wide) uncertainty to reality.
_METEOSTAT_DAILY_URL = "https://d.meteostat.net/app/proxy/stations/daily"
_METEOSTAT_HOURLY_URL = "https://d.meteostat.net/app/proxy/stations/hourly"
_METEOSTAT_HEADERS = {"User-Agent": "Mozilla/5.0"}  # the proxy 403s a bare client
_NWS_OBS_URL = "https://api.weather.gov/stations/{station}/observations"
_NWS_HEADERS = {"User-Agent": "(weather-trading-bot simulation, contact@example.com)"}
_observed_cache: Dict[str, tuple] = {}   # key -> (timestamp, value-or-None)
_OBSERVED_TTL = 300  # 5 min — the NWS feed updates ~5-20 min; a 15-min TTL could feed
                     # a whole scan cycle data that is one full scan stale


def _wu_round_f(temp_c: float) -> float:
    """°C ob -> the integer °F Wunderground displays for it (round half UP).
    The markets settle on 'the highest temperature recorded' as shown by
    Wunderground, which is the max over PER-OB integer °F values — so the
    running extreme must round each ob BEFORE taking max/min (KBKF 2026-07-01:
    continuous max 89.6°F, settled bucket 90-91 because 89.6 displays as 90)."""
    return float(math.floor(temp_c * 9.0 / 5.0 + 32.0 + 0.5))


async def _nws_observed_extreme(
    client: httpx.AsyncClient, station: str, tz_name: str, target_date: date, metric: str
) -> Optional[float]:
    """Observed extreme SO FAR on ``target_date`` (station-local) from the NWS
    station feed — the settlement-grade source. This is the same METAR/5-min
    ASOS data Wunderground resolves on, published within ~5-20 min (KORD/KATL
    report 5-min obs; KBKF hourly), vs Meteostat's hourly proxy which serves
    lagged/model-interpolated values intraday and revises them hours later
    (2026-07-01: it fed a floor 1.3-3.4°F below reality in all 3 cities).
    Values are per-ob Wunderground-rounded integer °F, so the returned bound
    IS the number the market settles against. None if no usable obs (caller
    falls back to Meteostat). NWS keeps ~7 days, so finished-day reads within
    a week also resolve here."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(tz_name)
    day_start = datetime(target_date.year, target_date.month, target_date.day, tzinfo=tz)
    r = await client.get(
        _NWS_OBS_URL.format(station=station),
        params={
            "start": day_start.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": (day_start + timedelta(days=1)).astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": 500,
        },
        headers=_NWS_HEADERS,
    )
    r.raise_for_status()
    temps_f = []
    for obs in r.json().get("features", []) or []:
        props = obs.get("properties", {})
        temp_c = (props.get("temperature") or {}).get("value")
        ts = props.get("timestamp")
        if temp_c is None or not ts:
            continue
        try:
            local_dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(tz)
        except ValueError:
            continue
        if local_dt.date() == target_date:  # UTC window over-fetches; keep the local day only
            temps_f.append(_wu_round_f(float(temp_c)))
    if not temps_f:
        return None
    return max(temps_f) if metric == "high" else min(temps_f)

# Only TRUST the observed-so-far bound once the relevant extreme has typically
# occurred in the station's local day: the daily HIGH is reached mid-afternoon,
# the daily LOW near dawn. Before then, "so far" is uninformative AND a reading
# that already exceeds the forecast is more likely a data artifact than a real
# bound — applying it then would make the bot over-confident in the morning (it
# would bet hard against the market off an overnight reading). After these hours
# the extreme is essentially locked, which is exactly when we want the bound.
_HIGH_SET_LOCAL_HOUR = 16   # ~4pm local
_LOW_SET_LOCAL_HOUR = 10    # ~10am local


def _approx_local_now(lon: float) -> datetime:
    """Rough station-local clock from longitude (15° per hour). Fallback for when the
    tz database is unavailable; ~1h off under summer daylight time (good enough for a
    coarse 'is the extreme likely set yet' gate, but the intraday curve is keyed by
    the exact local hour, so prefer station_local_now)."""
    return datetime.utcnow() + timedelta(hours=lon / 15.0)


def station_local_now(city_key: str) -> datetime:
    """Naive station-local datetime. Uses the city's IANA tz (DST-aware) when the
    tz database resolves, else falls back to the longitude approximation. Returned
    naive (no tzinfo) so date()/hour compare cleanly with our other naive dates."""
    cfg = CITY_CONFIG.get(city_key, {})
    tzname = cfg.get("tz")
    if tzname:
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(tzname)).replace(tzinfo=None)
        except Exception:
            pass
    return _approx_local_now(cfg.get("lon", 0.0))


def station_local_hour(city_key: str, target_date: date) -> Optional[int]:
    """Station-local hour (0-23) IF ``target_date`` is the in-progress local day at the
    station, else None. The intraday σ curve only describes a day still unfolding, so
    on past/future days the caller keeps the flat-floor σ."""
    now = station_local_now(city_key)
    return now.hour if target_date == now.date() else None


def is_day_ahead(city_key: str, target_date: date) -> bool:
    """True if ``target_date`` is a FUTURE local day at the station (day-ahead or later) —
    i.e. the day's high/low has not begun forming yet. Same-day / in-progress (or any past
    date) returns False.

    Used to route execution: day-ahead markets have thin instantaneous books and a genuinely
    unresolved outcome, so we REST limit orders there (maker); same-day markets have deep
    books and a resting order would suffer the worst adverse selection (it fills just as the
    result resolves), so we TAKE those at the ask. Uses the same station-local clock as the
    observed-floor / intraday-σ logic so all three agree on when 'today' is."""
    return target_date > station_local_now(city_key).date()


async def fetch_observed_extreme(
    city_key: str, metric: str, target_date: Optional[date] = None
) -> Optional[float]:
    """Observed daily HIGH (max) / LOW (min) SO FAR at the settlement station on
    ``target_date``, in the city's NATIVE unit — a hard bound on the final settled
    value. Returns None when it can't be trusted (no obs station, future date, or
    fetch failure) so callers fall back to the uncensored forecast.

    Source priority:
      1. NWS station feed (US cities with ``nws_station``) — settlement-grade: the
         same METAR/ASOS obs Wunderground resolves on, per-ob rounded to integer °F
         exactly like the resolution source, ~5-20 min behind real time. Works for
         both the in-progress day and finished days back ~1 week.
      2. Meteostat fallback (°C cities / NWS outage): hourly running-extreme on the
         in-progress day, daily aggregate on finished days.

    CAUTION (learned 2026-07-01): a lagged floor is NOT harmless here. It is safe
    for the censoring bound (a weak bound just censors less), but the same value
    feeds the observed-anchored nowcast CENTER with intraday σ of 0.2-0.4°F — there
    a stale/low reading is a confidently WRONG price, not a weak one. Meteostat's
    intraday hourly serves lagged, later-revised values (Jul 1: 1.3-3.4°F low in
    all 3 cities → two fake "edges" that settlement proved would have lost). Only
    the cost/liquidity gates prevented losses. Hence NWS first; the Meteostat path
    survives only as a fallback and we only count hours <= the current local hour
    there so a model-filled FUTURE hour can't leak in.
    """
    if city_key not in CITY_CONFIG:
        return None
    station = METEOSTAT_STATION.get(city_key)
    if not station:
        return None
    if target_date is None:
        target_date = date.today()

    # Time-gate: only trust the bound on a finished local day, or on the in-progress
    # local day AFTER the relevant extreme has typically occurred (else skip ->
    # uncensored forecast). This is what stops a morning overnight reading from
    # making the bot over-confident before the day's high has even happened.
    local_now = station_local_now(city_key)
    local_date = local_now.date()
    if target_date > local_date:
        return None  # future local day: nothing observed yet
    if target_date == local_date:
        set_hour = _HIGH_SET_LOCAL_HOUR if metric == "high" else _LOW_SET_LOCAL_HOUR
        if local_now.hour < set_hour:
            return None  # extreme not reliably reached yet today

    field = "tmax" if metric == "high" else "tmin"
    key = f"{station}_{target_date.isoformat()}_{field}"
    now = time.time()
    cached = _observed_cache.get(key)
    if cached and now - cached[0] < _OBSERVED_TTL:
        return cached[1]

    in_progress = target_date == local_date
    unit = CITY_CONFIG[city_key].get("unit", "F")
    value: Optional[float] = None

    # Settlement-grade source first (US cities): the NWS station feed.
    icao = CITY_CONFIG[city_key].get("nws_station")
    if icao and unit == "F":
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                value = await _nws_observed_extreme(
                    client, icao, CITY_CONFIG[city_key].get("tz") or "UTC", target_date, metric
                )
        except Exception as e:
            logger.warning(f"NWS observed-extreme fetch failed for {city_key}/{metric}, "
                           f"falling back to Meteostat: {e}")
        if value is not None:
            _observed_cache[key] = (now, value)
            return value

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if in_progress:
                # Intraday: derive the observed-SO-FAR extreme from HOURLY obs. The proxy
                # returns LOCAL timestamps when given ``tz``, so ``time[11:13]`` is the local
                # hour; keep only hours <= now so a backfilled future hour can't leak in.
                r = await client.get(_METEOSTAT_HOURLY_URL, params={
                    "station": station,
                    "start": target_date.isoformat(),
                    "end": target_date.isoformat(),
                    "tz": CITY_CONFIG[city_key].get("tz") or "UTC",
                }, headers=_METEOSTAT_HEADERS)
                if r.status_code == 200:
                    temps_c = []
                    for row in r.json().get("data", []) or []:
                        t, temp = row.get("time"), row.get("temp")
                        if not t or temp is None:
                            continue
                        try:
                            same_day = t[:10] == target_date.isoformat()
                            hr = int(t[11:13])
                        except (ValueError, IndexError):
                            continue
                        # target day only, and only hours already elapsed (a boundary row
                        # from an adjacent day, or a backfilled future hour, must not leak in)
                        if same_day and hr <= local_now.hour:
                            temps_c.append(float(temp))
                    if temps_c:
                        ext_c = max(temps_c) if metric == "high" else min(temps_c)
                        value = _celsius_to_fahrenheit(ext_c) if unit == "F" else ext_c
            else:
                # Finished local day: the DAILY aggregate is the authoritative final extreme.
                r = await client.get(_METEOSTAT_DAILY_URL, params={
                    "station": station,
                    "start": (target_date - timedelta(days=1)).isoformat(),
                    "end": target_date.isoformat(),
                }, headers=_METEOSTAT_HEADERS)
                if r.status_code == 200:
                    rows = {x.get("date", "")[:10]: x for x in r.json().get("data", []) or []}
                    row = rows.get(target_date.isoformat())
                    val_c = row.get(field) if row else None
                    if val_c is not None:
                        value = _celsius_to_fahrenheit(val_c) if unit == "F" else float(val_c)
    except Exception as e:
        logger.debug(f"Observed-extreme fetch failed for {city_key}/{metric}: {e}")
        return None  # don't cache transient failures

    _observed_cache[key] = (now, value)
    return value
