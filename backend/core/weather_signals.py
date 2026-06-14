"""Signal generator for weather temperature markets using ensemble forecasts."""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import httpx

from backend.config import settings
from backend.core.sizing import calculate_edge, calculate_kelly_size
from backend.data.weather import (
    fetch_ensemble_forecast, EnsembleForecast, CITY_CONFIG, get_station_bias,
    fetch_observed_extreme, station_local_hour, intraday_sigma,
)
from backend.data.weather_markets import WeatherMarket, fetch_polymarket_weather_markets
from backend.data.orderbook import (
    fetch_ask_levels, walk_asks_for_cash, fetch_book_top, fetch_books, LiveBook,
)
from backend.models.database import SessionLocal, Signal

logger = logging.getLogger("trading_bot")


@dataclass
class WeatherTradingSignal:
    """A trading signal for a weather temperature market."""
    market: WeatherMarket

    # Core signal data
    model_probability: float = 0.5   # Ensemble probability of YES outcome
    market_probability: float = 0.5  # Market's implied YES probability (mid)
    edge: float = 0.0                # gross edge: model - market mid
    direction: str = "yes"           # "yes" or "no"

    # Cost-adjusted economics (Phase 6)
    net_edge: float = 0.0            # gross edge minus trading costs (spread + fee)
    entry_price: float = 0.0         # effective entry (real ask, or mid + spread/2)
    cost: float = 0.0                # per-share cost in price units (spread/2 + fee)
    rel_spread: float = 1.0          # spread as a fraction of the side's price (Layer 1)

    # Confidence and sizing
    confidence: float = 0.5
    kelly_fraction: float = 0.0
    suggested_size: float = 0.0

    # Metadata
    sources: List[str] = field(default_factory=list)
    reasoning: str = ""
    timestamp: datetime = field(default_factory=datetime.utcnow)

    # Forecast context
    ensemble_mean: float = 0.0
    ensemble_std: float = 0.0
    ensemble_members: int = 0

    # Market-gap guardrail: |our corrected forecast mean - market-implied mean| in
    # native unit (None if the event lacked enough buckets to compute it).
    market_gap: Optional[float] = None

    @property
    def market_gap_ok(self) -> bool:
        """True if our forecast mean is close enough to the market-implied mean to
        trust our edge. A large gap means WE are likely miscalibrated (wrong station
        / uncorrected bias), so the edge is a mirage — suppress the whole event."""
        if not settings.WEATHER_MARKET_GAP_ENABLED or self.market_gap is None:
            return True
        scale = (1.0 / 1.8) if getattr(self.market, "unit", "F") == "C" else 1.0
        return self.market_gap <= settings.WEATHER_MAX_MARKET_GAP_F * scale

    @property
    def passes_threshold(self) -> bool:
        """Actionable only if, after costs, the edge clears the threshold, the
        effective entry price is within the cap, the market is real enough to trade
        (enough resting liquidity, enough actually-traded volume, tight-enough
        relative spread — Layer 1), AND our forecast mean isn't wildly off the
        market-implied mean (the market-gap guardrail)."""
        return (
            self.net_edge >= settings.WEATHER_MIN_EDGE_THRESHOLD
            and 0 < self.entry_price <= settings.WEATHER_MAX_ENTRY_PRICE
            and self.market.liquidity >= settings.WEATHER_MIN_LIQUIDITY
            and self.market.volume >= settings.WEATHER_MIN_VOLUME
            and self.rel_spread <= settings.WEATHER_MAX_REL_SPREAD
            and self.market_gap_ok
        )


def compute_event_market_means(markets: List[WeatherMarket]) -> None:
    """Set ``event_market_mean`` on every market from its event's bucket prices.

    The market-implied mean is the probability-weighted center of an event's
    buckets: sum(midpoint * yes_price) / sum(yes_price), over the buckets with a
    finite range (open-ended tails have no midpoint and are skipped). On a near-
    settlement day this is a near-truth estimate of where the high/low will land,
    so the guardrail can compare it to our forecast mean. Call AFTER live prices
    are refreshed. Events with fewer than the configured minimum of finite buckets
    are left as None (implied mean not trustworthy -> guardrail is a no-op there).
    """
    from collections import defaultdict
    events: Dict[tuple, List[WeatherMarket]] = defaultdict(list)
    for m in markets:
        events[(m.city_key, m.target_date, m.metric)].append(m)

    for bucket_list in events.values():
        num = den = 0.0
        n_finite = 0
        for m in bucket_list:
            if m.low_f is None or m.high_f is None:
                continue  # open tail: undefined midpoint
            price = m.yes_price
            if price is None or price <= 0:
                continue
            midpoint = (m.low_f + m.high_f) / 2.0
            num += midpoint * price
            den += price
            n_finite += 1
        implied = (num / den) if (den > 0 and n_finite >= settings.WEATHER_MARKET_GAP_MIN_BUCKETS) else None
        for m in bucket_list:
            m.event_market_mean = implied


def _apply_live_top(market: WeatherMarket, top) -> bool:
    """Overwrite the market's Gamma-sourced PRICE fields from a live CLOB BookTop.

    Gamma's ``outcomePrices``/``bestBid``/``bestAsk`` can be ~20c stale on thin
    daily-temperature markets, and those fields feed the edge SCREEN — so a stale
    mid can hide a genuine edge and the bucket would never get walked (a missed
    opportunity). We refresh from the YES token's live top-of-book; the NO side is
    the book's mirror (handled downstream). Only fast-moving PRICE fields are
    touched — ``liquidity``/``volume`` stay as Gamma's slower-moving aggregates.
    Only applies a clean two-sided quote; otherwise leaves Gamma values untouched,
    so this can only help, never break the scan. Returns True if it mutated.
    """
    if not top or top.best_bid is None or top.best_ask is None:
        return False
    bid, ask = top.best_bid, top.best_ask
    if not (0.0 < bid < ask < 1.0):
        return False
    market.best_bid = bid
    market.best_ask = ask
    market.spread = round(ask - bid, 4)
    market.yes_price = round(top.mid, 4)
    market.no_price = round(1.0 - top.mid, 4)
    return True


async def _refresh_market_prices_live(
    market: WeatherMarket, book_client: Optional[httpx.AsyncClient]
) -> None:
    """Single-market live-price refresh (for standalone callers).

    The scan refreshes all markets in one batched request (see
    ``scan_for_weather_signals``); this per-market path is for callers that score
    one market on its own. Fetches the YES token's live top-of-book and applies it.
    """
    token = market.token_id_yes
    if not token:
        return
    try:
        if book_client is not None:
            top = await fetch_book_top(token, book_client)
        else:
            async with httpx.AsyncClient(timeout=8.0) as client:
                top = await fetch_book_top(token, client)
    except Exception as e:
        logger.debug(f"Live price refresh failed for {market.market_id}: {e}")
        return
    _apply_live_top(market, top)


async def generate_weather_signal(
    market: WeatherMarket,
    bankroll: Optional[float] = None,
    book_client: Optional[httpx.AsyncClient] = None,
    refresh_prices: bool = True,
    books: Optional[Dict[str, LiveBook]] = None,
) -> Optional[WeatherTradingSignal]:
    """
    Generate a trading signal for a weather temperature market.

    Uses ensemble forecast to estimate probability:
    - Count fraction of ensemble members above/below the threshold
    - Compare to market price to find edge
    - Size using Kelly criterion

    Kelly is sized off the LIVE bankroll passed in (so bets shrink as the
    bankroll falls); it falls back to INITIAL_BANKROLL only if not provided.
    """
    forecast = await fetch_ensemble_forecast(market.city_key, market.target_date)
    if not forecast or not forecast.member_highs:
        return None

    # Observed-so-far hard bound: the final high can't end below the max already
    # recorded today, nor the low above the min. Censor the forecast at it so the
    # bot stops finding fake edges on outcomes that are already physically
    # impossible (the "1 hour to close, high already locked in" case). None when
    # unavailable (future date / no obs station) -> uncensored fallback.
    observed_bound = await fetch_observed_extreme(market.city_key, market.metric, market.target_date)

    # Intraday σ schedule: on the in-progress local day, narrow (or widen) the forecast
    # to the empirical residual uncertainty at the current station-local hour instead of
    # the flat σ-floor. ``local_hour`` is None except on the station-local today, so
    # future days keep the lead-time σ. None -> the prob fns use the old formula.
    local_hour = station_local_hour(market.city_key, market.target_date)

    # Model YES probability = fraction of ensemble members whose daily high/low
    # lands in this market's temperature bucket.
    if market.metric == "high":
        model_yes_prob = forecast.probability_high_in_range(
            market.low_f, market.high_f, floor=observed_bound, local_hour=local_hour)
    else:
        model_yes_prob = forecast.probability_low_in_range(
            market.low_f, market.high_f, ceiling=observed_bound, local_hour=local_hour)

    # Light clip only to keep Kelly's odds math finite; do NOT inflate genuinely
    # tiny bucket probabilities (that would manufacture fake edges on dead buckets).
    model_yes_prob = max(0.01, min(0.99, model_yes_prob))

    # Screen on LIVE prices: refresh the market's price fields from the CLOB book
    # before computing the edge, so a stale Gamma mid can't hide a real edge. In
    # the scan this is already done in one batched request up front, so it's
    # skipped here (refresh_prices=False); standalone callers refresh per-market.
    if refresh_prices:
        await _refresh_market_prices_live(market, book_client)

    market_yes_prob = market.yes_price

    # Gross edge & chosen side from the market MID (treats yes=up, no=down)
    edge, direction_raw = calculate_edge(model_yes_prob, market_yes_prob)
    direction = "yes" if direction_raw == "up" else "no"

    # --- Trading costs (Phase 6 + Layer 1) ---
    # The dominant cost on Polymarket is crossing the bid/ask spread. Prefer the
    # live best bid/ask when both are present and sane (then we enter at the real
    # ask); otherwise fall back to the reported spread around the side mid.
    side_mid = market.yes_price if direction == "yes" else market.no_price

    bid, ask = market.best_bid, market.best_ask
    if direction == "no" and bid is not None and ask is not None:
        # The NO book is the mirror of the YES book: ask_no = 1 - bid_yes, etc.
        bid, ask = (1.0 - market.best_ask), (1.0 - market.best_bid)

    if bid is not None and ask is not None and 0.0 < bid < ask < 1.0:
        spread_used = ask - bid
        entry_price = min(0.999, ask)              # the real ask we'd pay
    else:
        spread_used = market.spread if market.spread and market.spread > 0 else settings.WEATHER_DEFAULT_SPREAD
        entry_price = min(0.999, side_mid + spread_used / 2.0)

    half_spread = spread_used / 2.0
    cost = half_spread + settings.WEATHER_FEE_RATE

    # Spread as a fraction of the side's price: a 2c spread on a 4c contract is a
    # 50% mirage even though 2c "looks" tiny. Gated in passes_threshold.
    rel_spread = spread_used / side_mid if side_mid > 0 else 1.0

    # Edge after costs — this is what we actually gate and size on.
    net_edge = edge - cost

    # Confidence = how sharply the ensemble is concentrated (one-sided around median).
    confidence = min(0.9, forecast.ensemble_agreement)

    # Kelly sizing on the COST-ADJUSTED economics: pass the effective entry price
    # for the chosen side so the spread is baked into the odds. Size off the LIVE
    # bankroll so bets scale down after losses.
    if bankroll is None or bankroll <= 0:
        bankroll = settings.INITIAL_BANKROLL
    kelly_market_price = entry_price if direction == "yes" else (1.0 - entry_price)
    if net_edge > 0:
        suggested_size = calculate_kelly_size(
            edge=net_edge,
            probability=model_yes_prob,
            market_price=kelly_market_price,
            direction=direction_raw,  # calculate_kelly_size expects "up"/"down"
            bankroll=bankroll,
        )
        # Per-trade size is already capped relative to bankroll inside the Kelly helper
        # (KELLY_MAX_TRADE_FRACTION), so no fixed-dollar clamp here. Layer 2(i): never
        # simulate taking more than a small slice of the book, so we don't pretend to
        # fill a large stake into a thin market.
        if market.liquidity and market.liquidity > 0:
            suggested_size = min(suggested_size, settings.WEATHER_MAX_BOOK_FRACTION * market.liquidity)
    else:
        suggested_size = 0.0

    # --- Exact fill: walk the REAL order book (no modelled slippage) ----------
    # A marketable buy crosses the book level-by-level, so the price we actually
    # pay is the VWAP across consumed levels — worse than the top-of-book ask,
    # often dramatically so on thin weather buckets (a 6c best ask can fill at a
    # ~19c VWAP). We replace the estimated entry with this exact fill.
    #
    # Only candidates are refined: walking can only LOWER net edge (you always
    # pay up), so a bucket that doesn't clear the bar at the best ask can't clear
    # it after slippage — no point fetching its book. This also keeps the scan
    # cheap (a handful of book fetches, not one per bucket).
    fill_levels = 0
    fill_best_ask = entry_price
    fill_partial = False
    token_id = market.token_id_yes if direction == "yes" else market.token_id_no
    if net_edge >= settings.WEATHER_MIN_EDGE_THRESHOLD and suggested_size > 0 and token_id:
        asks = None
        # Prefer the pre-fetched batched book (the scan fetches every token's full
        # book in a few /books requests, so candidates need NO extra round-trip).
        if books is not None and token_id in books:
            asks = books[token_id].asks
        else:
            try:
                # Standalone fallback: reuse the pooled client if provided —
                # a fresh AsyncClient per bucket means a fresh TLS handshake each
                # time, which gets throttled and made a scan ~9s instead of <1s.
                if book_client is not None:
                    asks = await fetch_ask_levels(token_id, book_client)
                else:
                    async with httpx.AsyncClient(timeout=8.0) as client:
                        asks = await fetch_ask_levels(token_id, client)
            except Exception as e:
                logger.debug(f"Order-book walk failed for {market.market_id}: {e}")
        if asks:
            fill = walk_asks_for_cash(asks, suggested_size)
            if fill and fill.contracts > 0:
                # The book may be thinner than our Kelly cash; only stake what
                # actually fills against resting liquidity.
                if not fill.fully_filled:
                    suggested_size = round(fill.cash, 2)
                    fill_partial = True
                entry_price = min(0.999, fill.vwap)            # exact avg price paid
                # Cost is now the realized slippage over the side mid, plus fee;
                # net edge = our probability for the side minus what we really pay.
                cost = (entry_price - side_mid) + settings.WEATHER_FEE_RATE
                net_edge = edge - cost
                fill_levels = fill.levels
                fill_best_ask = fill.best_ask

    # Ensemble stats for display (show the bias correction we priced on)
    mean_val = forecast.mean_high if market.metric == "high" else forecast.mean_low
    std_val = forecast.std_high if market.metric == "high" else forecast.std_low
    bias = get_station_bias(market.city_key, market.metric)
    u = forecast.unit  # "F" or "C" — display in the market's native unit
    ensemble_str = f"{mean_val:.1f}{u}"
    if abs(bias) >= 0.05:
        ensemble_str = f"{mean_val:.1f}{u} (bias {bias:+.1f} -> {mean_val - bias:.1f}{u})"
    if observed_bound is not None:
        kind = "floor" if market.metric == "high" else "ceil"
        ensemble_str += f" [obs {kind} {observed_bound:.1f}{u}]"
    # Show the intraday sigma when the schedule is engaged (in-progress local day,
    # curve present) — so live-validation can watch evening highs get confident and
    # mornings stay appropriately unsure. ASCII only: this string is logged and stored,
    # and the Windows log/console encoding (cp1252) can't render a Greek sigma.
    if settings.WEATHER_INTRADAY_SIGMA_ENABLED and local_hour is not None:
        isig = intraday_sigma(market.city_key, market.metric, local_hour)
        if isig is not None:
            ensemble_str += f" [sigma {isig:.1f}{u} @{local_hour}h local]"

    # Market-gap guardrail: how far our (bias-corrected) mean sits from the market-
    # implied mean for this event. A large gap => we're likely the miscalibrated one.
    corrected_mean_val = mean_val - bias
    market_gap = None
    gap_threshold = settings.WEATHER_MAX_MARKET_GAP_F * ((1.0 / 1.8) if u == "C" else 1.0)
    if settings.WEATHER_MARKET_GAP_ENABLED and market.event_market_mean is not None:
        market_gap = abs(corrected_mean_val - market.event_market_mean)
    market_gap_ok = market_gap is None or market_gap <= gap_threshold

    # Build reasoning — mirror passes_threshold exactly so the recorded note
    # explains precisely why a bucket was or wasn't actionable.
    actionable = (net_edge >= settings.WEATHER_MIN_EDGE_THRESHOLD
                  and 0 < entry_price <= settings.WEATHER_MAX_ENTRY_PRICE
                  and market.liquidity >= settings.WEATHER_MIN_LIQUIDITY
                  and market.volume >= settings.WEATHER_MIN_VOLUME
                  and rel_spread <= settings.WEATHER_MAX_REL_SPREAD
                  and market_gap_ok)
    filter_notes = []
    if entry_price > settings.WEATHER_MAX_ENTRY_PRICE:
        filter_notes.append(f"entry {entry_price:.0%} > {settings.WEATHER_MAX_ENTRY_PRICE:.0%}")
    if net_edge < settings.WEATHER_MIN_EDGE_THRESHOLD:
        filter_notes.append(f"net edge {net_edge:.1%} < {settings.WEATHER_MIN_EDGE_THRESHOLD:.0%}")
    if market.liquidity < settings.WEATHER_MIN_LIQUIDITY:
        filter_notes.append(f"liq ${market.liquidity:.0f} < ${settings.WEATHER_MIN_LIQUIDITY:.0f}")
    if market.volume < settings.WEATHER_MIN_VOLUME:
        filter_notes.append(f"vol ${market.volume:.0f} < ${settings.WEATHER_MIN_VOLUME:.0f}")
    if rel_spread > settings.WEATHER_MAX_REL_SPREAD:
        filter_notes.append(f"rel-spread {rel_spread:.0%} > {settings.WEATHER_MAX_REL_SPREAD:.0%}")
    if not market_gap_ok:
        filter_notes.append(
            f"market-gap {market_gap:.1f}{u} > {gap_threshold:.1f}{u} "
            f"(ours {corrected_mean_val:.1f} vs mkt {market.event_market_mean:.1f})"
        )
    filter_note = f" [{', '.join(filter_notes)}]" if filter_notes else ""

    fill_note = ""
    if fill_levels:
        fill_note = (
            f" | fill VWAP {entry_price:.0%} over {fill_levels} lvl "
            f"(best ask {fill_best_ask:.0%}{', partial' if fill_partial else ''})"
        )

    reasoning = (
        f"[{'ACTIONABLE' if actionable else 'FILTERED'}]{filter_note} "
        f"{market.city_name} {market.metric} {market.bucket_label} on {market.target_date} | "
        f"Ensemble: {ensemble_str} +/- {std_val:.1f}{u} ({forecast.num_members} members) | "
        f"Model YES: {model_yes_prob:.0%} vs Market: {market_yes_prob:.0%} | "
        f"Edge: {edge:+.1%} -cost {cost:.1%} = net {net_edge:+.1%} -> {direction.upper()} @ {entry_price:.0%}"
        f"{fill_note}"
    )

    return WeatherTradingSignal(
        market=market,
        model_probability=model_yes_prob,
        market_probability=market_yes_prob,
        edge=edge,
        direction=direction,
        net_edge=net_edge,
        entry_price=entry_price,
        cost=cost,
        rel_spread=rel_spread,
        confidence=confidence,
        kelly_fraction=suggested_size / bankroll if bankroll > 0 else 0,
        suggested_size=suggested_size,
        sources=[f"open_meteo_ensemble_{forecast.num_members}m"],
        reasoning=reasoning,
        ensemble_mean=mean_val,
        ensemble_std=std_val,
        ensemble_members=forecast.num_members,
        market_gap=market_gap,
    )


def _current_bankroll() -> float:
    """Live bankroll from BotState, falling back to the configured starting value."""
    db = SessionLocal()
    try:
        from backend.models.database import BotState
        state = db.query(BotState).first()
        if state and state.bankroll and state.bankroll > 0:
            return float(state.bankroll)
    except Exception as e:
        logger.debug(f"Could not read live bankroll, using initial: {e}")
    finally:
        db.close()
    return settings.INITIAL_BANKROLL


async def scan_for_weather_signals() -> List[WeatherTradingSignal]:
    """
    Scan weather markets and generate ensemble-based signals.
    """
    signals = []

    bankroll = _current_bankroll()
    city_keys = [c.strip() for c in settings.WEATHER_CITIES.split(",") if c.strip()]

    logger.info("=" * 50)
    logger.info("WEATHER SCAN: Fetching temperature markets...")

    markets = []

    # Polymarket
    try:
        poly_markets = await fetch_polymarket_weather_markets(city_keys)
        markets.extend(poly_markets)
        logger.info(f"Polymarket: {len(poly_markets)} weather markets")
    except Exception as e:
        logger.error(f"Failed to fetch Polymarket weather markets: {e}")

    # Kalshi
    if settings.KALSHI_ENABLED:
        try:
            from backend.data.kalshi_client import kalshi_credentials_present
            from backend.data.kalshi_markets import fetch_kalshi_weather_markets
            if kalshi_credentials_present():
                kalshi_markets = await fetch_kalshi_weather_markets(city_keys)
                markets.extend(kalshi_markets)
                logger.info(f"Kalshi: {len(kalshi_markets)} weather markets")
        except Exception as e:
            logger.error(f"Failed to fetch Kalshi weather markets: {e}")

    logger.info(f"Found {len(markets)} total weather temperature markets")

    # Pre-warm the forecast cache (one call per unique city/date). Without this,
    # the concurrent pass below would stampede Open-Meteo with one request per
    # bucket on a cold cache.
    for city_key_, target_date_ in {(m.city_key, m.target_date) for m in markets}:
        try:
            await fetch_ensemble_forecast(city_key_, target_date_)
        except Exception:
            pass

    # Pre-warm the observed-so-far bound (one call per unique city/date/metric),
    # concurrently — so the per-bucket pass below hits a warm cache instead of
    # stampeding Meteostat. Only the combos that actually appear are fetched.
    obs_combos = {(m.city_key, m.target_date, m.metric) for m in markets}
    await asyncio.gather(
        *[fetch_observed_extreme(ck, mt, td) for (ck, td, mt) in obs_combos],
        return_exceptions=True,
    )

    # Generate signals concurrently (bounded), sharing ONE pooled HTTP client for
    # all the order-book fetches. Each candidate walks the live book; with the
    # forecasts pre-warmed and the client pooled this is a sub-second pass (a
    # fresh client per bucket was ~9s of repeated TLS handshakes).
    sem = asyncio.Semaphore(12)

    async with httpx.AsyncClient(
        timeout=8.0, limits=httpx.Limits(max_connections=20, max_keepalive_connections=20)
    ) as book_client:
        # Fetch every market's FULL live book up front in a few batched /books
        # requests (both YES and NO tokens — NO-side candidates walk the NO book).
        # This serves BOTH the edge screen (live mids, not Gamma's stale
        # outcomePrices) AND the exact-fill walk, so the per-bucket pass needs zero
        # extra round-trips. Per-token fetches were ~280+ requests (~50s, rate-
        # limited); batched it's ~3 requests (~2s).
        books: Dict[str, LiveBook] = {}
        try:
            token_ids = [t for m in markets for t in (m.token_id_yes, m.token_id_no) if t]
            books = await fetch_books(token_ids, book_client)
            refreshed = sum(
                _apply_live_top(m, books[m.token_id_yes].top)
                for m in markets if m.token_id_yes in books
            )
            logger.info(f"Live prices: refreshed {refreshed}/{len(markets)} markets from CLOB book")
        except Exception as e:
            logger.warning(f"Batch live-price refresh failed, using Gamma prices: {e}")

        # With live prices in hand, compute each event's market-implied mean so the
        # market-gap guardrail can suppress events where our forecast mean is far
        # from the market's (almost always us being miscalibrated, not free money).
        compute_event_market_means(markets)

        async def _gen(market: WeatherMarket) -> Optional[WeatherTradingSignal]:
            async with sem:
                try:
                    return await generate_weather_signal(
                        market, bankroll=bankroll, book_client=book_client,
                        refresh_prices=False, books=books,
                    )
                except Exception as e:
                    logger.debug(f"Weather signal generation failed for {market.title}: {e}")
                    return None

        results = await asyncio.gather(*[_gen(m) for m in markets])
    signals = [s for s in results if s]

    # Sort by absolute edge
    signals.sort(key=lambda s: abs(s.edge), reverse=True)

    actionable = [s for s in signals if s.passes_threshold]
    logger.info(f"WEATHER SCAN COMPLETE: {len(signals)} signals, {len(actionable)} actionable")

    for signal in actionable[:5]:
        logger.info(f"  {signal.market.city_name}: {signal.market.metric} "
                     f"{signal.market.bucket_label} | Edge: {signal.edge:+.1%}")

    # Persist signals to DB
    _persist_weather_signals(signals)

    # Cache the latest scan so read-only callers (the dashboard / API) can serve it
    # INSTANTLY instead of re-running a full scan per request (forecasts + order
    # books = tens of seconds, which was hanging the dashboard's loading screen).
    global _last_scan_signals, _last_scan_at
    _last_scan_signals = signals
    _last_scan_at = datetime.utcnow()

    return signals


# Latest scan result, for read-only API endpoints. The scheduler refreshes it
# every WEATHER_SCAN_INTERVAL; endpoints should READ this, never trigger a scan.
_last_scan_signals: List["WeatherTradingSignal"] = []
_last_scan_at: Optional[datetime] = None


def get_cached_signals() -> List["WeatherTradingSignal"]:
    """The most recent scan's signals (may be empty before the first scan)."""
    return _last_scan_signals


def _persist_weather_signals(signals: list):
    """Save weather signals for calibration/history.

    One row per market per UTC day: the row is updated in place as the day's
    scans refine it, then frozen once executed. This keeps a full record of every
    (non-dead) bucket — including filtered ones, so a bucket's edge can be seen to
    evolve and flip actionable — WITHOUT writing a new row every 5-min scan (which
    piled up ~22k rows/day). Dead rail buckets never reach here; the market reader
    already drops them. Note: the live bot never reads these rows to decide trades —
    every scan recomputes from scratch — so this is purely a record.
    """
    to_save = [s for s in signals if abs(s.edge) > 0]
    if not to_save:
        return

    db = SessionLocal()
    try:
        for signal in to_save:
            day_start = signal.timestamp.replace(hour=0, minute=0, second=0, microsecond=0)
            existing = db.query(Signal).filter(
                Signal.market_ticker == signal.market.market_id,
                Signal.market_type == "weather",
                Signal.timestamp >= day_start,
            ).order_by(Signal.timestamp.desc()).first()

            # Once a day's signal has been executed (a trade was placed off it),
            # freeze that trade-time snapshot — don't overwrite it with later scans.
            if existing and existing.executed:
                continue

            if existing is None:
                existing = Signal(
                    market_ticker=signal.market.market_id,
                    platform=signal.market.platform,
                    market_type="weather",
                    executed=False,
                )
                db.add(existing)

            # Upsert the latest snapshot for the day.
            existing.timestamp = signal.timestamp
            existing.direction = signal.direction
            existing.model_probability = signal.model_probability
            existing.market_price = signal.market_probability
            existing.edge = signal.edge
            existing.confidence = signal.confidence
            existing.net_edge = signal.net_edge
            existing.entry_price = signal.entry_price
            existing.cost = signal.cost
            existing.rel_spread = signal.rel_spread
            existing.liquidity = signal.market.liquidity
            existing.kelly_fraction = signal.kelly_fraction
            existing.suggested_size = signal.suggested_size
            existing.sources = signal.sources
            existing.reasoning = signal.reasoning

        db.commit()
    except Exception as e:
        logger.warning(f"Failed to persist weather signals: {e}")
        db.rollback()
    finally:
        db.close()
