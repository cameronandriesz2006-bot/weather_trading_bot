"""FastAPI backend for the weather trading bot dashboard."""
from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from typing import List, Optional
import asyncio
import json
import os
import time
import httpx

from backend.config import settings
from backend.models.database import (
    get_db, init_db, SessionLocal,
    Signal, Trade, BotState,
)

from pydantic import BaseModel

app = FastAPI(
    title="Weather Trading Bot",
    description="Kalshi + Polymarket weather temperature market trading bot",
    version="3.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# WebSocket connection manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                pass


ws_manager = ConnectionManager()


# Pydantic response models
class TradeResponse(BaseModel):
    id: int
    market_ticker: str
    platform: str
    event_slug: Optional[str] = None
    direction: str
    entry_price: float
    size: float
    timestamp: datetime
    settled: bool
    result: str
    pnl: Optional[float]
    # Readable market identity + settlement info (dashboard trades panel).
    bucket_label: Optional[str] = None
    city_name: Optional[str] = None
    metric: Optional[str] = None
    target_date: Optional[str] = None
    settlement_time: Optional[datetime] = None
    market_type: Optional[str] = None
    # Mark-to-market (display only): current price of the side we hold + unrealized P&L
    current_price: Optional[float] = None
    unrealized_pnl: Optional[float] = None


class BotStats(BaseModel):
    bankroll: float
    total_trades: int
    winning_trades: int
    win_rate: float
    total_pnl: float
    is_running: bool
    last_run: Optional[datetime]
    settled_trades: int = 0   # number of trades that have actually resolved
    # Risk / sizing context for the dashboard (read from config so the UI never
    # hard-codes the cap). daily_pnl is today's REALIZED P&L (UTC day); the
    # circuit breaker stops weather trading once it hits -daily_loss_limit.
    weather_max_allocation: float = 0.0
    daily_loss_limit: float = 0.0
    daily_pnl: float = 0.0


class CalibrationBucket(BaseModel):
    bucket: str
    predicted_avg: float
    actual_rate: float
    count: int


class CalibrationSummary(BaseModel):
    total_signals: int
    total_with_outcome: int
    accuracy: float
    avg_predicted_edge: float
    avg_actual_edge: float
    brier_score: float


class WeatherForecastResponse(BaseModel):
    city_key: str
    city_name: str
    target_date: str
    mean_high: float
    std_high: float
    mean_low: float
    std_low: float
    num_members: int
    ensemble_agreement: float


class WeatherMarketResponse(BaseModel):
    slug: str
    market_id: str
    platform: str = "polymarket"
    title: str
    city_key: str
    city_name: str
    target_date: str
    threshold_f: float
    metric: str
    direction: str
    yes_price: float
    no_price: float
    volume: float


class WeatherSignalResponse(BaseModel):
    market_id: str
    city_key: str
    city_name: str
    target_date: str
    threshold_f: float
    metric: str
    direction: str
    model_probability: float
    market_probability: float
    edge: float
    confidence: float
    suggested_size: float
    reasoning: str
    ensemble_mean: float
    ensemble_std: float
    ensemble_members: int
    actionable: bool = False
    # Identify the exact market + the cost-aware economics (dashboard redesign).
    slug: str = ""                      # event slug -> https://polymarket.com/event/<slug>
    bucket_label: str = ""              # the real range, e.g. "88-89°F" or "18°C"
    unit: str = "F"                     # native unit of this market ("F" US, "C" intl)
    low_f: Optional[float] = None
    high_f: Optional[float] = None
    net_edge: float = 0.0               # edge after costs (what we gate/size on)
    entry_price: float = 0.0            # effective price we'd pay (real ask)
    cost: float = 0.0                   # per-share cost (spread/2 + fee)
    rel_spread: float = 0.0             # spread as a fraction of the side price
    liquidity: float = 0.0              # $ resting in the book
    spread: float = 0.0
    yes_price: float = 0.0
    no_price: float = 0.0
    bias: float = 0.0                   # per-station bias applied (subtracted from mean)


class DashboardData(BaseModel):
    stats: BotStats
    recent_trades: List[TradeResponse]
    equity_curve: List[dict]
    calibration: Optional[CalibrationSummary] = None
    weather_signals: List[WeatherSignalResponse] = []
    weather_forecasts: List[WeatherForecastResponse] = []


class EventResponse(BaseModel):
    timestamp: str
    type: str
    message: str
    data: dict = {}


# Startup / Shutdown
@app.on_event("startup")
async def startup():
    print("=" * 60)
    print("WEATHER TRADING BOT v3.0")
    print("=" * 60)
    print("Initializing database...")

    init_db()

    db = SessionLocal()
    try:
        state = db.query(BotState).first()
        if not state:
            state = BotState(
                bankroll=settings.INITIAL_BANKROLL,
                total_trades=0,
                winning_trades=0,
                total_pnl=0.0,
                is_running=True
            )
            db.add(state)
            db.commit()
            print(f"Created new bot state with ${settings.INITIAL_BANKROLL:,.2f} bankroll")
        else:
            state.is_running = True
            db.commit()
            print(f"Loaded bot state: Bankroll ${state.bankroll:,.2f}, P&L ${state.total_pnl:+,.2f}, {state.total_trades} trades")
    finally:
        db.close()

    print("")
    print("Configuration:")
    print(f"  - Simulation mode: {settings.SIMULATION_MODE}")
    print(f"  - Kelly fraction: {settings.KELLY_FRACTION:.0%}")
    print(f"  - Settlement interval: {settings.SETTLEMENT_INTERVAL_SECONDS}s")
    print("")

    from backend.core.scheduler import start_scheduler, log_event
    start_scheduler()
    log_event("success", "Weather trading bot initialized")

    print("Bot is now running!")
    print(f"  - Settlement check: every {settings.SETTLEMENT_INTERVAL_SECONDS}s")
    if settings.WEATHER_ENABLED:
        print(f"  - Weather scan: every {settings.WEATHER_SCAN_INTERVAL_SECONDS}s (edge >= {settings.WEATHER_MIN_EDGE_THRESHOLD:.0%})")
        print(f"  - Weather cities: {settings.WEATHER_CITIES}")
    else:
        print("  - Weather trading: DISABLED")
    print("=" * 60)


@app.on_event("shutdown")
async def shutdown():
    from backend.core.scheduler import stop_scheduler
    stop_scheduler()


# Core endpoints
@app.get("/")
async def root():
    return {"status": "ok", "message": "Weather Trading Bot API v3.0", "simulation_mode": settings.SIMULATION_MODE}


@app.get("/api/health")
async def health():
    return {"status": "healthy"}


@app.get("/api/stats", response_model=BotStats)
async def get_stats(db: Session = Depends(get_db)):
    from sqlalchemy import func

    state = db.query(BotState).first()
    if not state:
        raise HTTPException(status_code=404, detail="Bot state not initialized")

    # Win rate is over RESOLVED trades only — dividing by total_trades (which
    # includes still-open positions) understates it while trades are pending.
    settled_trades = db.query(Trade).filter(Trade.settled == True).count()
    win_rate = state.winning_trades / settled_trades if settled_trades > 0 else 0

    # Today's realized P&L (UTC day) — same window the circuit breaker uses.
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    daily_pnl = db.query(func.coalesce(func.sum(Trade.pnl), 0.0)).filter(
        Trade.settled == True,
        Trade.settlement_time >= today_start,
    ).scalar() or 0.0

    return BotStats(
        bankroll=state.bankroll,
        total_trades=state.total_trades,
        winning_trades=state.winning_trades,
        win_rate=win_rate,
        total_pnl=state.total_pnl,
        is_running=state.is_running,
        last_run=state.last_run,
        settled_trades=settled_trades,
        weather_max_allocation=settings.WEATHER_MAX_ALLOCATION_FRACTION * state.bankroll,
        daily_loss_limit=settings.DAILY_LOSS_LIMIT_FRACTION * state.bankroll,
        daily_pnl=float(daily_pnl),
    )


@app.get("/api/trades", response_model=List[TradeResponse])
async def get_trades(
    limit: int = 50,
    status: Optional[str] = None,
    db: Session = Depends(get_db)
):
    query = db.query(Trade)
    if status:
        query = query.filter(Trade.result == status)
    trades = query.order_by(Trade.timestamp.desc()).limit(limit).all()

    return [_trade_to_response(t) for t in trades]


@app.get("/api/equity-curve")
async def get_equity_curve(db: Session = Depends(get_db)):
    trades = db.query(Trade).filter(Trade.settled == True).order_by(Trade.timestamp).all()

    curve = []
    cumulative_pnl = 0
    bankroll = settings.INITIAL_BANKROLL

    for trade in trades:
        if trade.pnl is not None:
            cumulative_pnl += trade.pnl
            curve.append({
                "timestamp": trade.timestamp.isoformat(),
                "pnl": cumulative_pnl,
                "bankroll": bankroll + cumulative_pnl,
                "trade_id": trade.id
            })

    return curve


@app.post("/api/run-scan")
async def run_scan(db: Session = Depends(get_db)):
    from backend.core.scheduler import run_manual_scan, log_event

    state = db.query(BotState).first()
    if state:
        state.last_run = datetime.utcnow()
        db.commit()

    log_event("info", "Manual weather scan triggered")
    await run_manual_scan()

    result = {
        "status": "ok",
        "total_signals": 0,
        "actionable_signals": 0,
        "timestamp": datetime.utcnow().isoformat(),
    }

    # Run weather scan if enabled
    if settings.WEATHER_ENABLED:
        try:
            from backend.core.weather_signals import scan_for_weather_signals
            wx_signals = await scan_for_weather_signals()
            wx_actionable = [s for s in wx_signals if s.passes_threshold]
            result["weather_signals"] = len(wx_signals)
            result["weather_actionable"] = len(wx_actionable)
        except Exception:
            result["weather_signals"] = 0
            result["weather_actionable"] = 0

    return result


@app.post("/api/settle-trades")
async def settle_trades_endpoint(db: Session = Depends(get_db)):
    from backend.core.settlement import settle_pending_trades, update_bot_state_with_settlements
    from backend.core.scheduler import log_event

    log_event("info", "Manual settlement triggered")

    settled = await settle_pending_trades(db)
    await update_bot_state_with_settlements(db, settled)

    return {
        "status": "ok",
        "settled_count": len(settled),
        "trades": [{"id": t.id, "result": t.result, "pnl": t.pnl} for t in settled]
    }


def _compute_calibration_summary(db: Session) -> Optional[CalibrationSummary]:
    """Compute calibration summary from settled signals."""
    total_signals = db.query(Signal).count()
    settled_signals = db.query(Signal).filter(Signal.outcome_correct.isnot(None)).all()

    if not settled_signals:
        if total_signals == 0:
            return None
        return CalibrationSummary(
            total_signals=total_signals,
            total_with_outcome=0,
            accuracy=0.0,
            avg_predicted_edge=0.0,
            avg_actual_edge=0.0,
            brier_score=0.0,
        )

    total_with_outcome = len(settled_signals)
    correct = sum(1 for s in settled_signals if s.outcome_correct)
    accuracy = correct / total_with_outcome if total_with_outcome > 0 else 0.0

    avg_predicted_edge = sum(abs(s.edge) for s in settled_signals) / total_with_outcome
    # Actual edge: for correct predictions, edge was real; for incorrect, edge was negative
    avg_actual_edge = sum(
        abs(s.edge) if s.outcome_correct else -abs(s.edge)
        for s in settled_signals
    ) / total_with_outcome

    # Brier score: mean squared error of probability forecasts
    # For each signal: (predicted_prob - actual_outcome)^2
    brier_sum = 0.0
    for s in settled_signals:
        # Model probability is for UP; actual is 1.0 if UP won, 0.0 if DOWN won
        actual = s.settlement_value if s.settlement_value is not None else 0.5
        brier_sum += (s.model_probability - actual) ** 2
    brier_score = brier_sum / total_with_outcome

    return CalibrationSummary(
        total_signals=total_signals,
        total_with_outcome=total_with_outcome,
        accuracy=accuracy,
        avg_predicted_edge=avg_predicted_edge,
        avg_actual_edge=avg_actual_edge,
        brier_score=brier_score,
    )


@app.get("/api/calibration")
async def get_calibration(db: Session = Depends(get_db)):
    """Return calibration data: predicted probability vs actual win rate."""
    signals = db.query(Signal).filter(Signal.outcome_correct.isnot(None)).all()

    if not signals:
        return {"buckets": [], "summary": None}

    # Bucket signals by model_probability into 5% bins
    from collections import defaultdict
    buckets_data = defaultdict(lambda: {"predicted_sum": 0.0, "correct": 0, "total": 0})

    for s in signals:
        # Bin by 5% increments
        bin_start = int(s.model_probability * 100 // 5) * 5
        bin_end = bin_start + 5
        bucket_key = f"{bin_start}-{bin_end}%"

        buckets_data[bucket_key]["predicted_sum"] += s.model_probability
        buckets_data[bucket_key]["total"] += 1
        if s.outcome_correct:
            buckets_data[bucket_key]["correct"] += 1

    buckets = []
    for bucket_key in sorted(buckets_data.keys()):
        d = buckets_data[bucket_key]
        buckets.append(CalibrationBucket(
            bucket=bucket_key,
            predicted_avg=d["predicted_sum"] / d["total"],
            actual_rate=d["correct"] / d["total"],
            count=d["total"],
        ))

    summary = _compute_calibration_summary(db)

    return {"buckets": buckets, "summary": summary}


# Kalshi endpoints
@app.get("/api/kalshi/status")
async def get_kalshi_status():
    """Test Kalshi API authentication and return connection status."""
    from backend.data.kalshi_client import KalshiClient, kalshi_credentials_present

    if not kalshi_credentials_present():
        return {
            "connected": False,
            "error": "Kalshi credentials not configured (KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH)",
        }

    try:
        client = KalshiClient()
        balance_data = await client.get_balance()
        return {
            "connected": True,
            "balance": balance_data,
        }
    except Exception as e:
        return {
            "connected": False,
            "error": str(e),
        }


# Weather endpoints
@app.get("/api/weather/forecasts", response_model=List[WeatherForecastResponse])
async def get_weather_forecasts():
    """Get ensemble forecasts for configured cities."""
    if not settings.WEATHER_ENABLED:
        return []

    try:
        from backend.data.weather import fetch_ensemble_forecast, CITY_CONFIG
        from datetime import date

        city_keys = [c.strip() for c in settings.WEATHER_CITIES.split(",") if c.strip()]
        forecasts = []

        for city_key in city_keys:
            if city_key not in CITY_CONFIG:
                continue
            forecast = await fetch_ensemble_forecast(city_key)
            if forecast:
                forecasts.append(WeatherForecastResponse(
                    city_key=forecast.city_key,
                    city_name=forecast.city_name,
                    target_date=forecast.target_date.isoformat(),
                    mean_high=forecast.mean_high,
                    std_high=forecast.std_high,
                    mean_low=forecast.mean_low,
                    std_low=forecast.std_low,
                    num_members=forecast.num_members,
                    ensemble_agreement=forecast.ensemble_agreement,
                ))

        return forecasts
    except Exception:
        return []


@app.get("/api/weather/markets", response_model=List[WeatherMarketResponse])
async def get_weather_markets():
    """Get active weather temperature markets."""
    if not settings.WEATHER_ENABLED:
        return []

    try:
        from backend.data.weather_markets import fetch_polymarket_weather_markets

        city_keys = [c.strip() for c in settings.WEATHER_CITIES.split(",") if c.strip()]
        markets = await fetch_polymarket_weather_markets(city_keys)

        # Also fetch Kalshi markets if enabled
        if settings.KALSHI_ENABLED:
            try:
                from backend.data.kalshi_client import kalshi_credentials_present
                from backend.data.kalshi_markets import fetch_kalshi_weather_markets
                if kalshi_credentials_present():
                    kalshi_markets = await fetch_kalshi_weather_markets(city_keys)
                    markets.extend(kalshi_markets)
            except Exception:
                pass

        return [
            WeatherMarketResponse(
                slug=m.slug,
                market_id=m.market_id,
                platform=m.platform,
                title=m.title,
                city_key=m.city_key,
                city_name=m.city_name,
                target_date=m.target_date.isoformat(),
                threshold_f=m.threshold_f,
                metric=m.metric,
                direction=m.direction,
                yes_price=m.yes_price,
                no_price=m.no_price,
                volume=m.volume,
            )
            for m in markets
        ]
    except Exception:
        return []


@app.get("/api/weather/signals", response_model=List[WeatherSignalResponse])
async def get_weather_signals():
    """Get current weather trading signals."""
    if not settings.WEATHER_ENABLED:
        return []

    try:
        from backend.core.weather_signals import get_cached_signals

        # Serve the latest scheduled scan (instant); never trigger a scan per request.
        signals = get_cached_signals()
        return [_weather_signal_to_response(s) for s in signals]
    except Exception:
        return []


def _trade_to_response(t, current_price: Optional[float] = None) -> TradeResponse:
    """Serialize a Trade, deriving readable market fields from the event slug.

    current_price (the live price of the side we hold) enables a mark-to-market
    unrealized P&L for open positions: what we'd realize if we sold now, using
    the same cash-staked odds as settlement (ignores exit spread/fees).
    """
    from backend.data.weather_markets import parse_event_slug
    from backend.data.weather import CITY_CONFIG
    city_name = None
    metric = None
    target_date = None
    parsed = parse_event_slug(t.event_slug or "")
    if parsed:
        city_key, metric, td = parsed
        city_name = CITY_CONFIG.get(city_key, {}).get("name", city_key)
        target_date = td.isoformat()

    unrealized = None
    if current_price is not None and not t.settled and t.entry_price and 0 < t.entry_price < 1:
        unrealized = round(t.size * (current_price - t.entry_price) / t.entry_price, 2)

    return TradeResponse(
        id=t.id,
        market_ticker=t.market_ticker,
        platform=t.platform,
        event_slug=t.event_slug,
        direction=t.direction,
        entry_price=t.entry_price,
        size=t.size,
        timestamp=t.timestamp,
        settled=t.settled,
        result=t.result,
        pnl=t.pnl,
        bucket_label=getattr(t, "bucket_label", None),
        city_name=city_name,
        metric=metric,
        target_date=target_date,
        settlement_time=getattr(t, "settlement_time", None),
        market_type=getattr(t, "market_type", None),
        current_price=current_price,
        unrealized_pnl=unrealized,
    )


# --- Mark-to-market price lookup (cached) ---------------------------------
_mtm_cache: dict = {}   # market_id -> (ts, (yes_price, no_price))
_MTM_TTL = 30.0


async def _fetch_outcome_prices(client: httpx.AsyncClient, market_id: str):
    """(yes, no) current prices for a Polymarket market id, cached briefly.

    Marks off the LIVE CLOB book (mid), not Gamma's ``outcomePrices``: those
    cached Gamma fields can be ~20c stale on thin daily-temperature markets (the
    same reason the signal generator walks the live book for fills). We look up
    the market's two outcome tokens (``clobTokenIds``) and read each token's live
    top-of-book mid. Falls back to Gamma ``outcomePrices`` only if the book is
    unavailable.
    """
    from backend.data.orderbook import fetch_book_top

    now = time.time()
    cached = _mtm_cache.get(market_id)
    if cached and now - cached[0] < _MTM_TTL:
        return cached[1]
    try:
        r = await client.get(f"https://gamma-api.polymarket.com/markets/{market_id}")
        if r.status_code != 200:
            return None
        data = r.json()
        pair = None

        # Preferred: live CLOB book mid for each outcome token.
        tids = data.get("clobTokenIds")
        if isinstance(tids, str):
            tids = json.loads(tids)
        if tids and len(tids) >= 2:
            yes_top = await fetch_book_top(tids[0], client)
            no_top = await fetch_book_top(tids[1], client)
            if yes_top or no_top:
                # The two sides are complementary (yes_mid ≈ 1 - no_mid); if one
                # book is empty, derive it from the other so we always show both.
                yes_mid = yes_top.mid if yes_top else (1.0 - no_top.mid)
                no_mid = no_top.mid if no_top else (1.0 - yes_top.mid)
                pair = (yes_mid, no_mid)

        # Fallback: Gamma's cached outcomePrices (may be stale, but better than nothing).
        if pair is None:
            op = data.get("outcomePrices")
            if isinstance(op, str):
                op = json.loads(op)
            if op and len(op) >= 2:
                pair = (float(op[0]), float(op[1]))

        if pair is None:
            return None
        _mtm_cache[market_id] = (now, pair)
        return pair
    except Exception:
        return None


async def _current_side_prices(trades) -> dict:
    """Map trade.id -> current price of the side held, for open weather trades."""
    out: dict = {}
    todo = [t for t in trades if not t.settled and getattr(t, "market_type", None) == "weather"]
    if not todo:
        return out
    async with httpx.AsyncClient(timeout=8.0) as client:
        for t in todo:
            pair = await _fetch_outcome_prices(client, t.market_ticker)
            if pair is None:
                continue
            yes, no = pair
            out[t.id] = yes if t.direction in ("yes", "up", "above") else no
    return out


def _weather_signal_to_response(s) -> WeatherSignalResponse:
    from backend.data.weather import get_station_bias
    return WeatherSignalResponse(
        market_id=s.market.market_id,
        city_key=s.market.city_key,
        city_name=s.market.city_name,
        target_date=s.market.target_date.isoformat(),
        threshold_f=s.market.threshold_f,
        metric=s.market.metric,
        direction=s.direction,
        model_probability=s.model_probability,
        market_probability=s.market_probability,
        edge=s.edge,
        confidence=s.confidence,
        suggested_size=s.suggested_size,
        reasoning=s.reasoning,
        ensemble_mean=s.ensemble_mean,
        ensemble_std=s.ensemble_std,
        ensemble_members=s.ensemble_members,
        actionable=s.passes_threshold,
        slug=s.market.slug,
        bucket_label=s.market.bucket_label,
        unit=getattr(s.market, "unit", "F"),
        low_f=s.market.low_f,
        high_f=s.market.high_f,
        net_edge=s.net_edge,
        entry_price=s.entry_price,
        cost=s.cost,
        rel_spread=s.rel_spread,
        liquidity=s.market.liquidity,
        spread=s.market.spread,
        yes_price=s.market.yes_price,
        no_price=s.market.no_price,
        bias=get_station_bias(s.market.city_key, s.market.metric),
    )


@app.get("/api/events", response_model=List[EventResponse])
async def get_events(limit: int = 50):
    from backend.core.scheduler import get_recent_events
    events = get_recent_events(limit)
    return [
        EventResponse(
            timestamp=e["timestamp"],
            type=e["type"],
            message=e["message"],
            data=e.get("data", {})
        )
        for e in events
    ]


# Bot control
@app.post("/api/bot/start")
async def start_bot(db: Session = Depends(get_db)):
    from backend.core.scheduler import start_scheduler, log_event, is_scheduler_running

    state = db.query(BotState).first()
    if state:
        state.is_running = True
        db.commit()

    if not is_scheduler_running():
        start_scheduler()

    log_event("success", "Trading bot started")
    return {"status": "started", "is_running": True}


@app.post("/api/bot/stop")
async def stop_bot(db: Session = Depends(get_db)):
    from backend.core.scheduler import log_event

    state = db.query(BotState).first()
    if state:
        state.is_running = False
        db.commit()

    log_event("info", "Trading bot paused")
    return {"status": "stopped", "is_running": False}


@app.post("/api/bot/reset")
async def reset_bot(db: Session = Depends(get_db)):
    from backend.core.scheduler import log_event

    try:
        trades_deleted = db.query(Trade).delete()
        state = db.query(BotState).first()
        if state:
            state.bankroll = settings.INITIAL_BANKROLL
            state.total_trades = 0
            state.winning_trades = 0
            state.total_pnl = 0.0
            state.is_running = True

        db.commit()

        log_event("success", f"Bot reset: {trades_deleted} trades deleted. Fresh start with ${settings.INITIAL_BANKROLL:,.2f}")

        return {
            "status": "reset",
            "trades_deleted": trades_deleted,
            "new_bankroll": settings.INITIAL_BANKROLL
        }

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Reset failed: {e}")


@app.get("/api/dashboard", response_model=DashboardData)
async def get_dashboard(db: Session = Depends(get_db)):
    """Get all dashboard data in one call."""
    stats = await get_stats(db)

    # Recent trades (with mark-to-market prices for open positions)
    trades = db.query(Trade).order_by(Trade.timestamp.desc()).limit(50).all()
    side_prices = await _current_side_prices(trades)
    recent_trades = [_trade_to_response(t, current_price=side_prices.get(t.id)) for t in trades]

    # Equity curve
    equity_trades = db.query(Trade).filter(Trade.settled == True).order_by(Trade.timestamp).all()
    equity_curve = []
    cumulative_pnl = 0
    for trade in equity_trades:
        if trade.pnl is not None:
            cumulative_pnl += trade.pnl
            equity_curve.append({
                "timestamp": trade.timestamp.isoformat(),
                "pnl": cumulative_pnl,
                "bankroll": settings.INITIAL_BANKROLL + cumulative_pnl
            })

    # Calibration summary
    calibration = _compute_calibration_summary(db)

    # Weather data (if enabled)
    weather_signals_data = []
    weather_forecasts_data = []
    if settings.WEATHER_ENABLED:
        try:
            from backend.core.weather_signals import get_cached_signals
            from backend.data.weather import fetch_ensemble_forecast, CITY_CONFIG

            # Serve the LATEST scheduled scan (instant), never run a scan per page
            # load — a live scan is tens of seconds and was hanging the dashboard.
            wx_signals = get_cached_signals()
            weather_signals_data = [_weather_signal_to_response(s) for s in wx_signals]

            city_keys = [c.strip() for c in settings.WEATHER_CITIES.split(",") if c.strip()]
            for city_key in city_keys:
                if city_key not in CITY_CONFIG:
                    continue
                # cache_only: never block the dashboard on the forecast API.
                forecast = await fetch_ensemble_forecast(city_key, cache_only=True)
                if forecast:
                    weather_forecasts_data.append(WeatherForecastResponse(
                        city_key=forecast.city_key,
                        city_name=forecast.city_name,
                        target_date=forecast.target_date.isoformat(),
                        mean_high=forecast.mean_high,
                        std_high=forecast.std_high,
                        mean_low=forecast.mean_low,
                        std_low=forecast.std_low,
                        num_members=forecast.num_members,
                        ensemble_agreement=forecast.ensemble_agreement,
                    ))
        except Exception:
            pass

    return DashboardData(
        stats=stats,
        recent_trades=recent_trades,
        equity_curve=equity_curve,
        calibration=calibration,
        weather_signals=weather_signals_data,
        weather_forecasts=weather_forecasts_data,
    )


@app.websocket("/ws/events")
async def websocket_events(websocket: WebSocket):
    await ws_manager.connect(websocket)

    try:
        await websocket.send_json({
            "timestamp": datetime.utcnow().isoformat(),
            "type": "success",
            "message": "Connected to weather trading bot"
        })

        from backend.core.scheduler import get_recent_events
        for event in get_recent_events(20):
            await websocket.send_json(event)

        last_event_count = len(get_recent_events(200))
        while True:
            await asyncio.sleep(2)

            current_events = get_recent_events(200)
            if len(current_events) > last_event_count:
                new_events = current_events[last_event_count - len(current_events):]
                for event in new_events:
                    await websocket.send_json(event)
                last_event_count = len(current_events)

            await websocket.send_json({
                "type": "heartbeat",
                "timestamp": datetime.utcnow().isoformat()
            })

    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception:
        ws_manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
