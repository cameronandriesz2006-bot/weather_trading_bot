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
  flip it until ready to go live.
- **Fix forecasts and measurement
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
| 9 | GATED go-live (connect to polymarket and run the bot live)| |

Re-run the scoreboard after every change. One change at a time, always keep it running.

## Current state (2026-06-14)

Model-correctness + cost work (Phases 1–7) is done. We are now in **Phase 7
(run-and-evaluate)**: the scoreboard was **reset to a clean slate** so it contains only
**current-model** trades (liquidity/spread-gated, bias-corrected, cash-staked). Let it run
and read the scoreboard.

### Forecast-calibration work (2026-06-14, third pass)
The bot was producing many large (20-40%) edges that were **mostly mirages that would LOSE**:
our forecast mean sits up to ±3° off the **market-implied mean** per station (NYC/LA warm,
Miami/Paris cold), and on 1°C/2°F buckets a 2-3° shift manufactures a huge fake edge. Root cause
is **per-station forecast error vs the actual settlement station**, NOT an Open-Meteo bug. Two fixes:
- **Market-gap guardrail — DONE (issue 12).** The market-implied mean (probability-weighted center
  of the live bucket prices) is a near-truth estimate of the settling value on a near-settlement day.
  If our bias-corrected forecast mean differs from it by more than `WEATHER_MAX_MARKET_GAP_F` (2.0°F,
  scaled 1/1.8 for °C), we suppress the WHOLE event — we're almost certainly the miscalibrated one,
  not holding free money. `compute_event_market_means` (scan, post-live-price) sets
  `WeatherMarket.event_market_mean`; `WeatherTradingSignal.market_gap`/`market_gap_ok` gate it in
  `passes_threshold` with a reasoning note. Live: suppresses Paris/Shanghai/Miami/LA/NYC-warm events;
  actionable ~18→~14. The mean is the easy part the market nails same-day — a real edge must come from
  distribution SHAPE around a similar mean, not from disagreeing on the level by several degrees.
- **Station-truth bias — DONE (issue 13).** The old bias backfill calibrated GFS against **ERA5
  reanalysis**, but ERA5 is itself gridded and agrees with GFS to <1° while differing from the
  official station by 2-3° (worst at coastal/microclimate stations a coarse grid can't resolve —
  LAX, Miami). So the real gap (to the station the market settles on) went uncorrected. Now
  `bias_backfill.py` uses **realized Meteostat station observations** (`d.meteostat.net` daily, current
  + global + keyless) as the "actual", per the station nearest each market's settlement point, and
  computes bias in each city's **native unit** (°F/°C). `get_station_bias` scales the safety cap by
  1/1.8 for °C. Two guards make it robust:
    - **Source-consistency guard.** The 60-day forecast history comes from historical-forecast-api,
      but the bot trades on **ensemble-api**. For coastal coords those two APIs snap to different grid
      cells and disagree by ~5° for the SAME `gfs_seamless` (historical-forecast is the *deterministic*
      run; the live model is the *ensemble mean*). We only keep a city's bias if the two agree on the
      most-recent 3 days within `CONSISTENCY_MAX_F` (2°F); else skip (the guardrail still covers it).
      → kept: nyc/chicago/miami/denver/london/paris; **skipped (coastal): LA, Tokyo, Seoul, HK**;
      shanghai skipped (no obs station within ~35km of ZSPD Pudong).
    - Native-unit storage + the existing min-samples/clamp gates.
  Run `python -m backend.data.bias_backfill`; writes `station_bias.json` (method
  `gfs_seamless_vs_meteostat_station_obs`).

#### STILL OPEN — σ too wide near settlement (the next mirage class)
After #1+#2 the **mean-shift** mirages are gone, but a second class remains: events where our mean
MATCHES the market (small gap) yet we assign far less probability to the market's favoured bucket —
e.g. Tokyo 21°C (NO, net 50%, gap 1.0; market 80% vs our 27%) and NYC 78-79°F (NO, net 23%, gap 0.1;
market 44% vs our 17%). Cause: our `sigma_eff` floor (2.0°F / 1.5°C, plus 0.7°F/lead-day) is too WIDE
for a near-settlement high (which is nearly determined), so we stay diffuse while the market correctly
concentrates. The mean-gap guardrail can't catch this (the mean agrees). **Do NOT blind-tune σ** — it
needs the scoreboard / **intraday conditioning** (Phase 5/7+: condition on observed-so-far near
settlement) to fix correctly. This is now the top forecast-correctness lever.

### Latest changes (2026-06-14, second pass)
- **Min traded-volume gate — DONE.** Added `WEATHER_MIN_VOLUME` ($500) to
  `passes_threshold` (`weather_signals.py`). **Liquidity ≠ volume:** liquidity is $ *resting*
  in the book (`liquidityNum`), volume is $ *actually traded* (`volumeNum`). A market can show
  $1.4k resting while only ~$70 has ever traded (e.g. LA `72-73°F` Jun15) — those quotes are
  likely a lone market maker that can vanish, and adverse selection is high; the static
  order-book sim can't see that risk. $500 was chosen from the **live volume distribution** (it
  sits in the natural $414→$756 gap), not for symmetry with the liquidity floor. Live effect:
  actionable 19→13. **Gate-level caveat (see below):** $500/$500 are fine for *evaluation* but
  are deliberately permissive; raise both before live/scaled trading.
- **Second scoreboard reset (clean test of the volume-gated model).** All trades + signals
  deleted, bankroll → $10k, counters → 0. This reset cost **nothing realized**: at reset time
  all 28 open trades were unsettled ($0 P&L, bankroll untouched), so the scoreboard was empty —
  but several open positions were in markets the new volume gate rejects (LA, Tokyo), so keeping
  them would have written the *first* scoreboard rows for trades the current model wouldn't take.
- **Mark-to-market "Now" price was stale — FIXED.** The dashboard's live price + unrealized P&L
  read Gamma's cached `outcomePrices`, which is **badly stale on thin daily-temperature markets**
  (observed Shanghai `22°C` NO at 0.42 when the live book was ~0.65 — a 23¢ error). Gamma's
  `bestBid`/`bestAsk`/`lastTradePrice` were *also* stale. **Only the live CLOB `/book` is ground
  truth.** `_fetch_outcome_prices` (`api/main.py`) now looks up the market's `clobTokenIds` and
  marks each side at its live CLOB **mid** (`orderbook.fetch_book_top`), falling back to Gamma
  `outcomePrices` only if the book is unavailable. (This is the same reason the signal generator
  already walks the live book for fills — issue 7.)
- **Entry edge screen now uses the LIVE book too — DONE.** Previously the screen read
  `yes_price`/`no_price` from Gamma `outcomePrices` and only *candidates* got the live-book walk,
  so a stale Gamma mid could hide a real edge and the bucket was never walked. Now the scan fetches
  every market's **full live book up front** via the CLOB **batch `POST /books`** endpoint
  (`orderbook.fetch_books` → `LiveBook{top, asks}`) and refreshes each market's price fields
  (`_apply_live_top`) before the edge screen. The same pre-fetched books feed the exact-fill walk,
  so the per-bucket pass needs **zero extra round-trips**. Performance: a naive per-token refresh
  was ~280 requests (~50s, rate-limited); batched it's ~3 requests (~0.7s), warm-cache scan ~2.2s.
  Effect: actionable rose ~13→~19 (real edges Gamma's stale prices were hiding). `generate_weather_signal`
  gained `refresh_prices` (scan passes `False`, having batch-refreshed) and `books` (the pre-fetched
  dict); standalone callers still refresh per-market via `_refresh_market_prices_live`.
  *Aside (pre-existing, untouched):* a cold process pays ~45s once for the **sequential** Open-Meteo
  forecast pre-warm; it's cached warm thereafter. Could be made concurrent later — separate issue.

- **Scoreboard reset.** All old trades + signals deleted (they were placed by the
  pre-gate / pre-bias / contract-count model and would pollute the test); bankroll back to
  $10k. A check showed 9 of the old 10 trades would be rejected by the current gates.
- **Exposure scaled up for faster data:** `WEATHER_MAX_ALLOCATION` $500→**$2,000** (~20% of
  bankroll; covers all actionable opportunities); `DAILY_LOSS_LIMIT` $300→**$750** in step so
  the breaker doesn't stall data collection. `MAX_TRADE_SIZE` $75 and `KELLY_FRACTION` 0.10
  unchanged. `MAX_TRADES_PER_SCAN`=3 (hard-coded throttle; only paces the ramp).
- **Cap display — DONE.** `/api/stats` now returns `weather_max_allocation`, `daily_loss_limit`,
  `daily_pnl`, and `settled_trades`; `App.tsx` reads the real cap (no more hard-coded "/$500"),
  added a **Daily-loss** status card ($ lost today / limit, red when the breaker trips), and the
  **win rate** is now over RESOLVED trades (was divided by `total_trades`, which counts open
  positions).
- **International cities (Celsius) — DONE.** Seoul/Tokyo/London/HK/Paris/Shanghai are now traded
  alongside the US markets (`WEATHER_CITIES` extended). They resolve in **°C** with **single-degree**
  buckets ("18°C") vs the US °F **two-degree** ranges ("82-83°F"). Design principle: **no temperature
  is ever converted** — each city has a native `unit` ("F"/"C") in `CITY_CONFIG`; the forecast is
  fetched in that unit (`temperature_unit`), buckets are parsed in that unit, and the existing
  ±0.5 rounding integral runs natively (0.5°C for °C). `parse_bucket_label` now reads °C/°F unit
  letters + single-degree labels → `(N,N)` → interval `[N-0.5, N+0.5)`; this is correct for both
  the whole-degree cities (London/Seoul/Paris/Shanghai, Wunderground) AND HK (one-decimal, HKO).
  Settlement stations per the markets' own resolution text: London City (EGLC), Tokyo Haneda (RJTT),
  Seoul→**Incheon** (RKSI), Paris→**Le Bourget** (LFPB), Shanghai→**Pudong** (ZSPD), HK→**HKO HQ**.
  The σ-floor constants (defined in °F) are scaled by 1/1.8 for °C cities (exact for a temperature
  *spread*). Polymarket settles from its own market outcome (price-based), so units never enter
  settlement. Live check: forecasts are sane °C, bucket probs form a clean bell curve summing ~1,
  intl books carry $2-4k liquidity vs ~$0.8k US. Tests: `tests/test_celsius_markets.py`.
  **Still deferred:** per-station **bias** for the °C cities (cold-start 0 = no-op until the backfill
  is run in °C — `bias_f` must be stored in the city's native unit); intraday conditioning.

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
- **Settlement never fired (resolved trades stuck "active") — FIXED.** Two bugs in
  `settlement.py`: (1) it graded against `markets[0]` of the event instead of the specific
  bucket we hold (`market_id`), and (2) it required Polymarket's `closed` flag, but
  daily-temperature events stay `closed: false` for hours/days after the day's high/low is
  fixed — the outcome shows only as the price going to the rails (~0.9995/0.0005). Now it
  matches the exact bucket by id and settles when EITHER `closed` is true OR the target local
  day is over AND the price is decisive (>0.99/<0.01). Bankroll/win-rate/scoreboard update via
  the existing settlement job once settled.
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
   extended in `tests/test_weather_signal_costs.py`.
   **Exact order-book fills (Layer 2(ii) + Layer 3) — NOW DONE.** Instead of assuming we
   fill the whole order at the top-of-book ask, candidate signals now WALK THE REAL CLOB
   ORDER BOOK and pay the exact VWAP across consumed levels — no modelled slippage curve,
   the actual fill against the book as quoted. `backend/data/orderbook.py`
   (`fetch_ask_levels` + `walk_asks_for_cash` + `simulate_market_buy`) fetches a token's book
   from `https://clob.polymarket.com/book` and consumes asks cheapest-first for the Kelly cash
   amount; `WeatherMarket` now carries `token_id_yes`/`token_id_no` (parsed from Gamma
   `clobTokenIds`), and `generate_weather_signal` replaces the estimated entry with the VWAP,
   recomputing `cost = (vwap − side_mid) + fee` and `net_edge`. Effect is dramatic on thin
   buckets: a 6¢ best ask can fill at a ~19–35¢ VWAP, so many top-of-book "edges" correctly
   collapse below threshold (live actionable dropped to ~4–5). **Platform-generic by design:**
   it keys off `clobTokenIds` + the live book, entirely in price/cash space (no city or °F/°C
   coupling), so it works for ANY Polymarket market we scan later (more-liquid US or
   international) — liquid books fill near the best ask with ~0 slippage; thin books slip. Only
   CANDIDATES are walked (walking can only lower edge, so sub-threshold buckets are skipped),
   and the scan shares ONE pooled HTTP client and runs concurrently — without that a fresh
   client per bucket was ~16s/scan; now ~1.5s. Tests: `tests/test_orderbook.py`. **Still
   deferred:** the NO side uses its own (mirror) book directly; Kalshi has no `clobTokenIds`
   so it still falls back to the spread estimate (needs its own book walker when enabled).
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
