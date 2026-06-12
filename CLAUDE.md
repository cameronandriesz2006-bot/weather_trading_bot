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
| 2 | **Fix the scoreboard (THE LINCHPIN)** — the yes/no vs up/down bug | **next** |
| 3 | Correctness — right station per platform, local-day high, robust market parsing (ranges, skip-don't-guess, Kalshi structured fields) | |
| 4 | Honest probability — fitted+widened distribution, lead-time uncertainty, bias correction, climatology blend | |
| 5 | Stronger model — add ECMWF + ICON and blend; intraday conditioning | |
| 6 | Real costs — subtract fees+spread before edge check and in P&L; add fee field; re-tune sizing; weather daily-loss stop | |
| 7 | Run weeks in simulation; decision point: does it beat the price net of fees? | |
| 7+ | Profitability levers (selectivity, intraday, threshold tuning, exit logic, more cities) | |
| 8 | Optional observe-only cross-platform arbitrage scanner | |
| 9 | GATED go-live (legal + code + evidence gates) — not a simple next step | |

Re-run the scoreboard after every change. One change at a time, always keep it running.

## Key bugs / known issues

1. **Scoreboard label mismatch (LINCHPIN, Phase 2).** Weather signals store
   `direction = "yes"/"no"` (`backend/core/weather_signals.py:80`, persisted `:226`).
   Settlement sets `actual_outcome = "up"/"down"` and grades
   `outcome_correct = (signal.direction == actual_outcome)`
   (`backend/core/settlement.py:276-278`). `"yes" == "up"` is always False, so **every
   weather prediction is marked wrong**. This breaks calibration accuracy and the
   prediction-vs-reality curve. (Brier score still works — it uses the numeric
   `model_probability` vs `settlement_value`, not the labels.) `calculate_pnl` is *not*
   affected — it maps up→yes/down→no and handles yes/no directly. Fix: normalize direction
   to one convention everywhere, or translate at the settlement step.

2. **Polymarket weather fetch finds 0 markets (Phase 3, market-reading).**
   `backend/data/weather_markets.py` queries Gamma with invalid params `tag=Weather` and
   `slug_contains=...`, which the API silently ignores → it returns a default feed of
   unrelated events and parses 0 temperature markets. The correct param is
   **`tag_slug=weather`**, which returns ~57 live daily-temperature events (NYC, Chicago,
   Miami, Denver, etc.). These are **bucketed range markets** ("87°F or below",
   "between 88-89°F", "106°F or higher"), not simple above/below binaries — the parser
   must handle ranges and skip what it can't read cleanly. Also: `"low" in title` matches
   the substring in "be**low**", misclassifying "or below" markets as low-temp markets;
   and the past-date filter rejects same-day markets dated "yesterday" in UTC.

3. **Wrong location/station (Phase 3).** `CITY_CONFIG` (`backend/data/weather.py`) uses
   one station per city, but Kalshi and Polymarket may settle on *different* stations.
   Point each city's forecast at the exact station the bet settles on, per platform.

4. **UTC vs local day (Phase 3).** Open-Meteo daily max/min is computed over a UTC day,
   not the market's local day. One setting on the forecast request.

5. **Overconfident forecast (Phase 4).** Probability = raw fraction of ensemble members
   past the threshold; `mean`/`std` are computed in `EnsembleForecast` then thrown away.
   Use a fitted, sensibly widened distribution instead of raw member-counting.

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
