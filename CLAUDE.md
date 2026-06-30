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

## Hard constraints (do not violate)

- **SIMULATION ONLY.** `SIMULATION_MODE` stays `True`. There is **no live-execution path** — going
  live is a build (order signing / submission / reconciliation), deferred until the simulation
  proves an edge.
- **Fix forecasts and measurement first**, then let the simulation say whether the model beats the
  market net of fees. Get the basics right before fancy model work.
- **Preserve `calculate_edge` / `calculate_kelly_size`** (in `backend/core/sizing.py`) — the
  weather path imports them.
- **`.env` overrides `config.py`** (pydantic-settings). Any config change must check `.env` first.

## Current state (2026-06-30)

Live 24/7 on the server, simulation only, GFS+ECMWF+ICON blend deployed (σ-inflation 2.04).

**A full quant audit (2026-06-29, `AUDIT_2026-06-29.md`) concluded the bot has no edge as deployed
and is losing** (active book −$924/27 settled). Root cause: the day-ahead probability distribution
is 3–4× flatter than the market's, so the bot bets NO against the bucket the market correctly
favours. The earlier "at parity" reading was a backtest artifact (in-sample σ + look-ahead + an
Asia timezone leak). One real seam exists: the forecast *center* beats the market inland (Chicago
OOS) — the over-wide σ is smearing it away.

**Direction (set 2026-06-30):**
- **Pursue a SAME-DAY taker edge.** Same-day books are deep and fill instantly; the catastrophic
  too-flat-σ problem is a *day-ahead* phenomenon (same-day uses the honest intraday curve). Edge is
  a nowcasting problem: observed-so-far + empirical intraday drift vs the market price.
- **Park the day-ahead maker leg.** Maker orders barely fill (~3.7% in sim; they expire before the
  extreme forms). Not relying the strategy on whether they fill.
- **Near-term goal = trade evenly with the market (parity) first**, then push for edge. Profitable
  PM weather bots are proven possible — the edge is a given; the question is HOW, not WHETHER.
- Judge on **Brier per slice (bootstrap CI)**, not P&L (too noisy at this scale).

Open audit fixes worth doing regardless (full list in `AUDIT_2026-06-29.md` §1): kill the
NO-on-modal trade + tighten the market-gap guardrail; add a trailing-drawdown halt; calibrate
probability (isotonic/Platt); point-in-time storage; a real event-driven P&L backtest; lock down
the unauthenticated control endpoints; fix the taker/maker double-book and UTC settlement clock.

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
- **Cities** — 6 active (`nyc, chicago, denver, tokyo, paris, hong_kong`); LA/shanghai cut
  (un-resolvable stations). Cut cities stay in `CITY_CONFIG` so open positions still settle.
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
