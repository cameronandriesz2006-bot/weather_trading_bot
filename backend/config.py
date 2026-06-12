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
    DAILY_LOSS_LIMIT: float = 300.0
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

    # Forecast calibration (Phase 4) — turn the raw ensemble into an honest
    # probability. We fit a Normal to the ensemble mean/spread and WIDEN the
    # spread, because the GFS ensemble is under-dispersed (too confident).
    #   sigma_eff = max(sigma_ensemble * INFLATION, FLOOR_F) + lead_days * PER_LEAD_DAY_F
    WEATHER_SIGMA_INFLATION: float = 1.3      # blow up the (too-narrow) ensemble spread
    WEATHER_SIGMA_FLOOR_F: float = 2.0        # irreducible uncertainty (deg F), even if unanimous
    WEATHER_SIGMA_PER_LEAD_DAY_F: float = 0.7  # extra uncertainty per day of lead time

    class Config:
        env_file = ".env"
        extra = "ignore"  # tolerate leftover/unknown keys in .env (e.g. removed BTC settings)


settings = Settings()
