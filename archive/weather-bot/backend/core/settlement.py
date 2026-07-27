"""Trade settlement logic for BTC 5-min and weather markets using Polymarket API."""
import httpx
import json
import logging
from datetime import datetime, date
from typing import Optional, List, Tuple
from sqlalchemy.orm import Session

from backend.models.database import Trade, BotState, Signal
from backend.data.weather_markets import parse_event_slug, yes_index

logger = logging.getLogger("trading_bot")


async def fetch_polymarket_resolution(market_id: str, event_slug: Optional[str] = None) -> Tuple[bool, Optional[float]]:
    """
    Fetch actual market resolution from Polymarket API.

    For weather markets each event is a GROUP of mutually-exclusive bucket
    markets, so we must resolve against the SPECIFIC bucket we hold
    (``market_id``) — not ``markets[0]`` — or we'd grade every bucket in the
    event against the first one's outcome.

    Returns: (is_resolved, settlement_value)
        - settlement_value: 1.0 if Yes/Up won, 0.0 if No/Down won
    """
    # Polymarket leaves weather events ``closed: false`` for a while after the
    # day's outcome is already decided. Once the target local day is over we can
    # trust a price that has gone to the rails (~0.9995 / ~0.0005) as final.
    day_is_over = False
    if event_slug:
        parsed = parse_event_slug(event_slug)
        if parsed:
            _, _, target_date = parsed
            day_is_over = target_date < date.today()

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Find the exact bucket market inside its event by id.
            if event_slug:
                response = await client.get(
                    "https://gamma-api.polymarket.com/events",
                    params={"slug": event_slug}
                )
                response.raise_for_status()
                events = response.json()

                if events:
                    event = events[0] if isinstance(events, list) else events
                    for market in event.get("markets", []):
                        if str(market.get("id")) == str(market_id):
                            return _parse_market_resolution(market, day_is_over=day_is_over)

            # Fallback: try market ID directly
            url = f"https://gamma-api.polymarket.com/markets/{market_id}"
            response = await client.get(url)

            if response.status_code == 404:
                return await _search_market_in_events(market_id, day_is_over=day_is_over)

            response.raise_for_status()
            market = response.json()
            return _parse_market_resolution(market, day_is_over=day_is_over)

    except Exception as e:
        logger.warning(f"Failed to fetch resolution for {event_slug or market_id}: {e}")
        return False, None


async def _search_market_in_events(market_id: str, day_is_over: bool = False) -> Tuple[bool, Optional[float]]:
    """Search for market in events (both active and closed)."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            for closed in [True, False]:
                params = {
                    "closed": str(closed).lower(),
                    "limit": 200
                }
                response = await client.get(
                    "https://gamma-api.polymarket.com/events",
                    params=params
                )
                response.raise_for_status()
                events = response.json()

                for event in events:
                    for market in event.get("markets", []):
                        if str(market.get("id")) == str(market_id):
                            return _parse_market_resolution(market, day_is_over=day_is_over)

        return False, None

    except Exception as e:
        logger.warning(f"Failed to search for market {market_id}: {e}")
        return False, None


def _parse_market_resolution(market: dict, day_is_over: bool = False) -> Tuple[bool, Optional[float]]:
    """
    Parse market data to determine if resolved and the outcome.

    Handles both Yes/No and Up/Down outcomes:
    - outcomePrices[0] >= ~1.0 -> first outcome won (Yes or Up)
    - outcomePrices[0] <= ~0.0 -> second outcome won (No or Down)

    A market counts as resolved when EITHER:
      - Polymarket has flipped ``closed`` to True (always authoritative), OR
      - the target local day is over (``day_is_over``) AND the price has gone to
        the rails (>0.99 / <0.01). Polymarket leaves daily-temperature events
        ``closed: false`` for hours/days after the high/low is fixed, so without
        this second path settled trades never moved off the books.
    """
    is_closed = market.get("closed", False)

    outcome_prices = market.get("outcomePrices", [])
    if isinstance(outcome_prices, str):
        try:
            outcome_prices = json.loads(outcome_prices)
        except Exception:
            return False, None
    if not outcome_prices:
        return False, None

    # Read the YES price by the market's own outcome labels — a flipped
    # ["No","Yes"] market would otherwise be graded backwards.
    yi = yes_index(market)
    try:
        first_price = float(outcome_prices[yi])
    except (ValueError, IndexError, TypeError) as e:
        logger.warning(f"Failed to parse outcome prices: {e}")
        return False, None

    decisive = first_price > 0.99 or first_price < 0.01

    if not (is_closed or (day_is_over and decisive)):
        return False, None

    if first_price >= 0.5:
        logger.info(f"Market {market.get('id')} resolved: YES/UP won (price {first_price:.4f})")
        return True, 1.0
    else:
        logger.info(f"Market {market.get('id')} resolved: NO/DOWN won (price {first_price:.4f})")
        return True, 0.0


def calculate_pnl(trade: Trade, settlement_value: float) -> float:
    """
    Calculate P&L for a trade given the settlement value, NET of fees.

    `trade.size` is the CASH staked (dollars). On a prediction market you buy
    ``size / entry_price`` contracts at ``entry_price`` each, and each winning
    contract pays $1. So:
      - win:  profit = size * (1 - entry_price) / entry_price   (the net odds)
      - loss: you lose the full stake = -size

    settlement_value: 1.0 if Up/Yes outcome won, 0.0 if Down/No outcome won.

    Maps up->yes, down->no internally. The spread cost is already baked into
    trade.entry_price (we enter at the ask); trade.fee is subtracted here.
    """
    # Map up/down to yes/no logic
    direction = trade.direction
    if direction == "up":
        direction = "yes"
    elif direction == "down":
        direction = "no"

    won = (
        (direction == "yes" and settlement_value == 1.0)
        or (direction == "no" and settlement_value == 0.0)
    )

    price = trade.entry_price
    if won and 0.0 < price < 1.0:
        pnl = trade.size * (1.0 - price) / price
    elif won:
        pnl = 0.0           # bought at certainty: no upside
    else:
        pnl = -trade.size   # losing side: lose the full stake

    pnl -= (getattr(trade, "fee", 0.0) or 0.0)

    return round(pnl, 2)


def grade_signal_outcome(direction: str, settlement_value: float) -> Tuple[str, bool]:
    """
    Grade a signal's predicted direction against the settled outcome.

    Vocabulary-agnostic: weather signals store direction as "yes"/"no", while
    legacy BTC signals used "up"/"down". Both name the SAME two sides —
    "yes"/"up" = the first outcome, "no"/"down" = the second.

    settlement_value is the ground truth: 1.0 = first outcome won (YES/UP),
    0.0 = second outcome won (NO/DOWN).

    Returns (actual_outcome, outcome_correct), where actual_outcome is expressed
    in the SAME vocabulary as `direction` so the recorded value stays consistent.

    Note: this previously compared direction ("yes"/"no") directly against an
    actual_outcome built as "up"/"down", so every weather signal graded as wrong.
    """
    actual_first = settlement_value == 1.0
    predicted_first = direction in ("yes", "up")

    if direction in ("yes", "no"):
        actual_outcome = "yes" if actual_first else "no"
    else:
        actual_outcome = "up" if actual_first else "down"

    return actual_outcome, (predicted_first == actual_first)


async def check_market_settlement(trade: Trade) -> Tuple[bool, Optional[float], Optional[float]]:
    """
    Check if a trade's market has settled.

    Returns: (is_settled, settlement_value, pnl)
    """
    is_resolved, settlement_value = await fetch_polymarket_resolution(
        trade.market_ticker,
        event_slug=trade.event_slug
    )

    if not is_resolved or settlement_value is None:
        return False, None, None

    pnl = calculate_pnl(trade, settlement_value)

    mapped_dir = "UP" if trade.direction in ("up", "yes") else "DOWN"
    outcome = "UP" if settlement_value == 1.0 else "DOWN"
    result = "WIN" if mapped_dir == outcome else "LOSS"

    logger.info(f"Trade {trade.id} settled: {mapped_dir} @ {trade.entry_price:.0%} -> "
                f"{result} P&L: ${pnl:+.2f}")

    return True, settlement_value, pnl


async def check_weather_settlement(trade: Trade) -> Tuple[bool, Optional[float], Optional[float]]:
    """
    Check if a weather trade's market has settled.
    Routes to the correct platform's resolution method.
    """
    platform = getattr(trade, 'platform', 'polymarket') or 'polymarket'

    if platform == "kalshi":
        is_resolved, settlement_value = await _fetch_kalshi_resolution(trade.market_ticker)
    else:
        is_resolved, settlement_value = await fetch_polymarket_resolution(
            trade.market_ticker,
            event_slug=trade.event_slug,
        )

    if is_resolved and settlement_value is not None:
        pnl = calculate_pnl(trade, settlement_value)
        return True, settlement_value, pnl

    return False, None, None


async def _fetch_kalshi_resolution(ticker: str) -> Tuple[bool, Optional[float]]:
    """Fetch resolution status for a Kalshi market."""
    try:
        from backend.data.kalshi_client import KalshiClient, kalshi_credentials_present

        if not kalshi_credentials_present():
            return False, None

        client = KalshiClient()
        data = await client.get_market(ticker)
        market = data.get("market", data)

        status = market.get("status", "")
        result = market.get("result", "")

        if status in ("finalized", "determined") and result:
            if result == "yes":
                return True, 1.0
            elif result == "no":
                return True, 0.0

        return False, None

    except Exception as e:
        logger.warning(f"Failed to fetch Kalshi resolution for {ticker}: {e}")
        return False, None


async def settle_pending_trades(db: Session) -> List[Trade]:
    """
    Process all pending trades for settlement.
    Uses REAL market outcomes from Polymarket API.
    """
    try:
        pending = db.query(Trade).filter(Trade.settled == False).all()
    except Exception as e:
        logger.error(f"Failed to query pending trades: {e}")
        return []

    if not pending:
        logger.info("No pending trades to settle")
        return []

    logger.info(f"Checking {len(pending)} pending trades for settlement...")
    settled_trades = []

    for trade in pending:
        try:
            # Route settlement by market type
            market_type = getattr(trade, 'market_type', 'btc') or 'btc'
            if market_type == "weather":
                is_settled, settlement_value, pnl = await check_weather_settlement(trade)
            else:
                is_settled, settlement_value, pnl = await check_market_settlement(trade)

            if is_settled and settlement_value is not None:
                trade.settled = True
                trade.settlement_value = settlement_value
                trade.pnl = pnl
                trade.settlement_time = datetime.utcnow()

                if pnl is not None and pnl > 0:
                    trade.result = "win"
                elif pnl is not None and pnl < 0:
                    trade.result = "loss"
                else:
                    trade.result = "push"

                settled_trades.append(trade)

                # Update linked Signal with actual outcome for calibration
                if trade.signal_id:
                    linked_signal = db.query(Signal).filter(Signal.id == trade.signal_id).first()
                    if linked_signal:
                        actual_outcome, outcome_correct = grade_signal_outcome(
                            linked_signal.direction, settlement_value
                        )
                        linked_signal.actual_outcome = actual_outcome
                        linked_signal.outcome_correct = outcome_correct
                        linked_signal.settlement_value = settlement_value
                        linked_signal.settled_at = datetime.utcnow()
        except Exception as e:
            logger.error(f"Failed to settle trade {trade.id}: {e}")
            continue

    if settled_trades:
        try:
            db.commit()
            logger.info(f"Settled {len(settled_trades)} trades")
        except Exception as e:
            logger.error(f"Failed to commit settlements: {e}")
            db.rollback()
            return []
    else:
        logger.info("No trades ready for settlement (markets still open)")

    return settled_trades


async def update_bot_state_with_settlements(db: Session, settled_trades: List[Trade]) -> None:
    """Update bot state with P&L from settled trades."""
    if not settled_trades:
        return

    try:
        state = db.query(BotState).first()
        if not state:
            logger.warning("Bot state not found")
            return

        for trade in settled_trades:
            if trade.pnl is not None:
                state.total_pnl += trade.pnl
                state.bankroll += trade.pnl
                if trade.result == "win":
                    state.winning_trades += 1

        db.commit()
        logger.info(f"Updated bot state: Bankroll ${state.bankroll:.2f}, P&L ${state.total_pnl:+.2f}")
    except Exception as e:
        logger.error(f"Failed to update bot state: {e}")
        db.rollback()
