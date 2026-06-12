"""Weather temperature market fetcher from Polymarket.

Polymarket lists daily temperature markets as a group of mutually-exclusive
range buckets per city per day, e.g. the event
``highest-temperature-in-nyc-on-june-12-2026`` contains markets like
"81°F or below", "82-83°F", ..., "100°F or higher". Each bucket is a Yes/No
market on whether the day's high (or low) lands in that range.

This reader:
- queries the Gamma API with the correct ``tag_slug=daily-temperature`` filter
  (the old ``tag`` / ``slug_contains`` params were silently ignored, which is
  why scans found 0 markets) and paginates past the 100-event page cap;
- derives city / metric / date from the *event slug* (robust, explicit year)
  rather than guessing from the question text;
- parses each bucket's ``groupItemTitle`` into a numeric range and SKIPS any
  bucket it cannot read cleanly instead of fabricating a threshold.
"""
import httpx
import re
import logging
from dataclasses import dataclass
from datetime import date
from typing import List, Optional, Tuple

logger = logging.getLogger("trading_bot")

# Map the city fragment in an event slug to our internal city key.
# (Slugs use "los-angeles"; our key is "los_angeles".)
SLUG_CITY_TO_KEY = {
    "nyc": "nyc",
    "chicago": "chicago",
    "miami": "miami",
    "denver": "denver",
    "los-angeles": "los_angeles",
}

MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

# event slug: "{highest|lowest}-temperature-in-{city}-on-{month}-{day}-{year}"
_EVENT_SLUG_RE = re.compile(
    r"^(?P<metric>highest|lowest)-temperature-in-"
    r"(?P<city>.+)-on-(?P<month>[a-z]+)-(?P<day>\d{1,2})-(?P<year>\d{4})$"
)

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
WEATHER_TAG_SLUG = "daily-temperature"


@dataclass
class WeatherMarket:
    """A single temperature range-bucket market on a prediction platform."""
    slug: str                 # event slug (shared by all buckets in the group)
    market_id: str
    platform: str
    title: str
    city_key: str
    city_name: str
    target_date: date
    metric: str               # "high" or "low"
    # Range bucket bounds in Fahrenheit. None = open-ended tail.
    #   "81°F or below" -> (None, 81)   "82-83°F" -> (82, 83)   "100°F or higher" -> (100, None)
    low_f: Optional[float]
    high_f: Optional[float]
    bucket_label: str         # human-readable, e.g. "82-83°F"
    yes_price: float          # Price of YES outcome (0-1), ~mid
    no_price: float           # Price of NO outcome (0-1), ~mid
    spread: float = 0.0       # live bid/ask spread in price units (cost to cross)
    volume: float = 0.0
    closed: bool = False

    @property
    def threshold_f(self) -> float:
        """Representative threshold for display/compat (the defined bound)."""
        if self.low_f is not None:
            return self.low_f
        if self.high_f is not None:
            return self.high_f
        return 0.0

    @property
    def direction(self) -> str:
        """Compat token describing the bucket shape: above / below / range."""
        if self.low_f is None:
            return "below"      # "X or below"
        if self.high_f is None:
            return "above"      # "X or higher"
        return "range"          # "X-Y"


def parse_bucket_label(label: str) -> Optional[Tuple[Optional[float], Optional[float]]]:
    """
    Parse a bucket label into (low_f, high_f); None bound = open-ended.

    Returns None if the label can't be read cleanly (caller should skip it).
    """
    if not label:
        return None
    text = label.lower().replace("°", " ").replace("℉", " ")

    # "81 or below" / "81f or below" / "81 or lower"
    m = re.search(r"(\d+)\s*f?\s*or\s*(?:below|lower|less)", text)
    if m:
        return (None, float(m.group(1)))

    # "100 or higher" / "100f or above" / "100 or more"
    m = re.search(r"(\d+)\s*f?\s*or\s*(?:higher|above|more)", text)
    if m:
        return (float(m.group(1)), None)

    # "82-83" / "82 - 83"
    m = re.search(r"(\d+)\s*[-–]\s*(\d+)", text)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        if lo <= hi:
            return (lo, hi)
        return None

    return None


def parse_event_slug(slug: str) -> Optional[Tuple[str, str, date]]:
    """
    Parse a daily-temperature event slug into (city_key, metric, target_date).

    Returns None if the slug isn't a recognised daily-temperature event for a
    city we track, or the date can't be parsed.
    """
    m = _EVENT_SLUG_RE.match(slug or "")
    if not m:
        return None

    city_key = SLUG_CITY_TO_KEY.get(m.group("city"))
    if not city_key:
        return None

    month = MONTH_MAP.get(m.group("month"))
    if not month:
        return None

    try:
        target_date = date(int(m.group("year")), month, int(m.group("day")))
    except ValueError:
        return None

    metric = "high" if m.group("metric") == "highest" else "low"
    return city_key, metric, target_date


def _parse_outcome_prices(market_data: dict) -> Optional[Tuple[float, float]]:
    """Return (yes_price, no_price) or None if unreadable."""
    outcome_prices = market_data.get("outcomePrices", [])
    if isinstance(outcome_prices, str):
        import json
        try:
            outcome_prices = json.loads(outcome_prices)
        except Exception:
            return None
    if not outcome_prices or len(outcome_prices) < 2:
        return None
    try:
        return float(outcome_prices[0]), float(outcome_prices[1])
    except (ValueError, TypeError, IndexError):
        return None


def _parse_bucket_market(
    market_data: dict,
    event_slug: str,
    city_key: str,
    city_name: str,
    metric: str,
    target_date: date,
) -> Optional[WeatherMarket]:
    """Parse one bucket market within a daily-temperature event."""
    if market_data.get("closed", False):
        return None

    label = market_data.get("groupItemTitle") or ""
    bounds = parse_bucket_label(label)
    if bounds is None:
        # Can't read this bucket cleanly — skip rather than guess.
        logger.debug(f"Skipping unparseable weather bucket {label!r} in {event_slug}")
        return None
    low_f, high_f = bounds

    prices = _parse_outcome_prices(market_data)
    if prices is None:
        return None
    yes_price, no_price = prices

    # Drop dead/illiquid buckets pinned to the rails; the near-the-money
    # buckets are the only tradeable ones.
    if yes_price <= 0.01 or yes_price >= 0.99:
        return None

    try:
        spread = float(market_data.get("spread", 0) or 0)
    except (ValueError, TypeError):
        spread = 0.0

    return WeatherMarket(
        slug=event_slug,
        market_id=str(market_data.get("id", "")),
        platform="polymarket",
        title=market_data.get("question", "") or label,
        city_key=city_key,
        city_name=city_name,
        target_date=target_date,
        metric=metric,
        low_f=low_f,
        high_f=high_f,
        bucket_label=label,
        yes_price=yes_price,
        no_price=no_price,
        spread=spread,
        volume=float(market_data.get("volume", 0) or 0),
    )


async def _fetch_daily_temperature_events(client: httpx.AsyncClient) -> List[dict]:
    """Fetch all open daily-temperature events, paginating past the page cap."""
    events: List[dict] = []
    for offset in range(0, 600, 100):  # safety cap; far more than the live set
        response = await client.get(
            GAMMA_EVENTS_URL,
            params={
                "closed": "false",
                "limit": 100,
                "offset": offset,
                "tag_slug": WEATHER_TAG_SLUG,
            },
        )
        response.raise_for_status()
        page = response.json()
        if not page:
            break
        events.extend(page)
        if len(page) < 100:
            break
    return events


async def fetch_polymarket_weather_markets(city_keys: Optional[List[str]] = None) -> List[WeatherMarket]:
    """
    Fetch open Polymarket daily-temperature range-bucket markets for the given
    cities (default: all tracked cities), dated today or later.
    """
    from backend.data.weather import CITY_CONFIG

    markets: List[WeatherMarket] = []
    today = date.today()

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            events = await _fetch_daily_temperature_events(client)
    except Exception as e:
        logger.warning(f"Failed to fetch weather markets: {e}")
        return markets

    for event in events:
        parsed = parse_event_slug(event.get("slug", ""))
        if not parsed:
            continue
        city_key, metric, target_date = parsed

        if city_keys and city_key not in city_keys:
            continue
        if target_date < today:
            continue
        if city_key not in CITY_CONFIG:
            continue
        city_name = CITY_CONFIG[city_key]["name"]

        for market_data in event.get("markets", []):
            market = _parse_bucket_market(
                market_data, event.get("slug", ""), city_key, city_name, metric, target_date
            )
            if market:
                markets.append(market)

    logger.info(f"Found {len(markets)} weather temperature markets")
    return markets
