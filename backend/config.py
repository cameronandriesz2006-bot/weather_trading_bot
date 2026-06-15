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
    KELLY_FRACTION: float = 0.05
    # Per-trade ceiling as a FRACTION OF THE LIVE BANKROLL (relative, so it scales at
    # any bankroll — the sim runs at $10k but the live account will be smaller). This is
    # now the SINGLE per-trade cap: it replaces the old fixed $75/$100 dollar caps that
    # were clamping essentially every Kelly bet to the same size regardless of edge or
    # confidence. The Kelly helper sizes each bet off the live bankroll and then clamps
    # the fraction here, so a bigger-edge / more-confident bet genuinely takes a larger
    # stake right up to this ceiling, and only the very strongest bets clip at it.
    KELLY_MAX_TRADE_FRACTION: float = 0.025   # <= 2.5% of bankroll on any single bet

    # Settlement cadence (shared — settles all trade types)
    SETTLEMENT_INTERVAL_SECONDS: int = 120

    # Risk management. Limits are FRACTIONS OF THE LIVE BANKROLL (not fixed dollars) so
    # they scale at any bankroll. (The per-trade sizing cap lives with
    # KELLY_MAX_TRADE_FRACTION above; total weather exposure / min trade are below.)
    DAILY_LOSS_LIMIT_FRACTION: float = 0.15    # halt trading for the day after losing this share of bankroll (~$1,500 @ $10k)
    MAX_TOTAL_PENDING_TRADES: int = 20

    # Weather trading settings
    WEATHER_ENABLED: bool = True
    WEATHER_SCAN_INTERVAL_SECONDS: int = 300  # 5 min
    WEATHER_SETTLEMENT_INTERVAL_SECONDS: int = 1800  # 30 min
    WEATHER_MIN_EDGE_THRESHOLD: float = 0.08  # 8% minimum edge to trade
    WEATHER_MAX_ENTRY_PRICE: float = 0.70
    # US markets resolve in °F; the international markets resolve in °C (handled
    # natively per-city — see CITY_CONFIG "unit" in backend/data/weather.py). The
    # international books carry ~2-3x the liquidity of the US weather markets.
    WEATHER_CITIES: str = "nyc,chicago,miami,los_angeles,denver,london,tokyo,seoul,paris,shanghai,hong_kong"

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
    # Minimum lifetime TRADED volume ($). Distinct from liquidity (resting quotes):
    # a market can show ~$900 of resting orders while having traded almost nothing,
    # in which case those quotes are likely a lone market maker that can vanish and
    # adverse selection is high — risk the static order-book sim can't capture. So
    # we additionally require the market to have actually traded this much.
    WEATHER_MIN_VOLUME: float = 500.0

    # Max total exposure to OPEN (unsettled) weather positions at once, and the minimum
    # stake for a single trade — both FRACTIONS OF THE LIVE BANKROLL so they scale at any
    # bankroll. The allocation cap is enforced as a hard per-trade ceiling (trim to the
    # remaining room) so total open exposure never overshoots; trades whose natural size
    # is below the minimum are bumped up to it (an actionable bucket is still worth a min
    # stake). Replaces the old fixed $2,000 allocation / $10 min.
    WEATHER_MAX_ALLOCATION_FRACTION: float = 0.20   # <= 20% of bankroll in open weather bets (~$2,000 @ $10k)
    WEATHER_MIN_TRADE_FRACTION: float = 0.001       # min stake 0.1% of bankroll (~$10 @ $10k)
    # Correlated-risk cap. Every bucket of the SAME city+day hinges on one forecast,
    # so they can all win or lose together; the high and low of a city share the same
    # air mass too. Limit total OPEN stake on any single city+day so the 20% allocation
    # can't pile onto a single weather outcome. Default chosen by the auditor (~one full
    # position per city/day, forcing diversification across >=3 cities to use the full
    # allocation); tune to your risk appetite.
    WEATHER_MAX_CITY_DAY_FRACTION: float = 0.07     # <= 7% of bankroll on one city+day (~$700 @ $10k)

    # Forecast calibration (Phase 4) — turn the raw ensemble into an honest
    # probability. We fit a Normal to the ensemble mean/spread and WIDEN the
    # spread, because the GFS ensemble is under-dispersed (too confident).
    #   sigma_eff = max(sigma_ensemble * INFLATION, FLOOR_F) + lead_days * PER_LEAD_DAY_F
    WEATHER_SIGMA_INFLATION: float = 1.3      # blow up the (too-narrow) ensemble spread
    WEATHER_SIGMA_FLOOR_F: float = 2.0        # irreducible uncertainty (deg F), even if unanimous
    WEATHER_SIGMA_PER_LEAD_DAY_F: float = 0.7  # extra uncertainty per day of lead time

    # Intraday σ schedule (Phase 5). The flat σ-floor above is wrong in BOTH
    # directions on the in-progress day: in the morning the day's extreme can still
    # move several degrees (the floor is too CONFIDENT), and by evening it's nearly
    # locked (the floor is far too UNSURE, so the bot won't commit when it should).
    # backend/data/intraday_curve.json holds, per city/metric/LOCAL hour, the empirical
    # std of (final daily extreme − extreme so far) from 10y of station obs — exactly
    # the residual uncertainty at that hour. On the in-progress local day we use that
    # std for sigma_eff instead of the flat floor (see EnsembleForecast._effective_sigma).
    # Enabled 2026-06-14 (user decision). Live-validate the next time the Open-Meteo
    # quota is back: evening highs should get confident, mornings stay unsure, and bet
    # sizes must NOT blow up. _MIN is a HARD floor so σ can never collapse to ~0 and
    # turn a tiny edge into an enormous Kelly bet — a KEY SAFETY RAIL. Defined in °F;
    # scaled 1/1.8 for °C cities (a temperature spread). Do NOT hand-tune the curve
    # numbers — they come from backend/data/intraday_backtest.py.
    WEATHER_INTRADAY_SIGMA_ENABLED: bool = True
    WEATHER_INTRADAY_SIGMA_MIN_F: float = 0.3

    # Per-station bias correction. Raw GFS has repeatable per-station offsets the
    # market has already priced in (e.g. ~2F cold on NYC overnight lows); left
    # uncorrected they masquerade as edge. We measure bias = mean(forecast - actual)
    # from historical archives (backend/data/bias_backfill.py -> station_bias.json)
    # and SUBTRACT it from the forecast mean before pricing buckets.
    WEATHER_BIAS_ENABLED: bool = True
    WEATHER_BIAS_MIN_SAMPLES: int = 10        # don't correct on noise: need this many obs
    WEATHER_BIAS_MAX_SHIFT_F: float = 4.0     # safety cap on the correction magnitude (deg F)

    # Market-gap guardrail. The market-implied mean (probability-weighted center of
    # the live bucket prices) is, on a near-settlement day, a near-truth estimate of
    # where the high/low will land. If OUR forecast mean disagrees with it by more
    # than this, we're almost certainly the miscalibrated one (wrong station / un-
    # corrected bias) and the resulting "edge" is a mirage that would lose — so we
    # SUPPRESS trading that whole event rather than bet into our own error. The mean
    # is the easy part the market nails same-day; a real edge should come from the
    # distribution SHAPE around a similar mean, not from disagreeing on the level by
    # several degrees. Expressed in °F; scaled by 1/1.8 for °C cities (a spread).
    WEATHER_MARKET_GAP_ENABLED: bool = True
    WEATHER_MAX_MARKET_GAP_F: float = 2.0
    WEATHER_MARKET_GAP_MIN_BUCKETS: int = 3   # need this many finite buckets to trust the implied mean
    # The gap tolerance is NOT flat. Once the intraday schedule sharpens our forecast
    # (small sigma), a 2°F disagreement with the market would put our probability on
    # the WRONG bucket — so the allowed gap scales with our effective sigma:
    #   gap_threshold = clamp(SIGMA_K * sigma_eff, MIN, MAX_MARKET_GAP_F)
    # MIN stops it collapsing so tight that a fraction-of-a-degree thermometer
    # difference suppresses everything (0.5°F still sits inside one 2°F bucket); MAX
    # preserves the original behaviour when we are genuinely unsure (mornings).
    # Both MIN/SIGMA_K are °F-defined and scaled by 1/1.8 for °C cities.
    WEATHER_MIN_MARKET_GAP_F: float = 0.5
    WEATHER_MARKET_GAP_SIGMA_K: float = 2.0

    class Config:
        env_file = ".env"
        extra = "ignore"  # tolerate leftover/unknown keys in .env (e.g. removed BTC settings)


settings = Settings()
