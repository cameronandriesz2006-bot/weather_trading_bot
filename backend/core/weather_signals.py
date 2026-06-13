"""Signal generator for weather temperature markets using ensemble forecasts."""
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from backend.config import settings
from backend.core.sizing import calculate_edge, calculate_kelly_size
from backend.data.weather import fetch_ensemble_forecast, EnsembleForecast, CITY_CONFIG
from backend.data.weather_markets import WeatherMarket, fetch_polymarket_weather_markets
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

    @property
    def passes_threshold(self) -> bool:
        """Actionable only if, after costs, the edge clears the threshold, the
        effective entry price is within the cap, AND the market is liquid enough
        with a tight-enough relative spread to actually trade (Layer 1)."""
        return (
            self.net_edge >= settings.WEATHER_MIN_EDGE_THRESHOLD
            and 0 < self.entry_price <= settings.WEATHER_MAX_ENTRY_PRICE
            and self.market.liquidity >= settings.WEATHER_MIN_LIQUIDITY
            and self.rel_spread <= settings.WEATHER_MAX_REL_SPREAD
        )


async def generate_weather_signal(
    market: WeatherMarket, bankroll: Optional[float] = None
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

    # Model YES probability = fraction of ensemble members whose daily high/low
    # lands in this market's temperature bucket.
    if market.metric == "high":
        model_yes_prob = forecast.probability_high_in_range(market.low_f, market.high_f)
    else:
        model_yes_prob = forecast.probability_low_in_range(market.low_f, market.high_f)

    # Light clip only to keep Kelly's odds math finite; do NOT inflate genuinely
    # tiny bucket probabilities (that would manufacture fake edges on dead buckets).
    model_yes_prob = max(0.01, min(0.99, model_yes_prob))

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
        suggested_size = min(suggested_size, settings.WEATHER_MAX_TRADE_SIZE)
        # Layer 2(i): never simulate taking more than a small slice of the book,
        # so we don't pretend to fill $75 into a $200 market.
        if market.liquidity and market.liquidity > 0:
            suggested_size = min(suggested_size, settings.WEATHER_MAX_BOOK_FRACTION * market.liquidity)
    else:
        suggested_size = 0.0

    # Ensemble stats for display
    mean_val = forecast.mean_high if market.metric == "high" else forecast.mean_low
    std_val = forecast.std_high if market.metric == "high" else forecast.std_low

    # Build reasoning — mirror passes_threshold exactly so the recorded note
    # explains precisely why a bucket was or wasn't actionable.
    actionable = (net_edge >= settings.WEATHER_MIN_EDGE_THRESHOLD
                  and 0 < entry_price <= settings.WEATHER_MAX_ENTRY_PRICE
                  and market.liquidity >= settings.WEATHER_MIN_LIQUIDITY
                  and rel_spread <= settings.WEATHER_MAX_REL_SPREAD)
    filter_notes = []
    if entry_price > settings.WEATHER_MAX_ENTRY_PRICE:
        filter_notes.append(f"entry {entry_price:.0%} > {settings.WEATHER_MAX_ENTRY_PRICE:.0%}")
    if net_edge < settings.WEATHER_MIN_EDGE_THRESHOLD:
        filter_notes.append(f"net edge {net_edge:.1%} < {settings.WEATHER_MIN_EDGE_THRESHOLD:.0%}")
    if market.liquidity < settings.WEATHER_MIN_LIQUIDITY:
        filter_notes.append(f"liq ${market.liquidity:.0f} < ${settings.WEATHER_MIN_LIQUIDITY:.0f}")
    if rel_spread > settings.WEATHER_MAX_REL_SPREAD:
        filter_notes.append(f"rel-spread {rel_spread:.0%} > {settings.WEATHER_MAX_REL_SPREAD:.0%}")
    filter_note = f" [{', '.join(filter_notes)}]" if filter_notes else ""

    reasoning = (
        f"[{'ACTIONABLE' if actionable else 'FILTERED'}]{filter_note} "
        f"{market.city_name} {market.metric} {market.bucket_label} on {market.target_date} | "
        f"Ensemble: {mean_val:.1f}F +/- {std_val:.1f}F ({forecast.num_members} members) | "
        f"Model YES: {model_yes_prob:.0%} vs Market: {market_yes_prob:.0%} | "
        f"Edge: {edge:+.1%} -cost {cost:.1%} = net {net_edge:+.1%} -> {direction.upper()} @ {entry_price:.0%}"
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

    for market in markets:
        try:
            signal = await generate_weather_signal(market, bankroll=bankroll)
            if signal:
                signals.append(signal)
        except Exception as e:
            logger.debug(f"Weather signal generation failed for {market.title}: {e}")

    # Sort by absolute edge
    signals.sort(key=lambda s: abs(s.edge), reverse=True)

    actionable = [s for s in signals if s.passes_threshold]
    logger.info(f"WEATHER SCAN COMPLETE: {len(signals)} signals, {len(actionable)} actionable")

    for signal in actionable[:5]:
        logger.info(f"  {signal.market.city_name}: {signal.market.metric} {signal.market.direction} "
                     f"{signal.market.threshold_f:.0f}F | Edge: {signal.edge:+.1%}")

    # Persist signals to DB
    _persist_weather_signals(signals)

    return signals


def _persist_weather_signals(signals: list):
    """Save weather signals to DB for calibration tracking."""
    to_save = [s for s in signals if abs(s.edge) > 0]
    if not to_save:
        return

    db = SessionLocal()
    try:
        for signal in to_save:
            # Dedup: skip if already logged for this market
            existing = db.query(Signal).filter(
                Signal.market_ticker == signal.market.market_id,
                Signal.timestamp >= signal.timestamp.replace(second=0, microsecond=0),
            ).first()
            if existing:
                continue

            db_signal = Signal(
                market_ticker=signal.market.market_id,
                platform=signal.market.platform,
                market_type="weather",
                timestamp=signal.timestamp,
                direction=signal.direction,
                model_probability=signal.model_probability,
                market_price=signal.market_probability,
                edge=signal.edge,
                confidence=signal.confidence,
                net_edge=signal.net_edge,
                entry_price=signal.entry_price,
                cost=signal.cost,
                rel_spread=signal.rel_spread,
                liquidity=signal.market.liquidity,
                kelly_fraction=signal.kelly_fraction,
                suggested_size=signal.suggested_size,
                sources=signal.sources,
                reasoning=signal.reasoning,
                executed=False,
            )
            db.add(db_signal)

        db.commit()
    except Exception as e:
        logger.warning(f"Failed to persist weather signals: {e}")
        db.rollback()
    finally:
        db.close()
