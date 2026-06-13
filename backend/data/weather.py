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
# NOTE: Kalshi may settle on different stations; verify separately before enabling it.
CITY_CONFIG: Dict[str, dict] = {
    "nyc": {
        "name": "New York City",
        # Settles on LaGuardia Airport (KLGA), NOT Central Park (KNYC).
        "lat": 40.7792,
        "lon": -73.8800,
        "nws_station": "KLGA",
    },
    "chicago": {
        "name": "Chicago",
        # Chicago O'Hare Intl (KORD).
        "lat": 41.9950,
        "lon": -87.9336,
        "nws_station": "KORD",
    },
    "miami": {
        "name": "Miami",
        # Miami Intl (KMIA).
        "lat": 25.7906,
        "lon": -80.3164,
        "nws_station": "KMIA",
    },
    "los_angeles": {
        "name": "Los Angeles",
        # Los Angeles Intl (KLAX) — coastal, much cooler than downtown.
        "lat": 33.9381,
        "lon": -118.3889,
        "nws_station": "KLAX",
    },
    "denver": {
        "name": "Denver",
        # Settles on Buckley Space Force Base (KBKF) in Aurora, NOT Denver Intl (KDEN).
        "lat": 39.7172,
        "lon": -104.7517,
        "nws_station": "KBKF",
    },
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
    Signed correction (deg F) to SUBTRACT from the model mean for this station.
    Positive => the model runs warm. Returns 0.0 when disabled, missing, or
    under-sampled, and is clamped to +/- WEATHER_BIAS_MAX_SHIFT_F for safety.
    """
    from backend.config import settings
    if not settings.WEATHER_BIAS_ENABLED:
        return 0.0
    entry = _load_bias().get(city_key, {}).get(metric)
    if not entry or entry.get("samples", 0) < settings.WEATHER_BIAS_MIN_SAMPLES:
        return 0.0
    bias = float(entry.get("bias_f", 0.0))
    cap = settings.WEATHER_BIAS_MAX_SHIFT_F
    return max(-cap, min(cap, bias))


@dataclass
class EnsembleForecast:
    """Ensemble weather forecast with per-member data."""
    city_key: str
    city_name: str
    target_date: date
    member_highs: List[float]  # Daily max temps (F) per ensemble member
    member_lows: List[float]   # Daily min temps (F) per ensemble member
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

    def _effective_sigma(self, raw_sigma: float) -> float:
        """
        Widen the (under-dispersed) ensemble spread into an honest forecast sigma.

        sigma_eff = max(raw_sigma * INFLATION, FLOOR) + lead_days * PER_LEAD_DAY
        """
        from backend.config import settings
        base = max(raw_sigma * settings.WEATHER_SIGMA_INFLATION, settings.WEATHER_SIGMA_FLOOR_F)
        lead_days = max(0, (self.target_date - date.today()).days)
        return base + lead_days * settings.WEATHER_SIGMA_PER_LEAD_DAY_F

    def _fitted_bucket_prob(self, mean: float, raw_sigma: float,
                            low_f: Optional[float], high_f: Optional[float]) -> float:
        """
        P(temperature in bucket) under a fitted, widened Normal.

        Integrates N(mean, sigma_eff) over the bucket's rounding interval
        [low-0.5, high+0.5); open bounds extend to -/+ infinity.
        """
        sigma = self._effective_sigma(raw_sigma)
        lo = (low_f - 0.5) if low_f is not None else None
        hi = (high_f + 0.5) if high_f is not None else None
        p_lo = self._normal_cdf(lo, mean, sigma) if lo is not None else 0.0
        p_hi = self._normal_cdf(hi, mean, sigma) if hi is not None else 1.0
        return max(0.0, p_hi - p_lo)

    def corrected_mean(self, metric: str) -> float:
        """Forecast mean with the per-station bias removed (the mean we price on)."""
        raw = self.mean_high if metric == "high" else self.mean_low
        return raw - get_station_bias(self.city_key, metric)

    def probability_high_in_range(self, low_f: Optional[float], high_f: Optional[float]) -> float:
        """Probability the daily HIGH falls in the bucket (fitted, widened, bias-corrected)."""
        if not self.member_highs:
            return 0.5
        return self._fitted_bucket_prob(self.corrected_mean("high"), self.std_high, low_f, high_f)

    def probability_low_in_range(self, low_f: Optional[float], high_f: Optional[float]) -> float:
        """Probability the daily LOW falls in the bucket (fitted, widened, bias-corrected)."""
        if not self.member_lows:
            return 0.5
        return self._fitted_bucket_prob(self.corrected_mean("low"), self.std_low, low_f, high_f)

    @property
    def ensemble_agreement(self) -> float:
        """How one-sided the ensemble is (0.5 = split, 1.0 = unanimous)."""
        if not self.member_highs:
            return 0.5
        median = statistics.median(self.member_highs)
        above = sum(1 for h in self.member_highs if h > median)
        frac = above / len(self.member_highs)
        return max(frac, 1 - frac)


# Simple cache: (city_key, target_date_str) -> (timestamp, EnsembleForecast)
_forecast_cache: Dict[str, tuple] = {}
_CACHE_TTL = 900  # 15 minutes


def _celsius_to_fahrenheit(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


async def fetch_ensemble_forecast(city_key: str, target_date: Optional[date] = None) -> Optional[EnsembleForecast]:
    """
    Fetch ensemble forecast from Open-Meteo Ensemble API (free, 31-member GFS).
    Returns per-member daily max/min temperatures in Fahrenheit.
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
        if now - cached_time < _CACHE_TTL:
            return cached_forecast

    city = CITY_CONFIG[city_key]

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Open-Meteo Ensemble API — GFS ensemble with 31 members
            params = {
                "latitude": city["lat"],
                "longitude": city["lon"],
                "daily": "temperature_2m_max,temperature_2m_min",
                "temperature_unit": "fahrenheit",
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
                return None

            forecast = EnsembleForecast(
                city_key=city_key,
                city_name=city["name"],
                target_date=target_date,
                member_highs=member_highs,
                member_lows=member_lows,
            )

            _forecast_cache[cache_key] = (now, forecast)
            logger.info(f"Ensemble forecast for {city['name']} on {target_date}: "
                        f"High {forecast.mean_high:.1f}F +/- {forecast.std_high:.1f}F "
                        f"({forecast.num_members} members)")

            return forecast

    except Exception as e:
        logger.warning(f"Failed to fetch ensemble forecast for {city_key}: {e}")
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
