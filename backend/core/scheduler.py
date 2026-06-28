"""Background scheduler for autonomous weather trading."""
import asyncio
from datetime import datetime, timedelta
from typing import List, Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import func
import logging

from backend.config import settings
from backend.models.database import SessionLocal, Trade, BotState, Signal

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("trading_bot")

# Global scheduler instance
scheduler: Optional[AsyncIOScheduler] = None

# Event log for terminal display (in-memory, last 200 events)
event_log: List[dict] = []
MAX_LOG_SIZE = 200


def log_event(event_type: str, message: str, data: dict = None):
    """Log an event for terminal display."""
    event = {
        "timestamp": datetime.utcnow().isoformat(),
        "type": event_type,
        "message": message,
        "data": data or {}
    }
    event_log.append(event)

    while len(event_log) > MAX_LOG_SIZE:
        event_log.pop(0)

    log_func = {
        "error": logger.error,
        "warning": logger.warning,
        "success": logger.info,
        "info": logger.info,
        "data": logger.debug,
        "trade": logger.info
    }.get(event_type, logger.info)

    log_func(f"[{event_type.upper()}] {message}")


def get_recent_events(limit: int = 50) -> List[dict]:
    """Get recent events for terminal display."""
    return event_log[-limit:]


async def weather_scan_and_trade_job():
    """
    Background job: Scan weather temperature markets, generate signals, execute trades.
    Runs every 5 minutes when WEATHER_ENABLED.
    """
    log_event("info", "Scanning weather temperature markets...")

    try:
        from backend.core.weather_signals import scan_for_weather_signals

        signals = await scan_for_weather_signals()
        actionable = [s for s in signals if s.passes_threshold]

        log_event("data", f"Weather: {len(signals)} signals, {len(actionable)} actionable", {
            "total_signals": len(signals),
            "actionable": len(actionable),
        })

        # --- MAKER path (gated). Instead of taking at the ask, POST resting limit orders
        # and let the day's flow fill them (the day-ahead edge is maker-only). The poll job
        # (maker_poll_job) advances fills + expiries and writes Trades. When the flag is off
        # this whole branch is skipped and the taker path below runs unchanged. ---
        if settings.WEATHER_MAKER_ENABLED:
            from backend.core.maker import place_maker_orders
            db = SessionLocal()
            try:
                posted = await place_maker_orders(db, signals)
                log_event("success" if posted else "info",
                          f"Maker: posted {posted} resting order(s)")
            except Exception as e:
                log_event("error", f"Maker place error: {str(e)}")
                logger.exception("Error placing maker orders")
            finally:
                db.close()
            return

        if not actionable:
            log_event("info", "No actionable weather signals")
            return

        db = SessionLocal()
        try:
            state = db.query(BotState).first()
            if not state:
                log_event("error", "Bot state not initialized")
                return

            if not state.is_running:
                log_event("info", "Bot is paused, skipping weather trades")
                return

            MAX_TRADES_PER_SCAN = 3
            # Sizing/exposure limits are fractions of the LIVE bankroll (so they scale
            # at any bankroll size — see config). Compute the dollar equivalents off it.
            min_trade_size = settings.WEATHER_MIN_TRADE_FRACTION * state.bankroll
            max_allocation = settings.WEATHER_MAX_ALLOCATION_FRACTION * state.bankroll  # max open weather exposure

            # Track remaining room under the allocation cap; we enforce it per
            # trade below so total open exposure never overshoots the cap.
            weather_pending = db.query(func.coalesce(func.sum(Trade.size), 0.0)).filter(
                Trade.settled == False,
                Trade.market_type == "weather",
            ).scalar()
            remaining_allocation = max_allocation - weather_pending

            if remaining_allocation < min_trade_size:
                log_event("info", f"Weather allocation full: ${weather_pending:.0f}/${max_allocation:.0f}")
                return

            # --- Daily loss circuit breaker (Phase 6: now guards weather too) ---
            today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            daily_pnl = db.query(func.coalesce(func.sum(Trade.pnl), 0.0)).filter(
                Trade.settled == True,
                Trade.settlement_time >= today_start,
            ).scalar()
            daily_loss_limit = settings.DAILY_LOSS_LIMIT_FRACTION * state.bankroll
            if daily_pnl <= -daily_loss_limit:
                log_event("warning",
                    f"Daily loss limit hit: ${daily_pnl:.2f} (limit: -${daily_loss_limit:.0f}). "
                    f"Stopping weather trades.")
                return

            # Correlated-risk + open-position caps. Buckets of the same city+day all
            # hinge on one forecast (and a city's high/low share an air mass), so cap
            # how much OPEN stake can ride on any single city+day; also enforce the
            # global cap on the number of open positions.
            max_city_day = settings.WEATHER_MAX_CITY_DAY_FRACTION * state.bankroll
            open_weather = db.query(Trade).filter(
                Trade.settled == False,
                Trade.market_type == "weather",
            ).all()
            open_count = len(open_weather)
            from collections import defaultdict
            from backend.data.weather_markets import parse_event_slug
            city_day_exposure = defaultdict(float)
            for t in open_weather:
                parsed = parse_event_slug(t.event_slug or "")
                if parsed:
                    ck, _m, td = parsed
                    city_day_exposure[(ck, td)] += float(t.size or 0.0)

            trades_executed = 0
            # Iterate ALL actionable signals (sorted by edge), skipping markets we
            # already hold, and place up to MAX_TRADES_PER_SCAN *new* trades.
            # (Previously this looked only at actionable[:MAX_TRADES_PER_SCAN]; once
            # the top-N by edge were all already held, it placed nothing and never
            # reached the other actionable buckets below them.)
            for signal in actionable:
                if trades_executed >= MAX_TRADES_PER_SCAN:
                    break
                if open_count >= settings.MAX_TOTAL_PENDING_TRADES:
                    log_event("info", f"Max open weather trades reached "
                              f"({open_count}/{settings.MAX_TOTAL_PENDING_TRADES})")
                    break

                # Skip markets we already have an open trade in.
                existing = db.query(Trade).filter(
                    Trade.market_ticker == signal.market.market_id,
                    Trade.settled == False,
                ).first()

                if existing:
                    continue

                # suggested_size is already capped to KELLY_MAX_TRADE_FRACTION of the
                # live bankroll in the signal generator; just enforce the minimum stake.
                trade_size = max(signal.suggested_size, min_trade_size)

                # Hard allocation ceiling: never let open weather exposure exceed
                # the cap. Trim to the remaining room; stop if there's no room left.
                if remaining_allocation < min_trade_size:
                    log_event("info", f"Weather allocation full: ${max_allocation - remaining_allocation:.0f}/${max_allocation:.0f}")
                    break
                if trade_size > remaining_allocation:
                    trade_size = remaining_allocation

                # Correlated-risk cap: limit total open stake on this city+day.
                cd_key = (signal.market.city_key, signal.market.target_date)
                cd_room = max_city_day - city_day_exposure[cd_key]
                if cd_room < min_trade_size:
                    continue   # this city+day is already at its cap; try another
                if trade_size > cd_room:
                    trade_size = cd_room

                if state.bankroll < min_trade_size:
                    log_event("warning", f"Bankroll too low: ${state.bankroll:.2f}")
                    break

                # Enter at the effective (cost-adjusted) ask, and book the fee.
                entry_price = signal.entry_price
                fee = settings.WEATHER_FEE_RATE * trade_size

                # Tag the scoreboard cohort: did this city+metric get a per-station
                # bias correction? (False for the uncorrected coastal cities.)
                from backend.data.weather import is_bias_corrected
                bias_corrected = is_bias_corrected(
                    signal.market.city_key, signal.market.metric)

                trade = Trade(
                    market_ticker=signal.market.market_id,
                    platform="polymarket",
                    event_slug=signal.market.slug,
                    market_type="weather",
                    bucket_label=signal.market.bucket_label,
                    direction=signal.direction,
                    entry_price=entry_price,
                    size=trade_size,
                    fee=fee,
                    model_probability=signal.model_probability,
                    market_price_at_entry=signal.market_probability,
                    edge_at_entry=signal.edge,
                    bias_corrected=bias_corrected,
                )

                db.add(trade)
                db.flush()

                # Link to signal record
                matching_signal = db.query(Signal).filter(
                    Signal.market_ticker == signal.market.market_id,
                    Signal.market_type == "weather",
                    Signal.executed == False,
                ).order_by(Signal.timestamp.desc()).first()
                if matching_signal:
                    matching_signal.executed = True
                    trade.signal_id = matching_signal.id

                state.total_trades += 1
                trades_executed += 1
                remaining_allocation -= trade_size
                city_day_exposure[cd_key] += trade_size
                open_count += 1

                log_event("trade",
                    f"WX {signal.market.city_name}: {signal.direction.upper()} "
                    f"${trade_size:.0f} @ {entry_price:.0%} | "
                    f"{signal.market.metric} {signal.market.bucket_label}",
                    {
                        "slug": signal.market.slug,
                        "direction": signal.direction,
                        "size": trade_size,
                        "edge": signal.edge,
                        "entry_price": entry_price,
                        "city": signal.market.city_name,
                    }
                )

            state.last_run = datetime.utcnow()
            db.commit()

            if trades_executed > 0:
                log_event("success", f"Executed {trades_executed} weather trade(s)")
            else:
                log_event("info", "No new weather trades executed")

        finally:
            db.close()

    except Exception as e:
        log_event("error", f"Weather scan error: {str(e)}")
        logger.exception("Error in weather_scan_and_trade_job")


async def maker_poll_job():
    """Advance resting maker orders against the live trade tape: fill what the flow hits, expire
    the stale, and write a Trade for whatever filled. Only scheduled when WEATHER_MAKER_ENABLED;
    runs on the (fast) maker poll cadence so day-ahead orders fill promptly as flow arrives."""
    try:
        from backend.core.maker import poll_maker_orders
        db = SessionLocal()
        try:
            filled = await poll_maker_orders(db)
            if filled:
                log_event("success", f"Maker: {filled} resting order(s) filled")
        finally:
            db.close()
    except Exception as e:
        log_event("error", f"Maker poll error: {str(e)}")
        logger.exception("Error in maker_poll_job")


async def settlement_job():
    """
    Background job: Check and settle pending trades.
    Runs on the settlement interval.
    """
    log_event("info", "Checking trade settlements...")

    try:
        from backend.core.settlement import settle_pending_trades, update_bot_state_with_settlements

        db = SessionLocal()
        try:
            pending_count = db.query(Trade).filter(Trade.settled == False).count()

            if pending_count == 0:
                log_event("data", "No pending trades to settle")
                return

            log_event("data", f"Processing {pending_count} pending trades")

            settled = await settle_pending_trades(db)

            if settled:
                await update_bot_state_with_settlements(db, settled)

                wins = sum(1 for t in settled if t.result == "win")
                losses = sum(1 for t in settled if t.result == "loss")
                total_pnl = sum(t.pnl for t in settled if t.pnl is not None)

                log_event("success", f"Settled {len(settled)} trades: {wins}W/{losses}L, P&L: ${total_pnl:.2f}", {
                    "settled_count": len(settled),
                    "wins": wins,
                    "losses": losses,
                    "pnl": total_pnl
                })

                for trade in settled:
                    result_prefix = "+" if trade.pnl and trade.pnl > 0 else ""
                    log_event("data", f"  {trade.event_slug}: {trade.result.upper()} {result_prefix}${trade.pnl:.2f}")
            else:
                log_event("info", "No trades ready for settlement")

        finally:
            db.close()

    except Exception as e:
        log_event("error", f"Settlement error: {str(e)}")
        logger.exception("Error in settlement_job")


async def heartbeat_job():
    """Periodic heartbeat. Runs every minute."""
    db = None
    try:
        db = SessionLocal()
        state = db.query(BotState).first()
        pending = db.query(Trade).filter(Trade.settled == False).count()

        if state is None:
            log_event("warning", "Heartbeat: Bot state not initialized")
            return

        log_event("data", f"Heartbeat: {pending} pending trades, bankroll: ${state.bankroll:.2f}", {
            "pending_trades": pending,
            "bankroll": state.bankroll,
            "is_running": state.is_running
        })
    except Exception as e:
        log_event("warning", f"Heartbeat failed: {str(e)}")
    finally:
        if db:
            db.close()


def start_scheduler():
    """Start the background scheduler for weather trading."""
    global scheduler

    if scheduler is not None and scheduler.running:
        log_event("warning", "Scheduler already running")
        return

    scheduler = AsyncIOScheduler()

    settle_seconds = settings.SETTLEMENT_INTERVAL_SECONDS

    # Check settlements on the settlement interval
    scheduler.add_job(
        settlement_job,
        IntervalTrigger(seconds=settle_seconds),
        id="settlement_check",
        replace_existing=True,
        max_instances=1
    )

    # Heartbeat every minute
    scheduler.add_job(
        heartbeat_job,
        IntervalTrigger(minutes=1),
        id="heartbeat",
        replace_existing=True,
        max_instances=1
    )

    # Weather trading jobs (gated by WEATHER_ENABLED)
    if settings.WEATHER_ENABLED:
        weather_scan_seconds = settings.WEATHER_SCAN_INTERVAL_SECONDS

        scheduler.add_job(
            weather_scan_and_trade_job,
            IntervalTrigger(seconds=weather_scan_seconds),
            id="weather_scan",
            replace_existing=True,
            max_instances=1,
        )

        # Maker poll job: advance/fill/expire resting limit orders on a fast cadence.
        # Only when the maker path is enabled (otherwise there are no working orders).
        if settings.WEATHER_MAKER_ENABLED:
            scheduler.add_job(
                maker_poll_job,
                IntervalTrigger(seconds=settings.WEATHER_MAKER_POLL_SECONDS),
                id="maker_poll",
                replace_existing=True,
                max_instances=1,
            )

    scheduler.start()
    log_event("success", "Weather trading scheduler started", {
        "settlement_interval": f"{settle_seconds}s",
        "weather_enabled": settings.WEATHER_ENABLED,
        "weather_min_edge": f"{settings.WEATHER_MIN_EDGE_THRESHOLD:.0%}",
    })

    if settings.WEATHER_ENABLED:
        asyncio.create_task(weather_scan_and_trade_job())


def stop_scheduler():
    """Stop the background scheduler."""
    global scheduler

    if scheduler is None or not scheduler.running:
        log_event("info", "Scheduler not running")
        return

    scheduler.shutdown(wait=False)
    scheduler = None
    log_event("info", "Scheduler stopped")


def is_scheduler_running() -> bool:
    """Check if scheduler is currently running."""
    return scheduler is not None and scheduler.running


async def run_manual_scan():
    """Trigger a manual weather market scan."""
    log_event("info", "Manual scan triggered")
    await weather_scan_and_trade_job()


async def run_manual_settlement():
    """Trigger a manual settlement check."""
    log_event("info", "Manual settlement triggered")
    await settlement_job()
