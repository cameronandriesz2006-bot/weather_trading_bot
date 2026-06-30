# CLAUDE.md — Weather Prediction-Market Bot

Quick-reference for working in this repo. Read alongside `weather-bot-build-plan.md`.
**Session history before 2026-06-30 lives in `git log` and the auto-memories** — this file is
kept short on purpose.

## What this project is

A prediction-market bot that trades **daily high/low temperature markets** on **Polymarket**
(Gamma API) and, deferred, **Kalshi** (`KXHIGH*`). It makes its own ensemble weather forecast
(GFS+ECMWF+ICON blend via Open-Meteo), converts it into a probability for each market's
temperature bucket, compares to the live order-book price, and bets when the gap exceeds an edge
threshold. Sizing is fractional Kelly. A FastAPI backend runs the scan/settlement loop and serves
a React dashboard. **This machine is the always-on runner** (systemd `weatherbot.service`, port
8000).

## Safeguards (what keeps a fake "edge" from being traded)

If OUR forecast is wrong we *think* the market is mispriced when really we are — and we lose. Each
safeguard exists to only bet when we genuinely know something:

1. **Volume gate** — skip "ghost town" markets where almost nothing has actually traded.
2. **Live CLOB price, not stale Gamma** — read the real order book both to find edges and to mark
   open positions.
3. **Real fill price incl. slippage** — walk the actual offers and pay the true VWAP; edges that
   only exist at top-of-book disappear.
4. **Station-truth bias correction** — measure the forecast's offset vs the actual station
   thermometer and subtract it; auto-skip cities where that measurement is unreliable (coastal).
5. **Market-gap guardrail** — if our forecast's temperature disagrees with the market's by more
   than a confidence-scaled tolerance, refuse the event; the market nails the basic level.
6. **Observed-so-far floor/ceiling** — once the day's extreme has actually occurred, don't price
   the final high below (or low above) what's already on the thermometer; plus an intraday-σ
   schedule + observed-anchored pricing center so confidence tracks reality through the day.
7. **Post-extreme gate** (`WEATHER_REQUIRE_EXTREME_IN`) — only act once the day's extreme is
   actually in (observed floor/ceiling active: high ≥16h, low ≥10h local). Never bet day-ahead or
   pre-extreme, where the forecast σ is too flat to beat the market. Doubles as the safety gate
   that, with the maker leg off, stops day-ahead buckets from being taken.

## Hard constraints (do not violate)

- **SIMULATION ONLY.** `SIMULATION_MODE` stays `True`. There is **no live-execution path** — going
  live is a build (order signing / submission / reconciliation), deferred until the simulation
  proves an edge.
- **Fix forecasts and measurement first**, then let the simulation say whether the model beats the
  market net of fees. Get the basics right before fancy model work.
- **Preserve `calculate_edge` / `calculate_kelly_size`** (in `backend/core/sizing.py`) — the
  weather path imports them.
- **`.env` overrides `config.py`** (pydantic-settings). Any config change must check `.env` first.

## Current state (2026-06-30) — Edge-2 live test

Live 24/7 on the server, simulation only, GFS+ECMWF+ICON blend. **Now running the Edge-2 live
test**: the one OOS-robust seam the backtests found — the same-day inland afternoon nowcast
(`backend/data/edge2_backtest.py`; memory `edge2-live-test-config` / `edge2-inland-afternoon-seam`).

**Deployed config for the test (in `config.py` defaults unless noted):**
- **Cities = `denver,chicago` only** — the Brier-confirmed H≥16 post-high cells. Coastal
  (`tokyo/paris/hong_kong`) + `nyc` PARKED (their backtest "profit" was a variance/Asia-leak
  fluke). All parked/cut cities stay in `CITY_CONFIG` so open positions still settle.
- **Same-day TAKER only** — day-ahead maker leg RETIRED (`WEATHER_MAKER_ENABLED=False`); reverts to
  the byte-identical taker path, no maker_poll job. Dashboard maker panel removed.
- **Post-extreme gate** (`WEATHER_REQUIRE_EXTREME_IN`, safeguard 7) — only trade once the day's
  extreme is in; also blocks day-ahead taker (the losing too-flat-σ regime).
- **Scoreboard soft-reset** (`SCOREBOARD_EPOCH` in `.env`, the one operational override) — headline
  P&L / win-rate / calibration count only post-reset trades; history kept, open positions settle;
  sizing still off the true bankroll.

Background: the 2026-06-29 audit (`AUDIT_2026-06-29.md`) found the broad as-deployed bot had **no
edge** (active −$924/27). The day-ahead distribution is 3–4× flatter than the market's → fake NO
bets; "parity" was an in-sample-σ + look-ahead + Asia-leak artifact. The one real seam is the
inland same-day post-high nowcast — which is exactly what this test now isolates.

**Judge on Brier per slice (bootstrap CI), not P&L** (too noisy at this scale). The decisive
go/no-go before trusting any P&L is still an **OOS lead-correct-forecast backtest** (kills vintage
look-ahead) + real-book fill realism — run that offline in parallel; the live test is the slow
forward shadow. Other open audit fixes in `AUDIT_2026-06-29.md` §1 (NO-on-modal, drawdown halt,
isotonic calibration, point-in-time storage, auth on control endpoints, UTC settlement clock).

## Phase order

0 baseline · 1 cut crypto · 2 fix scoreboard (linchpin yes/no bug) · 3 correctness (station, local
day, market parsing) · 4 honest probability (fitted+widened dist, station bias) · 6 real costs
(fees+spread, net edge) — **all done**. 5 stronger model (blend) — **done/live**. **7
run-and-evaluate — current** (does it beat the price net of fees?). 7+ profitability levers · 8
optional arb scanner · 9 gated go-live.

## What's already fixed (don't re-break)

- **Scoreboard grading (linchpin)** — `grade_signal_outcome` translates yes/no vs up/down;
  every weather prediction used to grade wrong.
- **Market fetch** — Gamma `tag_slug=daily-temperature`, paginated, city/metric/date from event
  slug, buckets parsed as numeric ranges, skip-don't-guess. `parse_bucket_label` handles °F ranges,
  °C single-degree, sub-zero, open tails.
- **Station/timezone** — `CITY_CONFIG` lat/lon at the settlement station; `timezone=auto` so the
  high/low is the local-day extreme.
- **Honest probability** — fitted Normal over ensemble mean/spread, integrated over the bucket's
  rounding interval; spread widened (under-dispersed ensemble); per-station bias subtracted
  (`station_bias.json`, Meteostat station obs).
- **Costs** — enter at the real ask/VWAP; gate+size on net edge (gross − spread/2 − fee); `fee`
  column; `calculate_pnl` pays net odds on win, full stake on loss.
- **Liquidity/slippage** — min liquidity + max relative-spread gates; size capped to a book
  fraction; candidates walk the real CLOB book for exact VWAP (`backend/data/orderbook.py`).
- **Sizing is bankroll-relative** — `KELLY_FRACTION` 0.05, `KELLY_MAX_TRADE_FRACTION` 0.025,
  `WEATHER_MAX_ALLOCATION_FRACTION` 0.20, `WEATHER_MAX_CITY_DAY_FRACTION` 0.07, daily-loss 0.15.
- **Settlement** — matches the exact bucket by id; settles when closed OR local day over + price
  decisive.
- **Cities** — Edge-2 test: **2 active (`denver, chicago`)**; `nyc` + coastal (`tokyo, paris,
  hong_kong`) parked, LA/shanghai cut (un-resolvable stations). All parked/cut cities stay in
  `CITY_CONFIG` so open positions still settle.
- **°C cities** — native unit throughout, no conversion; σ-floor constants scaled 1/1.8 for °C.

## Known open / deferred

- Date parser assumes current year when a title omits it — wrong-year risk near New Year.
- Control endpoints (`/api/bot/*`, `/api/run-scan`, `/api/settle-trades`) unauthenticated — gate
  before any non-local deploy.
- Kalshi is NOT a drop-in (US-only cities, different stations, own parser/auth/fees) — deferred.
- Orphaned legacy tables (`ai_logs`/`scan_logs`/`btc_price_snapshots`) still exist — drop them.

## Architecture quick map

- `backend/api/main.py` — FastAPI routes + dashboard aggregation.
- `backend/core/scheduler.py` — APScheduler jobs (weather scan, settlement, heartbeat).
- `backend/core/sizing.py` — shared `calculate_edge` / `calculate_kelly_size`.
- `backend/core/weather_signals.py` — weather signal generation (forecast → edge → Kelly).
- `backend/core/settlement.py` — routes settlement by `market_type`; grades P&L + calibration.
- `backend/data/weather.py` — Open-Meteo ensemble + obs + `CITY_CONFIG` + intraday/bias loaders.
- `backend/data/weather_markets.py` — Polymarket weather market fetcher/parser.
- `backend/data/orderbook.py` — live CLOB book fetch + VWAP fill walk.
- `backend/data/kalshi_markets.py` / `kalshi_client.py` — Kalshi (deferred).
- `backend/models/database.py` — SQLAlchemy models (`Trade`, `Signal`, `BotState`).

## Working agreement

One change at a time, explained in plain English, keep the bot running, re-read the scoreboard
after each. Answers stay concise (see memory). The honest finish line is **"the scoreboard says it
beats the price, net of fees"** — not "it runs".
