"""Maker path: post resting limit orders for actionable signals, fill them from real flow.

The taker path (scheduler.weather_scan_and_trade_job) writes a filled Trade immediately at the
ask VWAP. The MAKER path instead POSTS a resting limit order a tick above the bid (so we earn
the spread instead of paying it) and only writes a Trade for the amount that actually FILLS as
the day's flow trades into it. Unfilled orders auto-expire (GTD) and leave no Trade.

This is what makes the day-ahead edge harvestable: Stream-1 showed the resting depth a day out is
tiny (taker-unfriendly) but ~$1k/bucket of flow crosses over the day — capturable only as a
patient maker. It runs on backend/core/execution.py: simulated now against the REAL live tape
(CLOB book + Data API trades), swappable to the live Polymarket client to go live.

Two entry points, called by the scheduler when WEATHER_MAKER_ENABLED:
  * place_maker_orders(db, signals) — post resting orders for fresh actionable signals (mirrors
    the taker's allocation / city-day / daily-loss / max-pending caps, counting working orders as
    committed exposure so we never over-commit).
  * poll_maker_orders(db) — advance every open order against the trade tape, expire the stale
    ones, and write a Trade for whatever filled. Settlement + the scoreboard then treat that
    Trade exactly like any other filled position.

The simulator's per-order resting state (queue_ahead, last_poll_ts, filled-so-far) is persisted on
the WorkingOrder row, so each poll reconstructs the order, advances it, and writes it back — robust
across restarts. Both functions accept an injected ``executor`` for tests; otherwise they build a
SimulatedExecutor over LiveMarketData.
"""
import logging
import time
from datetime import datetime
from typing import List, Optional

import httpx
from sqlalchemy import func

from backend.config import settings
from backend.core.execution import (SimulatedExecutor, LiveMarketData, OrderManager, OrderSide,
                                     TimeInForce, OrderStatus, LimitOrder, Fill, shares_for_cash)
from backend.models.database import SessionLocal, Trade, BotState, Signal, WorkingOrder
from backend.data.weather import is_bias_corrected

logger = logging.getLogger("trading_bot")

_OPEN_STATUSES = ("OPEN", "PARTIALLY_FILLED")
MAX_ORDERS_PER_SCAN = 3


# --------------------------------------------------------------------------------------------
# Maker price: where to post the resting BUY for the side this signal favours
# --------------------------------------------------------------------------------------------
def maker_price(signal) -> Optional[tuple]:
    """Pick the resting BUY limit price for the side we favour, or None if we can't post a sane
    maker order. We improve the current best bid by one tick (queue priority) but never cross the
    ask (stay a maker) and never bid at/above our own fair value (keep the edge). Returns
    (token_id, price, side_fair). The NO side's book is the mirror of the YES book."""
    m = signal.market
    tick = settings.WEATHER_MAKER_TICK
    if signal.direction == "yes":
        token, bid, ask = m.token_id_yes, m.best_bid, m.best_ask
        side_fair = signal.model_probability
    else:
        token = m.token_id_no
        bid = (1.0 - m.best_ask) if m.best_ask is not None else None   # NO book = mirror of YES
        ask = (1.0 - m.best_bid) if m.best_bid is not None else None
        side_fair = 1.0 - signal.model_probability
    if token is None or bid is None or ask is None or not (0.0 < bid < ask < 1.0):
        return None
    # Post the highest non-crossing price that still carries edge: improve the bid by a tick when
    # the spread allows, otherwise JOIN the bid — never post BELOW it (that just forfeits queue
    # position for no gain). If improving would erase the edge (price >= our fair value) fall back
    # to the bid; if even the bid has no edge, skip. This is robust to sub-tick spreads (where
    # ask-tick can underflow below the bid) without needing the market's exact tick size.
    improve = min(bid + tick, ask - tick)
    post = improve if improve > bid else bid   # improve if we can, else join the bid
    if post >= side_fair:                      # improving killed the edge -> just join the bid
        post = bid
    if post >= side_fair:                      # even the bid carries no edge -> don't post
        return None
    price = round(post, 3)                     # Polymarket rounds to a valid tick on submit
    if price <= 0.0 or price >= ask:
        return None
    return token, price, side_fair


# --------------------------------------------------------------------------------------------
# Persist <-> reconstruct the simulator order state on the WorkingOrder row
# --------------------------------------------------------------------------------------------
def _reconstruct(row: WorkingOrder) -> LimitOrder:
    lo = LimitOrder(
        token_id=row.token_id, side=OrderSide.BUY, price=row.limit_price, size=row.size_shares,
        tif=TimeInForce(row.tif or "GTD"), expiration_ts=row.expiration_ts,
        order_id=row.order_id, created_ts=row.created_ts or 0.0,
    )
    lo._queue_ahead = row.queue_ahead or 0.0
    lo._last_poll_ts = row.last_poll_ts or row.created_ts or 0.0
    if row.filled_shares and row.filled_shares > 0:
        lo.fills.append(Fill(price=row.avg_fill_price or row.limit_price,
                             size=row.filled_shares, ts=lo._last_poll_ts, taker=False))
    try:
        lo.status = OrderStatus(row.status)
    except ValueError:
        lo.status = OrderStatus.OPEN
    return lo


def _persist_state(row: WorkingOrder, lo: LimitOrder) -> None:
    row.filled_shares = lo.filled_size
    row.avg_fill_price = lo.avg_fill_price
    row.queue_ahead = lo._queue_ahead
    row.last_poll_ts = lo._last_poll_ts
    row.status = lo.status.value


def _create_trade_from_fill(db, row: WorkingOrder, lo: LimitOrder, state: Optional[BotState]) -> Trade:
    """A working order that filled (fully or partially-then-expired) becomes ONE Trade for the
    filled portion, at the realized average fill price — identical in shape to a taker Trade, so
    settlement + the scoreboard handle it unchanged."""
    cash = lo.notional_filled                         # actual USDC spent on fills
    trade = Trade(
        market_ticker=row.market_ticker, platform="polymarket", event_slug=row.event_slug,
        market_type="weather", bucket_label=row.bucket_label, direction=row.direction,
        entry_price=lo.avg_fill_price, size=cash, fee=settings.WEATHER_FEE_RATE * cash,
        model_probability=row.model_probability, market_price_at_entry=row.market_price_at_entry,
        edge_at_entry=row.edge_at_entry, bias_corrected=row.bias_corrected,
    )
    db.add(trade)
    db.flush()
    row.trade_id = trade.id
    if row.signal_id:
        trade.signal_id = row.signal_id
    if state is not None:
        state.total_trades += 1
    logger.info(f"[MAKER] FILLED {row.city_key} {row.direction.upper()} {row.bucket_label}: "
                f"{lo.filled_size:.0f} sh @ {lo.avg_fill_price:.3f} = ${cash:.0f} "
                f"({lo.status.value})")
    return trade


# --------------------------------------------------------------------------------------------
# Exposure accounting (working orders count as committed, like open positions)
# --------------------------------------------------------------------------------------------
def _committed_and_counts(db):
    """Total committed weather exposure (open Trades' cash + open WorkingOrders' intended cash),
    the open count, and per-(city,day) exposure — so the maker respects the same caps as the
    taker and can't over-commit the bankroll across positions AND resting orders."""
    from collections import defaultdict
    from backend.data.weather_markets import parse_event_slug
    city_day = defaultdict(float)
    committed = 0.0
    count = 0
    for t in db.query(Trade).filter(Trade.settled == False, Trade.market_type == "weather").all():
        committed += float(t.size or 0.0)
        count += 1
        pe = parse_event_slug(t.event_slug or "")
        if pe:
            city_day[(pe[0], pe[2])] += float(t.size or 0.0)
    for w in db.query(WorkingOrder).filter(WorkingOrder.status.in_(_OPEN_STATUSES)).all():
        committed += float(w.intended_cash or 0.0)
        count += 1
        if w.city_key and w.target_date:
            try:
                from datetime import date
                city_day[(w.city_key, date.fromisoformat(w.target_date))] += float(w.intended_cash or 0.0)
            except ValueError:
                pass
    return committed, count, city_day


def _already_active(db, market_id: str) -> bool:
    """We already hold a filled position OR have a resting order in this exact market."""
    if db.query(Trade).filter(Trade.market_ticker == market_id, Trade.settled == False).first():
        return True
    if db.query(WorkingOrder).filter(WorkingOrder.market_ticker == market_id,
                                     WorkingOrder.status.in_(_OPEN_STATUSES)).first():
        return True
    return False


# --------------------------------------------------------------------------------------------
# Place
# --------------------------------------------------------------------------------------------
async def place_maker_orders(db, signals: List, executor: Optional[SimulatedExecutor] = None) -> int:
    """Post resting limit orders for fresh actionable signals (respecting the same caps as the
    taker path). Returns the number of orders posted."""
    state = db.query(BotState).first()
    if not state:
        return 0
    if not state.is_running:
        return 0

    actionable = [s for s in signals if s.passes_threshold]
    if not actionable:
        return 0

    # daily-loss breaker (same as the taker)
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    daily_pnl = db.query(func.coalesce(func.sum(Trade.pnl), 0.0)).filter(
        Trade.settled == True, Trade.settlement_time >= today_start).scalar()
    if daily_pnl <= -settings.DAILY_LOSS_LIMIT_FRACTION * state.bankroll:
        logger.info(f"[MAKER] daily loss limit hit (${daily_pnl:.0f}); not posting")
        return 0

    if executor is not None:
        return await _place(db, state, actionable, executor)
    cond = {}
    for s in actionable:
        if s.market.condition_id:
            cond[s.market.token_id_yes] = s.market.condition_id
            cond[s.market.token_id_no] = s.market.condition_id
    async with httpx.AsyncClient(timeout=10.0) as client:
        ex = SimulatedExecutor(LiveMarketData(client, cond))
        return await _place(db, state, actionable, ex)


async def _place(db, state, actionable, ex) -> int:
    min_trade = settings.WEATHER_MIN_TRADE_FRACTION * state.bankroll
    max_allocation = settings.WEATHER_MAX_ALLOCATION_FRACTION * state.bankroll
    max_city_day = settings.WEATHER_MAX_CITY_DAY_FRACTION * state.bankroll
    committed, open_count, city_day = _committed_and_counts(db)
    remaining = max_allocation - committed

    placed = 0
    for signal in actionable:
        if placed >= MAX_ORDERS_PER_SCAN or open_count >= settings.MAX_TOTAL_PENDING_TRADES:
            break
        if remaining < min_trade:
            break
        if _already_active(db, signal.market.market_id):
            continue
        priced = maker_price(signal)
        if priced is None:
            continue
        token, price, _side_fair = priced

        size = max(signal.suggested_size, min_trade)
        size = min(size, remaining)
        cd_key = (signal.market.city_key, signal.market.target_date)
        cd_room = max_city_day - city_day[cd_key]
        if cd_room < min_trade:
            continue
        size = min(size, cd_room)
        if size < min_trade:
            continue
        shares = shares_for_cash(size, price)
        if shares <= 0:
            continue

        try:
            order = await ex.place_order(token, OrderSide.BUY, price, shares,
                                         tif=TimeInForce.GTD,
                                         ttl_seconds=settings.WEATHER_MAKER_TTL_SECONDS)
        except Exception as e:
            logger.debug(f"[MAKER] place failed for {signal.market.market_id}: {e}")
            continue

        # link to the signal record (freeze it as executed, like the taker)
        sig = db.query(Signal).filter(
            Signal.market_ticker == signal.market.market_id, Signal.market_type == "weather",
            Signal.executed == False).order_by(Signal.timestamp.desc()).first()

        bias_corrected = is_bias_corrected(signal.market.city_key, signal.market.metric)
        row = WorkingOrder(
            order_id=order.order_id, market_ticker=signal.market.market_id,
            event_slug=signal.market.slug, bucket_label=signal.market.bucket_label,
            city_key=signal.market.city_key, metric=signal.market.metric,
            target_date=signal.market.target_date.isoformat(), token_id=token,
            condition_id=signal.market.condition_id, direction=signal.direction,
            limit_price=price, size_shares=shares, intended_cash=size, tif=order.tif.value,
            created_ts=order.created_ts, expiration_ts=order.expiration_ts,
            status=order.status.value, filled_shares=order.filled_size,
            avg_fill_price=order.avg_fill_price, queue_ahead=order._queue_ahead,
            last_poll_ts=order._last_poll_ts or order.created_ts,
            model_probability=signal.model_probability,
            market_price_at_entry=signal.market_probability, edge_at_entry=signal.edge,
            bias_corrected=bias_corrected, signal_id=sig.id if sig else None,
        )
        db.add(row)
        db.flush()
        if sig:
            sig.executed = True
        # rare: a posted order that immediately crossed (book moved) -> book the fill now
        if order.is_terminal and order.filled_size > 1e-9:
            _create_trade_from_fill(db, row, order, state)

        remaining -= size
        city_day[cd_key] += size
        open_count += 1
        placed += 1
        logger.info(f"[MAKER] POSTED {signal.market.city_name} {signal.direction.upper()} "
                    f"{signal.market.bucket_label}: {shares:.0f} sh @ {price:.3f} "
                    f"(${size:.0f}, ttl {settings.WEATHER_MAKER_TTL_SECONDS}s)")

    db.commit()
    return placed


# --------------------------------------------------------------------------------------------
# Poll
# --------------------------------------------------------------------------------------------
async def poll_maker_orders(db, executor: Optional[SimulatedExecutor] = None) -> int:
    """Advance every open working order against the real trade tape, expire stale ones, and write
    a Trade for whatever filled. Returns the number of orders that became filled this poll."""
    rows = db.query(WorkingOrder).filter(WorkingOrder.status.in_(_OPEN_STATUSES)).all()
    if not rows:
        return 0
    if executor is not None:
        return await _poll(db, rows, executor)
    cond = {r.token_id: r.condition_id for r in rows if r.condition_id and r.token_id}
    async with httpx.AsyncClient(timeout=10.0) as client:
        ex = SimulatedExecutor(LiveMarketData(client, cond))
        return await _poll(db, rows, ex)


async def _poll(db, rows, ex) -> int:
    state = db.query(BotState).first()
    newly_filled = 0
    for row in rows:
        lo = _reconstruct(row)
        try:
            await ex.poll_order(lo)
        except Exception as e:
            logger.debug(f"[MAKER] poll failed for {row.order_id}: {e}")
            continue
        _persist_state(row, lo)
        if lo.is_terminal and row.trade_id is None and lo.filled_size > 1e-9:
            _create_trade_from_fill(db, row, lo, state)
            newly_filled += 1
    db.commit()
    return newly_filled
