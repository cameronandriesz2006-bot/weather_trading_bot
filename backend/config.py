"""Configuration settings for the weather trading bot."""
import os
from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Database (SQLite for Phase 1, PostgreSQL for production)
    DATABASE_URL: str = "sqlite:///./tradingbot.db"

    # API Keys (optional)
    POLYMARKET_API_KEY: Optional[str] = None

    # Kalshi API
    KALSHI_API_KEY_ID: Optional[str] = None
    KALSHI_PRIVATE_KEY_PATH: Optional[str] = None
    KALSHI_ENABLED: bool = True

    # AI API Keys
    GROQ_API_KEY: Optional[str] = None

    # AI Model Configuration
    GROQ_MODEL: str = "llama-3.1-8b-instant"

    # AI Feature Flags
    AI_LOG_ALL_CALLS: bool = True
    AI_DAILY_BUDGET_USD: float = 1.0

    # Bot settings
    SIMULATION_MODE: bool = True
    INITIAL_BANKROLL: float = 10000.0
    # Fractional Kelly (shared sizing helper). Lowered in Phase 6: with honest
    # (Phase 4) probabilities we no longer need the aggressive fraction, and
    # smaller bets are safer until the scoreboard proves an edge. Tune empirically.
    KELLY_FRACTION: float = 0.10

    # Settlement cadence (shared — settles all trade types)
    SETTLEMENT_INTERVAL_SECONDS: int = 120

    # Risk management
    DAILY_LOSS_LIMIT: float = 750.0
    MAX_TRADE_SIZE: float = 75.0  # Per-trade hard cap inside the Kelly helper
    MAX_TOTAL_PENDING_TRADES: int = 20

    # Weather trading settings
    WEATHER_ENABLED: bool = True
    WEATHER_SCAN_INTERVAL_SECONDS: int = 300  # 5 min
    WEATHER_SETTLEMENT_INTERVAL_SECONDS: int = 1800  # 30 min
    WEATHER_MIN_EDGE_THRESHOLD: float = 0.08  # 8% minimum edge to trade
    WEATHER_MAX_ENTRY_PRICE: float = 0.70
    WEATHER_MAX_TRADE_SIZE: float = 100.0
    WEATHER_CITIES: str = "nyc,chicago,miami,los_angeles,denver"

    # Trading costs (Phase 6) — "profit" must mean profit net of costs.
    # On Polymarket the dominant cost is the bid/ask spread (the live market
    # spread is used when available; this is the fallback). We enter at the ask
    # (mid + spread/2) and require the model edge to clear costs before trading.
    WEATHER_DEFAULT_SPREAD: float = 0.02      # fallback spread (price units) if market lacks one
    WEATHER_FEE_RATE: float = 0.0             # platform trading fee as a fraction of notional
                                              # (Polymarket ~0; set for Kalshi when enabled)

    # Liquidity / slippage guard (Layer 1 + size cap). Thin, wide-spread weather
    # buckets produce mirage edges (a 30% "edge" on a 4c market whose spread is 2c
    # is not real). These gate such buckets out and cap our size to the book.
    #   - MIN_LIQUIDITY: skip buckets with less than this much resting liquidity ($)
    #   - MAX_REL_SPREAD: skip if the spread is a large fraction of the side's price
    #     (e.g. a 1.7c spread on a 4c contract = 0.42 -> mirage), even if absolute
    #     spread looks tiny
    #   - MAX_BOOK_FRACTION: never simulate taking more than this fraction of the
    #     book (so we don't pretend to fill $75 into a $200 market)
    WEATHER_MIN_LIQUIDITY: float = 500.0
    WEATHER_MAX_REL_SPREAD: float = 0.10
    WEATHER_MAX_BOOK_FRACTION: float = 0.10

    # Max total exposure to OPEN (unsettled) weather positions at once. Enforced
    # as a hard ceiling per trade so it never overshoots (was a hard-coded $500 in
    # the scheduler, checked only once per scan -> it could blow past to ~$600).
    WEATHER_MAX_ALLOCATION: float = 2000.0

    # Forecast calibration (Phase 4) — turn the raw ensemble into an honest
    # probability. We fit a Normal to the ensemble mean/spread and WIDEN the
    # spread, because the GFS ensemble is under-dispersed (too confident).
    #   sigma_eff = max(sigma_ensemble * INFLATION, FLOOR_F) + lead_days * PER_LEAD_DAY_F
    WEATHER_SIGMA_INFLATION: float = 1.3      # blow up the (too-narrow) ensemble spread
    WEATHER_SIGMA_FLOOR_F: float = 2.0        # irreducible uncertainty (deg F), even if unanimous
    WEATHER_SIGMA_PER_LEAD_DAY_F: float = 0.7  # extra uncertainty per day of lead time

    # Per-station bias correction. Raw GFS has repeatable per-station offsets the
    # market has already priced in (e.g. ~2F cold on NYC overnight lows); left
    # uncorrected they masquerade as edge. We measure bias = mean(forecast - actual)
    # from historical archives (backend/data/bias_backfill.py -> station_bias.json)
    # and SUBTRACT it from the forecast mean before pricing buckets.
    WEATHER_BIAS_ENABLED: bool = True
    WEATHER_BIAS_MIN_SAMPLES: int = 10        # don't correct on noise: need this many obs
    WEATHER_BIAS_MAX_SHIFT_F: float = 4.0     # safety cap on the correction magnitude (deg F)

    class Config:
        env_file = ".env"
        extra = "ignore"  # tolerate leftover/unknown keys in .env (e.g. removed BTC settings)


settings = Settings()
