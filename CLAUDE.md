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
| 4 | Honest probability — fitted+widened distribution + lead-time uncertainty **done**; bias correction & climatology **deferred** (need run-time forecast-vs-actual history; markets are 0–1 day out so climatology barely matters) | core **done** |
| 5 | Stronger model — add ECMWF + ICON and blend; intraday conditioning | **next** |
| 6 | Real costs — subtract fees+spread before edge check and in P&L; add fee field; re-tune sizing; weather daily-loss stop | |
| 7 | Run weeks in simulation; decision point: does it beat the price net of fees? | |
| 7+ | Profitability levers (selectivity, intraday, threshold tuning, exit logic, more cities) | |
| 8 | Optional observe-only cross-platform arbitrage scanner | |
| 9 | GATED go-live (legal + code + evidence gates) — not a simple next step | |

Re-run the scoreboard after every change. One change at a time, always keep it running.

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
   100%. Tests: `tests/test_forecast_distribution.py`. **Still deferred:** per-station bias
   correction and climatology blend — both need forecast-vs-actual history the sim will only
   produce by running, and at 0–1 day lead climatology adds little. `_fraction_in_range` is
   kept as a raw reference but is no longer the traded probability.

6. **No fees anywhere (Phase 6).** Edge threshold and simulated P&L ignore fees and
   spread; there is no fee field on the trade record.

### Minor cleanup (catch along the way)
- `database.py ensure_schema()` swallows schema-migration errors silently — make it log
  loudly (matters when adding the fee column in Phase 6).
- Date parser assumes current year when a title omits it — wrong-year risk near New Year.
- Weather exposure cap ($500) is hard-coded in the scheduler, not in config — move it.
- Dead weight: unused observed-temp function, unused `ScanLog` table, an uncalled
  Groq/AI hook in the weather path, a configured-but-unscheduled weather-settlement timer.
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
- **Keep:** all frontend (display only).

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
