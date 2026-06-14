# CLAUDE.md — Weather Prediction-Market Bot

Guidance for working in this repo. Read alongside `weather-bot-build-plan.md` (the
authoritative, phase-by-phase plan). This file is the quick-reference summary.

## What this project is

A forked prediction-market bot that trades **daily high/low temperature markets** on
**Kalshi** (`KXHIGH*` series) and **Polymarket** (Gamma API). It makes its *own*
ensemble weather forecast (31-member GFS from Open-Meteo), converts that into a
probability that a market's temperature threshold is met, compares to the market price,
and bets when its probability differs from the market's by more than an edge threshold.
Position sizing is fractional Kelly. A FastAPI backend runs the scan/settlement loop and
serves a React dashboard.

## Hard constraints (do not violate)

- **SIMULATION ONLY.** `SIMULATION_MODE` must stay `True` (`backend/config.py`). Never
  flip it. There is no real-trade execution layer and we are not building one now.
- **The goal is not profit — it's an honest scoreboard.** Fix forecasts and measurement
  first, then let the simulation tell us whether the model beats the market net of fees.
- **Don't skip ahead.** Get the basics correct before any fancy model work. Better models
  on top of a wrong location/timezone/scoreboard just produce confident nonsense faster.
- **Preserve `calculate_edge` and `calculate_kelly_size`** before deleting any crypto
  code — the weather path imports them from `backend/core/signals.py`.

## Phase order (from the build plan)

| Phase | Goal | Status |
|---|---|---|
| 0 | Run unchanged in simulation; commit known-good baseline | done (repo runs) |
| 1 | **Cut crypto cleanly** — extracted `calculate_edge`/`calculate_kelly_size` to `backend/core/sizing.py`; deleted `signals.py`/`crypto.py`/`btc_markets.py`/`markets.py`; removed BTC scan job; BTC endpoints now empty stubs; dropped BTC-only config | **done** |
| 2 | **Fix the scoreboard (THE LINCHPIN)** — the yes/no vs up/down bug | **done** |
| 3 | Correctness — right station per platform, local-day high, robust market parsing (ranges, skip-don't-guess) | **done** (Polymarket; Kalshi structured-fields part deferred until Kalshi is enabled) |
| 4 | Honest probability — fitted+widened distribution + lead-time uncertainty **done**; per-station bias correction **done** (historical backfill, no sim needed); climatology **deferred** (markets are 0–1 day out so climatology barely matters) | **done** |
| 6 | Real costs — subtract fees+spread before edge check and in P&L; add fee field; re-tune sizing; weather daily-loss stop | **done** |
| 5 | Stronger model — add ECMWF + ICON and blend; intraday conditioning | next (or run-and-evaluate, Phase 7, first) |
| 7 | Run weeks in simulation; decision point: does it beat the price net of fees? | |
| 7+ | Profitability levers (selectivity, intraday, threshold tuning, exit logic, more cities) | |
| 8 | Optional observe-only cross-platform arbitrage scanner | |
| 9 | GATED go-live (legal + code + evidence gates) — not a simple next step | |

Re-run the scoreboard after every change. One change at a time, always keep it running.

## Current state (2026-06-14)

Model-correctness + cost work (Phases 1–7) is done. We are now in **Phase 7
(run-and-evaluate)**: the scoreboard was **reset to a clean slate** so it contains only
**current-model** trades (liquidity/spread-gated, bias-corrected, cash-staked). Let it run
and read the scoreboard.

- **Scoreboard reset.** All old trades + signals deleted (they were placed by the
  pre-gate / pre-bias / contract-count model and would pollute the test); bankroll back to
  $10k. A check showed 9 of the old 10 trades would be rejected by the current gates.
- **Exposure scaled up for faster data:** `WEATHER_MAX_ALLOCATION` $500→**$2,000** (~20% of
  bankroll; covers all actionable opportunities); `DAILY_LOSS_LIMIT` $300→**$750** in step so
  the breaker doesn't stall data collection. `MAX_TRADE_SIZE` $75 and `KELLY_FRACTION` 0.10
  unchanged. `MAX_TRADES_PER_SCAN`=3 (hard-coded throttle; only paces the ramp).
- **Pending (small):** the dashboard "Open positions" sub-label still hard-codes "/$500" — make
  it read the real cap from the API (was mid-change when this was written: add the cap to the
  stats/dashboard response and use it in `App.tsx`).
- **Next big lever (discussed, NOT started): international cities.** Seoul/Tokyo/London/HK/Paris/
  Shanghai have **2–3× the liquidity** of the US markets, and Open-Meteo forecasts them for free —
  BUT they price in **°C** while the bot assumes **°F**. Main work = Celsius unit handling (parser
  + conversion + rounding is ±0.5°C not ±0.5°F + sigma config units) and per-city settlement
  stations; everything else (signals, sizing, P&L, dashboard, bias backfill) is reusable.

### Dashboard — REBUILT (weather-only, clean & spacious)
The old dense BTC-era dashboard was replaced. Frontend components now: `ScanView` (dropdown →
one event's bucket table + bias-corrected forecast header + Polymarket links + top-opportunities
strip; held buckets marked "holding"), `TradesPanel` (Active/Settled dropdown; Active shows
mark-to-market "Now" price + unrealized P&L + "Settles in"; Settled shows result/P&L/when),
`Scoreboard` (win rate, Brier, calibration), `LiveLog` (collapsible event log). **Deleted:**
SignalsTable, WeatherPanel, GlobeView, EdgeDistribution, MicrostructurePanel, EquityChart,
CalibrationPanel, Terminal, FilterBar, StatsCards. `WeatherSignalResponse` + `TradeResponse`
were extended with the fields the UI needs (slug, bucket_label, low/high_f, net_edge,
entry_price, cost, rel_spread, liquidity, bias, current_price, unrealized_pnl, city/metric/
target_date, settlement_time). `Trade` gained a `bucket_label` column (+ a one-off
`backend/data/backfill_trade_buckets.py` to fill old rows).

### Other recent fixes (post-Phase-6)
- **P&L = cash-staked** (see issue 6 sizing note): loss = full stake (`-size`), win = net odds
  (`size*(1-p)/p`). Old settled trades were re-graded. Tests: `tests/test_pnl.py`.
- **Trade loop no longer freezes:** it used `actionable[:3]` then skipped held markets, so once
  the top-3 by edge were all held it placed nothing and never reached the rest. Now it scans the
  whole actionable list and places up to `MAX_TRADES_PER_SCAN` *new* (non-held) trades.
- **Signal persistence deduped:** one row per market per UTC day (was a new row every 5-min scan
  ≈ 22k/day); the row updates in place, then freezes once executed.
- **Timezone display:** backend logs/timestamps are UTC without a 'Z'; the frontend now tags
  tz-less timestamps as UTC (`asUtc` in `LiveLog`/`TradesPanel`) so the live log + settled
  "X ago" render in local time.
- **"Actionable now" excludes held markets** (headline count, opportunities strip, dropdown count).
- **Mark-to-market:** open positions show the live side price + unrealized P&L (`_current_side_prices`
  in `api/main.py`, 30s server-side cache).

## Key bugs / known issues

1. **Scoreboard label mismatch (LINCHPIN, Phase 2) — FIXED.** Weather signals store
   `direction = "yes"/"no"`; settlement used to build `actual_outcome = "up"/"down"` and
   grade `outcome_correct = (signal.direction == actual_outcome)`, so `"yes" == "up"` was
   always False and **every weather prediction graded wrong**. Fixed by translating at the
   settlement step: `grade_signal_outcome(direction, settlement_value)` in
   `backend/core/settlement.py` is vocabulary-agnostic ("yes"/"up" = first outcome,
   "no"/"down" = second) and returns `(actual_outcome, outcome_correct)` in the signal's own
   vocab. Regression test: `tests/test_settlement_grading.py` (run with repo root on
   `PYTHONPATH`; pytest not installed, file is also runnable as a script). Brier score was
   always correct (numeric `model_probability` vs `settlement_value`); `calculate_pnl` was
   never affected. Note: at fix time the DB held only legacy BTC data and zero weather
   signals, so no retroactive re-grading was needed.

2. **Polymarket weather fetch finds 0 markets (Phase 3, market-reading) — FIXED.**
   `weather_markets.py` was rebuilt: it now queries Gamma with `tag_slug=daily-temperature`
   (the old `tag`/`slug_contains` params were silently ignored → 0 markets), paginates past
   the 100-event page cap, derives city/metric/date from the **event slug** (robust, explicit
   year), parses each bucket's `groupItemTitle` into a numeric range, and **skips** anything it
   can't read cleanly. Markets are modelled as range buckets (`low_f`/`high_f`, open-ended tails)
   and scored by `EnsembleForecast.probability_high/low_in_range` (rounding-aware: bucket [82,83]
   = raw [81.5,83.5)). `WeatherMarket.threshold_f`/`direction` are compat properties so the API
   /frontend contract is unchanged. Verified: 85 markets / 85 signals via the live API.

3. **Wrong location/station (Phase 3) — FIXED.** `CITY_CONFIG` (`backend/data/weather.py`) now
   points each city's lat/lon at the **settlement station** (not city centre): NYC = LaGuardia
   (KLGA, was Central Park), Denver = Buckley SFB (KBKF, was Denver Intl), and Chicago/Miami/LA
   were moved from downtown to their airports (KORD/KMIA/KLAX) — airport vs downtown can differ
   several degrees (esp. coastal LAX). Unused `nws_office`/`nws_gridpoint` fields removed. NWS
   observed-temp stays unused for Polymarket settlement (it resolves from its own market outcome),
   so station only affects the forecast. Kalshi may settle on *different* stations — verify before
   enabling it.

4. **UTC vs local day (Phase 3) — FIXED.** The Open-Meteo ensemble request now passes
   `timezone=auto`, so the daily high/low is aggregated over the station's local calendar day
   (the day markets settle on) rather than a UTC day.

5. **Overconfident forecast (Phase 4) — core FIXED.** Bucket probability now comes from a
   fitted Normal over the ensemble mean/spread, integrated across the bucket's rounding
   interval (`EnsembleForecast._fitted_bucket_prob` / `probability_high|low_in_range`). The
   spread is **widened** (`sigma_eff = max(sigma*INFLATION, FLOOR) + lead_days*PER_LEAD_DAY`,
   config knobs `WEATHER_SIGMA_*`) because the GFS ensemble is under-dispersed. Effect: live
   median |edge| fell to ~1% and max from ~95% → ~36%; a unanimous ensemble no longer implies
   100%. Tests: `tests/test_forecast_distribution.py`. Per-station **bias correction is now
   done** (issue 11). **Still deferred:** climatology blend — at 0–1 day lead it adds little.
   `_fraction_in_range` is kept as a raw reference but is no longer the traded probability.

6. **No fees anywhere (Phase 6) — FIXED.** The dominant Polymarket cost is the bid/ask
   spread, so the signal now enters at the effective **ask** (`mid + spread/2`, live spread
   captured into `WeatherMarket.spread`) and gates/sizes on **net edge** (`gross − spread/2 −
   fee`); `passes_threshold` also enforces the entry-price cap. P&L is net of fees: a `fee`
   column was added to `Trade` (migrated in `ensure_schema`, which now logs failures loudly)
   and `calculate_pnl` subtracts it (spread is already in `entry_price`). Config knobs:
   `WEATHER_DEFAULT_SPREAD`, `WEATHER_FEE_RATE` (0 for Polymarket; set for Kalshi). Kelly
   fraction lowered 0.25→0.10 (honest probs need less aggression). The daily-loss circuit
   breaker now guards the weather job too (was crypto-only). Wide-spread illiquid buckets are
   correctly filtered (e.g. a +30% gross edge can drop below 8% net). Tests:
   `tests/test_weather_signal_costs.py`.
   **Sizing convention (fixed):** `Trade.size` is the CASH staked. `calculate_pnl` now
   pays the prediction-market net odds on a win (`size*(1-p)/p`) and loses the full stake
   on a loss (`-size`) — consistent with the Kelly cash fraction and the $500 exposure cap.
   (It previously treated `size` as a contract count, so a loss was only `size*p`; existing
   settled trades were re-graded.) Tests: `tests/test_pnl.py`.

7. **Liquidity/slippage + sizing realism (Phase 7+, Layer 1) — FIXED.** The reader now
   captures `liquidity` (Gamma `liquidityNum`) and live `best_bid`/`best_ask`; signals enter
   at the **real ask** when the book is present. Two new gates in `passes_threshold` reject
   mirage edges: a minimum liquidity (`WEATHER_MIN_LIQUIDITY`, $500) and a maximum **relative**
   spread (`WEATHER_MAX_REL_SPREAD`, 10% — a 2¢ spread on a 4¢ contract is a 50% mirage even
   though 2¢ "looks" tiny). Trade size is capped to a fraction of the book
   (`WEATHER_MAX_BOOK_FRACTION`, 10%) so we don't pretend to fill $75 into a $200 market. Live
   check: actionable fell from ~22 to ~8–10; the large edges that survive are on liquid,
   tight-spread markets (→ that's forecast **bias**, the next lever, not liquidity). Tests
   extended in `tests/test_weather_signal_costs.py`. **Still deferred:** size-dependent
   slippage baked into the fill price (Layer 2(ii)) and real order-book walking (Layer 3).
8. **Kelly sized off a constant bankroll — FIXED.** `generate_weather_signal` now takes the
   **live** bankroll (read from `BotState` in `scan_for_weather_signals`, fallback
   `INITIAL_BANKROLL`), so bets shrink after losses instead of always sizing off $10k.
9. **Allocation cap hard-coded & overshooting — FIXED.** Moved to `config.WEATHER_MAX_ALLOCATION`
   ($500) and enforced as a **hard per-trade ceiling** (trim to remaining room; stop when
   <`MIN_TRADE_SIZE`), so open weather exposure no longer blows past to ~$600.
10. **Cost-aware economics now persisted — DONE.** `Signal` gained `net_edge`, `entry_price`,
   `cost`, `rel_spread`, `liquidity` (migrated in `ensure_schema`), so the scoreboard can prove
   an edge NET of cost and the dashboard (Q8) has the fields it needs.
11. **Per-station bias correction — DONE (historical backfill, no sim needed).** Raw GFS has
   repeatable per-station offsets the market has already priced in (measured: NYC & LA run
   ~1.2F cold on overnight lows, Chicago/LA ~0.9F warm on highs). `backend/data/bias_backfill.py`
   pulls forecast (historical-forecast-api, GFS) vs actual (archive-api, ERA5) over a 60-day
   window and writes `backend/data/station_bias.json`; `weather.get_station_bias()` reads it and
   `EnsembleForecast.corrected_mean()` SUBTRACTS the bias before pricing buckets. Gated by
   `WEATHER_BIAS_ENABLED` / `_MIN_SAMPLES` (10) / `_MAX_SHIFT_F` (4F clamp); zero bias = cold-start
   no-op. Re-run the script periodically (or wire a job) to refresh. Tests:
   `tests/test_bias_correction.py`. **Deferred refinement:** a live verification loop against the
   official settlement station (NWS) to refine the archive-derived prior — `fetch_nws_observed_temperature`
   is its data source (do not delete).

### Minor cleanup (catch along the way)
- `database.py ensure_schema()` swallows schema-migration errors silently — make it log
  loudly (matters when adding the fee column in Phase 6). **(done)**
- Date parser assumes current year when a title omits it — wrong-year risk near New Year.
- Weather exposure cap ($500) is hard-coded in the scheduler, not in config — move it.
  **(done — now `config.WEATHER_MAX_ALLOCATION`, enforced per-trade)**
- Dead weight: unused `ScanLog` table, an uncalled Groq/AI hook in the weather path, a
  configured-but-unscheduled weather-settlement timer. NOTE: the "unused observed-temp
  function" (`fetch_nws_observed_temperature`) is NOT dead weight — keep it; it's the
  data source for the deferred per-station **bias correction** (forecast vs. actual).
- Verify the Kalshi base URL in `kalshi_client.py` is current (lower priority; Kalshi is
  region-blocked).

## Keep / Fix / Rebuild (per file)

- **Keep:** `kalshi_client.py`, `database.py` (+fee col), `scheduler.py` (-BTC, fix
  weather loss-stop), `api/main.py`, `config.py` (-BTC, +model/fee settings).
- **Fix:** `kalshi_markets.py` (ranges + structured fields), `weather_signals.py`
  (re-wire to new probability source).
- **Done (Phase 1):** `calculate_edge` & `calculate_kelly_size` now live in
  `core/sizing.py`; `core/signals.py` deleted.
- **Rebuild:** `data/weather.py` (multi-model, calibrated, station-correct),
  `data/weather_markets.py` (robust range handling).
- **Deleted (Phase 1):** `crypto.py`, `btc_markets.py`, `markets.py`.
- **Frontend: REBUILT** (no longer "display only / keep as-is"). See the "Dashboard — REBUILT"
  note above for the current components and the 10 deleted ones.

## Architecture quick map

- `backend/api/main.py` — FastAPI routes + dashboard aggregation.
- `backend/core/scheduler.py` — APScheduler jobs (weather scan, settlement, heartbeat).
- `backend/core/sizing.py` — shared `calculate_edge` / `calculate_kelly_size`.
- `backend/core/weather_signals.py` — weather signal generation (forecast → edge → Kelly).
- `backend/core/settlement.py` — routes settlement by `market_type`; grades P&L + calibration.
- `backend/data/weather.py` — Open-Meteo ensemble + NWS observations + `CITY_CONFIG`.
- `backend/data/weather_markets.py` — Polymarket weather market fetcher/parser.
- `backend/data/kalshi_markets.py` / `kalshi_client.py` — Kalshi fetch + RSA-PSS auth.
- `backend/models/database.py` — SQLAlchemy models (`Trade`, `Signal`, `BotState`, ...).

## Working agreement

Propose one change at a time, explain it in plain English, keep the bot running after
each step, and re-read the scoreboard. The honest finish line is **"the scoreboard says
it beats the price, net of fees"** — not "it runs".
