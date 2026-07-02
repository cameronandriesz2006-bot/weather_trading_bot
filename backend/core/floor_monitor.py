"""Floor-honesty monitor — the regression alarm for the observed-extreme feed.

The 2026-07-01 autopsy: the intraday floor fed the nowcast values 1.3-3.4°F below
settlement truth for a full day and nothing noticed (only the cost gates prevented
losses). This job makes that class of failure loud: every morning it compares OUR
settlement-grade observed extreme for each finished local day against the bucket the
market actually resolved to. Any day where our number lands outside the settled
bucket means the obs feed and the settlement source have diverged — the exact
precondition for confidently-wrong pricing — and is logged as a WARNING.

Checks the last two finished local days (day-2 catches markets that resolved late).
"""
import json
import logging
from datetime import timedelta
from typing import List, Optional, Tuple

import httpx

from backend.config import settings
from backend.data.weather import CITY_CONFIG, fetch_observed_extreme, station_local_now
from backend.data.weather_markets import parse_bucket_label

logger = logging.getLogger("trading_bot")

GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
# city key -> event-slug fragment where they differ
_SLUG = {"los_angeles": "los-angeles", "hong_kong": "hong-kong"}


def _settled_bucket(event: dict) -> Optional[Tuple[Optional[float], Optional[float], str]]:
    """(low, high, label) of the resolved winning bucket, or None if not resolved yet."""
    for m in event.get("markets", []) or []:
        op = m.get("outcomePrices")
        try:
            op = json.loads(op) if isinstance(op, str) else op
        except (ValueError, TypeError):
            continue
        if not op or float(op[0]) < 0.99:
            continue
        if m.get("umaResolutionStatus") != "resolved" and not m.get("closed"):
            continue
        label = m.get("groupItemTitle") or ""
        rng = parse_bucket_label(label)
        if rng:
            return rng[0], rng[1], label
    return None


async def floor_honesty_check() -> List[dict]:
    """Compare our settlement-grade observed high vs the settled bucket, per active
    city, for the last two finished station-local days. Returns one result dict per
    (city, day) that could be checked; logs a WARNING on any mismatch."""
    results: List[dict] = []
    cities = [c.strip() for c in settings.WEATHER_CITIES.split(",") if c.strip()]
    async with httpx.AsyncClient(timeout=20.0) as client:
        for city in cities:
            cfg = CITY_CONFIG.get(city)
            if not cfg or not cfg.get("nws_station"):
                continue
            for back in (1, 2):
                d = station_local_now(city).date() - timedelta(days=back)
                slug = (f"highest-temperature-in-{_SLUG.get(city, city)}-on-"
                        f"{d.strftime('%B').lower()}-{d.day}-{d.year}")
                try:
                    r = await client.get(GAMMA_EVENTS, params={"slug": slug})
                    events = r.json()
                except Exception as e:
                    logger.debug(f"floor-honesty: event fetch failed for {slug}: {e}")
                    continue
                if not events:
                    continue
                settled = _settled_bucket(events[0])
                if settled is None:
                    continue  # not resolved yet; the day-2 pass will catch it tomorrow
                lo, hi, label = settled
                ours = await fetch_observed_extreme(city, "high", d)
                if ours is None:
                    logger.warning(f"FLOOR-HONESTY: {city} {d}: settled '{label}' but our obs "
                                   f"feed returned NOTHING — check the NWS/Meteostat path")
                    results.append({"city": city, "date": d.isoformat(), "ours": None,
                                    "settled": label, "ok": False})
                    continue
                ok = (lo is None or ours >= lo) and (hi is None or ours <= hi)
                if not ok:
                    logger.warning(f"FLOOR-HONESTY MISMATCH: {city} {d}: our observed high "
                                   f"{ours:.0f} is OUTSIDE the settled bucket '{label}' — the "
                                   f"obs feed and settlement source have diverged; do not "
                                   f"trust the floor until resolved")
                else:
                    logger.info(f"floor-honesty ok: {city} {d}: ours {ours:.0f} in settled '{label}'")
                results.append({"city": city, "date": d.isoformat(), "ours": ours,
                                "settled": label, "ok": ok})
    return results
