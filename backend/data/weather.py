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
_bias_cache: Optional[Dict[str, dict]] = None


def _load_bias() -> Dict[str, dict]:
    """Load (and memoise) the station-bias table; {} if absent/unreadable."""
    global _bias_cache
    if _bias_cache is None:
        try:
            _bias_cache = json.loads(_BIAS_FILE.read_text()).get("stations", {}) or {}
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
        base = max(raw_sigma * settings.WEATHER_SIGMA_INFLATION, floor)
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

    def probability_high_in_range(self, low_f: Optional[float], high_f: Optional[float],
                                  floor: Optional[float] = None,
                                  local_hour: Optional[int] = None) -> float:
        """Probability the daily HIGH falls in the bucket (fitted, widened, bias-corrected).
        ``floor`` = observed max so far today: the final high can't end below it.
        ``local_hour`` (station-local, in-progress day only) engages the intraday σ curve."""
        if not self.member_highs:
            return 0.5
        return self._fitted_bucket_prob(self.corrected_mean("high"), self.std_high,
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
        return self._fitted_bucket_prob(self.corrected_mean("low"), self.std_low,
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
# Open-Meteo's free tier is ~10,000 requests/DAY (per IP). With ~33 unique
# (city, date) forecasts, a 30-min cache costs ~1,600/day — a comfortable 6x
# margin (a 15-min cache was ~3,200/day, still safe, but day-of testing + the old
# failure-cascade blew past 10k and exhausted the quota until the next UTC day).
_CACHE_TTL = 1800      # 30 minutes (successful forecast) — daily-temp forecasts
                       # barely move in 30 min, and the observed-floor adds intraday
                       # freshness near settlement.
# When a fetch FAILS (e.g. the daily limit is hit), back off for a while instead
# of retrying every scan — that both saves pointless load and avoids keeping a
# rolling-window limit pegged. Recovery is still noticed within this interval.
_FAIL_TTL = 600        # 10 minutes (negative cache / backoff for a failed fetch)


def _celsius_to_fahrenheit(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


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

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Open-Meteo Ensemble API — GFS ensemble with 31 members
            params = {
                "latitude": city["lat"],
                "longitude": city["lon"],
                "daily": "temperature_2m_max,temperature_2m_min",
                "temperature_unit": api_temp_unit,
                "start_date": target_date.isoformat(),
                "end_date": target_date.isoformat(),
                "models": "gfs_seamless",
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
            )

            _forecast_cache[cache_key] = (now, forecast)
            logger.info(f"Ensemble forecast for {city['name']} on {target_date}: "
                        f"High {forecast.mean_high:.1f}{unit} +/- {forecast.std_high:.1f}{unit} "
                        f"({forecast.num_members} members)")

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
_METEOSTAT_HEADERS = {"User-Agent": "Mozilla/5.0"}  # the proxy 403s a bare client
_observed_cache: Dict[str, tuple] = {}   # key -> (timestamp, value-or-None)
_OBSERVED_TTL = 900  # 15 min

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


async def fetch_observed_extreme(
    city_key: str, metric: str, target_date: Optional[date] = None
) -> Optional[float]:
    """Observed daily HIGH (max) / LOW (min) SO FAR at the settlement station on
    ``target_date``, in the city's NATIVE unit — a hard bound on the final settled
    value. Returns None when it can't be trusted (no obs station, future date, or
    fetch failure) so callers fall back to the uncensored forecast.

    Meteostat is observation-based, so it never OVERSTATES the extreme; if it lags
    a couple hours the bound is merely weaker (safe), never wrong — which is also
    why it's harmless before the diurnal peak and strongest in the evening, exactly
    when we want it.
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

    value: Optional[float] = None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
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
                    unit = CITY_CONFIG[city_key].get("unit", "F")
                    value = _celsius_to_fahrenheit(val_c) if unit == "F" else float(val_c)
    except Exception as e:
        logger.debug(f"Observed-extreme fetch failed for {city_key}/{metric}: {e}")
        return None  # don't cache transient failures

    _observed_cache[key] = (now, value)
    return value
