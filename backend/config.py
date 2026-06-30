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
    # RAISED 0.05 -> 0.20 on 2026-06-30 (user): the 5% fraction was far too timid for the
    # OOS-confirmed post-high edge. Set here (not .env) so the committed config is the source
    # of truth; the .env KELLY_FRACTION override was removed.
    KELLY_FRACTION: float = 0.20
    # Per-trade ceiling as a FRACTION OF THE LIVE BANKROLL (relative, so it scales at
    # any bankroll — the sim runs at $10k but the live account will be smaller). This is
    # now the SINGLE per-trade cap: it replaces the old fixed $75/$100 dollar caps that
    # were clamping essentially every Kelly bet to the same size regardless of edge or
    # confidence. The Kelly helper sizes each bet off the live bankroll and then clamps
    # the fraction here, so a bigger-edge / more-confident bet genuinely takes a larger
    # stake right up to this ceiling, and only the very strongest bets clip at it.
    KELLY_MAX_TRADE_FRACTION: float = 0.05   # RAISED 0.025->0.05 (2026-06-30) so Kelly 0.20 isn't clamped away; <= 5% of bankroll on any single bet

    # Settlement cadence (shared — settles all trade types)
    SETTLEMENT_INTERVAL_SECONDS: int = 120

    # Risk management. Limits are FRACTIONS OF THE LIVE BANKROLL (not fixed dollars) so
    # they scale at any bankroll. (The per-trade sizing cap lives with
    # KELLY_MAX_TRADE_FRACTION above; total weather exposure / min trade are below.)
    DAILY_LOSS_LIMIT_FRACTION: float = 0.15    # halt trading for the day after losing this share of bankroll (~$1,500 @ $10k)
    MAX_TOTAL_PENDING_TRADES: int = 20

    # Weather trading settings
    WEATHER_ENABLED: bool = True
    WEATHER_SCAN_INTERVAL_SECONDS: int = 900  # 15 min. Raised from 5 min (2026-06-22) to
    # cut API load after the blend tripled per-forecast cost and exhausted the Open-Meteo
    # free-tier daily quota. Daily-temp markets settle once/day, so 15-min scanning is plenty;
    # the 90-min forecast cache (_CACHE_TTL in weather.py) absorbs repeated scans so this
    # mainly throttles the per-scan Meteostat/Polymarket/order-book calls.
    WEATHER_SETTLEMENT_INTERVAL_SECONDS: int = 1800  # 30 min
    # Gap (seconds) inserted between consecutive COLD-cache forecast fetches in the
    # per-scan pre-warm loop, so the heavy 3-model blend requests don't burst all 6
    # cities back-to-back and trip Open-Meteo's per-minute rate limit (the cause of
    # the ~66% 429 rate, 2026-06-29). Only applies to real network fetches — warm
    # cache hits are skipped. ~12 cold combos x 2s = ~24s, trivial inside a 15-min scan.
    WEATHER_FORECAST_FETCH_SPACING_SECONDS: float = 2.0
    WEATHER_MIN_EDGE_THRESHOLD: float = 0.08  # 8% minimum edge to trade
    WEATHER_MAX_ENTRY_PRICE: float = 0.90   # RAISED 0.70->0.90 (2026-06-30, user): stop refusing the post-high FAVORITE bucket (the high is already in -> highest-confidence trades)
    # US markets resolve in °F; the international markets resolve in °C (handled
    # natively per-city — see CITY_CONFIG "unit" in backend/data/weather.py). The
    # international books carry ~2-3x the liquidity of the US weather markets.
    # NOTE (2026-06-21): los_angeles + shanghai DROPPED from active trading. Their
    # settlement stations are un-resolvable by the forecast grid (LA = coastal marine
    # layer; Shanghai/Pudong has no obs station within ~35km), so they can't be bias-
    # corrected and were the two worst money-losers AND worst Brier-vs-market in the
    # Phase-7 run (Shanghai −$534, LA −$312). They stay in CITY_CONFIG so any open
    # positions still settle and their scoreboard history is preserved (shown as
    # "retired" in the dashboard's active-vs-retired city panel — nothing deleted).
    # NOTE (2026-06-28): miami + london + seoul PARKED too. The full-history calibration
    # (offline, ~545 resolved markets, 3 independent reconstructions) showed they consistently
    # TRAIL the market's Brier (london +0.049 / miami +0.046), and seoul is the worst city AND
    # the only one with NO bias correction. Kept = Chicago/NYC (beat the market) + Denver/HK/
    # Tokyo/Paris (parity; Paris +0.017 is borderline-parity, kept on watch). Same mechanism as
    # above: PARKED not deleted — they stay in CITY_CONFIG so positions settle + history is
    # preserved, and it's reversible. The maker wiring (next) trades exactly these 6.
    # NARROWED 2026-06-30 to the Edge-2 live test: the Brier-confirmed same-day afternoon
    # (H>=16, post-high) nowcast cells. denver+chicago first; ATLANTA added after it cleared the
    # same OOS bar denver did (edge2_oos_backtest: Brier beats market both halves, profitable both;
    # dallas/austin FAILED and were rejected). Coastal (tokyo/paris/hong_kong) + nyc PARKED (their
    # backtest profit was a variance/Asia-leak fluke); all stay in CITY_CONFIG so open positions
    # still settle. See edge2_oos_backtest.py / memory edge2-inland-afternoon-seam.
    WEATHER_CITIES: str = "denver,chicago,atlanta"

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
    WEATHER_MAX_CITY_DAY_FRACTION: float = 0.12     # RAISED 0.07->0.12 (2026-06-30) to fit the bigger Kelly stakes; <= 12% of bankroll on one city+day

    # --- Maker / limit-order execution (Phase 8, 2026-06-28) ---------------------
    # The day-ahead edge (Chicago/NYC, the previous day) lives in books too THIN to take at
    # size as a taker — but ~$1k/bucket of flow crosses over the day. So the bot can POST a
    # resting limit order near fair value and let flow fill it (earning the spread instead of
    # paying it). This is the MAKER path; it uses backend/core/execution.py — simulated now
    # against the REAL live tape, swappable to py-clob-client to go live (SIMULATION_MODE gate).
    # GATED OFF by default: when False the scan uses the existing TAKER path BYTE-IDENTICALLY.
    # ENABLED 2026-06-28 (simulated — SIMULATION_MODE still True, so these are sim orders against
    # the real tape, no real money). Flip back to False to instantly revert to the taker path.
    # RETIRED 2026-06-30 (-> False) for the Edge-2 same-day-TAKER live test: the day-ahead maker
    # leg's fill rate was never proven, and the only OOS-robust edge is same-day post-high (taken
    # at the ask). Flip True to revive the hybrid maker/taker routing. See memory
    # maker-live-status-open-questions / edge2-live-test-config.
    WEATHER_MAKER_ENABLED: bool = False
    # How long a posted order rests before auto-cancel (GTD; Polymarket adds a 1-min security
    # threshold on top). Day-ahead orders rest for HOURS to catch the day's flow.
    WEATHER_MAKER_TTL_SECONDS: int = 21600     # 6h
    # Poll cadence for advancing fills (against the real trade tape) and expiring orders.
    WEATHER_MAKER_POLL_SECONDS: int = 120
    # Tick used to improve the bid by one step when posting (Polymarket rounds to a valid tick
    # on submit; 0.01 is the common weather-market tick).
    WEATHER_MAKER_TICK: float = 0.01

    # --- Post-extreme (same-day afternoon) gate (Edge-2 strategy, 2026-06-30) -----
    # The ONLY edge that survives out-of-sample is the same-day NOWCAST: once the day's
    # extreme is actually in (observed floor/ceiling active), our observed-anchored price
    # beats a slightly-slow market (denver/chicago, H>=16). Day-ahead and morning same-day
    # both LOSE (forecast σ too flat). When True, a signal is actionable ONLY if the
    # observed extreme is in (``observed_bound is not None`` — false for any future day and
    # for same-day before the set-hour), so the bot never takes a day-ahead or pre-extreme
    # bucket. This is ALSO the safety gate that, with WEATHER_MAKER_ENABLED off, stops
    # day-ahead signals from falling through to the taker path. Flip False to revert.
    WEATHER_REQUIRE_EXTREME_IN: bool = True

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

    # --- Multi-model blend (Phase 5). BUILT 2026-06-21, GATED OFF pending offline
    # validation — DO NOT enable without first re-fitting the bias table for the blend
    # AND resolving WEATHER_BLEND_SIGMA_INFLATION (see below). When enabled, the forecast
    # is an EQUAL-WEIGHT blend of the GFS + ECMWF + ICON ensembles instead of GFS-only.
    # Offline evidence (n=612 skill backtest): ~10% lower de-biased RMSE on highs and
    # ~half the cold bias; ECMWF is ~unbiased in heat where GFS runs ~2°F cold. It is a
    # BLEND, not a switch to ECMWF (ECMWF is worse at coastal microclimates) — the blend
    # banks best-of-each. ENABLED 2026-06-22 after blend_validate passed (skill: blend
    # beats GFS by ~16%/18% de-biased RMSE on highs/lows over n=543; bias table re-fit;
    # σ-inflation derived = 2.04). Flip back to False to revert to GFS-only instantly.
    WEATHER_BLEND_ENABLED: bool = True
    # The ensemble models to blend (Open-Meteo ensemble-api ids). Equal MODEL weight
    # (each model contributes equally regardless of its member count: GFS 31 / ECMWF 51
    # / ICON 40). If a model returns no data the blend falls back to whatever is present.
    WEATHER_BLEND_MODELS: str = "gfs_seamless,ecmwf_ifs025,icon_seamless"
    # σ-widening for the blend path. DERIVED via backend/data/blend_validate.py on
    # 2026-06-22 (n=30 city-days): the 3-model pool's spread-skill ratio is ~0.49 (vs
    # GEFS ~0.25), i.e. still under-dispersed, so spread is multiplied by ~1/0.49 ≈ 2.04
    # to calibrate (lifted range-coverage 50%→83%, target ~90%). NOTE this is MORE
    # widening than the GFS path (1.3) — the earlier "the blend needs less" guess was
    # wrong: a single model's 31 members agree too much, and even the 3-model pool is
    # still over-confident (ratio 0.49 < 1.0). CAVEAT: the dispersion sample is shallow (~30 days =
    # ensemble-api archive depth), so 2.04 is a STARTING POINT — re-run blend_validate as
    # the archive deepens / the season shifts and watch live coverage. The skill result
    # (n=543) is solid; this knob is the soft part, and erring wider is the safe side.
    WEATHER_BLEND_SIGMA_INFLATION: float = 2.04

    # Scoreboard visual-reset cutoff (UTC ISO8601, matches the UTC-naive DB timestamps).
    # When SET, the dashboard scoreboard (calibration + cohort tables) counts ONLY trades &
    # signals entered from this instant forward — a SOFT reset that hides old rows while
    # leaving them in the live DB. CURRENTLY EMPTY (disabled): on 2026-06-22 we instead did
    # a HARD reset — the pre-blend book was archived to archive/*.db and the live tables +
    # bankroll were cleared to a fresh $10k (see archive/README.md), so the live DB itself
    # is already clean and no filter is needed. Kept (code lives in api/main.py
    # _scoreboard_epoch + the three aggregations) for the next model change, when a soft
    # hide may be preferable to archiving. Empty string => score whatever is in the DB.
    SCOREBOARD_EPOCH: str = ""

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
