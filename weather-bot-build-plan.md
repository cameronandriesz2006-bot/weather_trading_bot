# Weather Bot — Build Plan & Walkthrough

A start-to-finish plan for the forked prediction-market weather bot, written to follow
when you're at a computer. Plain English throughout.

**Three principles guiding everything below:**

1. **Keep the skeleton, replace the brain.** The plumbing (reading markets, grading
   results, storage, dashboard, scheduler) works — reuse it. The decision-making
   (forecast, bet-reading) is flawed at the design level — rewrite those as clean
   modules that plug into the existing skeleton.
2. **Measure first, get clever last.** Fix the scoreboard and the "quietly wrong" bugs
   before adding any sophistication. Adding better models on top of a wrong location
   just makes the bot confidently wrong faster.
3. **Everything runs in simulation.** The scoreboard — not a hunch — decides whether the
   model is good. Going live is a separate, gated step at the very end (see Phase 9).

---

## Phase 0 — Setup (first session, ~half a day with troubleshooting)

Goal: get the bot running **unchanged**, in simulation, so we have a working baseline
before touching anything.

1. Install the tools (I'll give exact commands and explain each):
   - **Git** — downloads and version-controls the code.
   - **Python** — runs the backend (the bot's brain).
   - **Node.js** — runs the frontend (the dashboard).
   - **Claude Code** — lets me read and edit the actual files with you.
2. Clone your fork of the repo to your machine.
3. Create a Python virtual environment and install the backend dependencies.
4. Start the backend, then the frontend. Open the dashboard in your browser.
5. **Confirm it runs as-is.** Let it sit for a few minutes. You'll see it scanning weather
   markets and (maybe) placing simulated trades. Expect the calibration panel to look
   broken — we now know exactly why.
6. Commit this as your known-good starting point.

*Why: if something breaks after we start changing things, we know it was us, not a
setup problem.*

---

## Phase 1 — Cut crypto cleanly (1 session)

Goal: remove the Bitcoin strategy without breaking weather.

- **First**, move the two shared math helpers (`calculate_edge`, `calculate_kelly_size`)
  out of the crypto file into a new shared module — the weather code depends on them.
- Remove the BTC strategy file, the BTC market fetcher, the crypto price module, and the
  BTC scan job in the scheduler.
- Update config to drop BTC-only settings.
- **Confirm it still runs**, weather-only, in simulation.

---

## Phase 2 — Fix the scoreboard (1 session) — THE LINCHPIN

Goal: make the measurement honest. Nothing after this is judgeable until this works.

- Fix the "yes/no" vs "up/down" label bug so weather predictions get scored against the
  right vocabulary. (Cleanest fix: normalize direction to one convention everywhere, or
  translate at the settlement step.)
- Confirm the calibration **accuracy** and the **prediction-vs-reality curve** now
  populate correctly for weather (the Brier score already works).
- Optional: fix the manual "simulate trade" button so it can also handle weather and
  links trades to predictions (the automatic engine already does this right).

*After this, the scoreboard tells the truth — which is the whole point of the project.*

---

## Phase 3 — Correctness fixes (1–2 sessions) — stop being quietly wrong

These are the bugs where the bot trades on a misunderstanding.

- **Right location.** Point each city's forecast at the exact station the bet settles on,
  and check Kalshi and Polymarket separately (they may use different stations).
- **Right day.** Tell the forecast service to measure the daily high over the *local*
  day, not a UTC day (one setting).
- **Read the bet correctly.** Handle "between X and Y" range markets; make the parser
  *skip* anything it can't read cleanly instead of silently guessing "high/above"; on
  Kalshi, read the structured boundary fields the exchange already provides.

*Re-run the scoreboard after each change.*

---

## Phase 4 — Honest probability (1–2 sessions) — calibration

Goal: stop the forecast being overconfident.

- Replace raw member-counting with a fitted distribution using the average and a
  sensibly **widened** spread (the code already calculates these and throws them away).
- Make it **less certain the further out the day is** (lead-time-dependent uncertainty).
- Subtract the model's **known bias** using recent forecast-vs-actual error at each
  station.
- For days far out (where forecasts are shaky), lean partly on **climatology** — the
  normal high for that date and place over the last 10 years — so one weird forecast run
  can't fool the bot.

*Re-score after each — keep what improves the numbers, drop what doesn't.*

---

## Phase 5 — Stronger model (2–3 sessions)

Goal: genuinely better forecasts.

- Add the **European (ECMWF)** and **German (ICON)** models alongside GFS and blend them.
- Add **intraday conditioning** — fold in the temperature already observed during the day.
  This is where the real, low-risk edge tends to live near settlement.

*Re-score after each.*

---

## Phase 6 — Real costs (1 session)

Goal: make "profit" mean profit.

- Subtract **fees and the spread** before checking against the edge threshold, and in the
  simulated profit/loss. Add a fee field to the trade record.
- Re-tune bet-sizing now that the probabilities are honest (it currently over-bets when
  the model is overconfident).
- Close the gap where the daily-loss safety stop guards crypto but not weather.

---

## Phase 7 — Run and evaluate (weeks, hands-off)

Goal: let the evidence decide.

- Run weather-only in simulation for several weeks.
- Read the scoreboard: Brier score, the calibration curve, and **net-of-fees** profit.
- **Decision point:** Is the model actually beating the market price after costs?
  - If yes → you have something real, and Phases 8–9 become worth considering.
  - If no → you found out for free, before risking a cent.

---

## Phase 7+ — Profitability levers (only after the foundation works)

All of these come *after* Phase 7. Tuning or expanding before the scoreboard is honest
just multiplies confident nonsense across more markets. Listed biggest-first:

1. **Selectivity beats coverage.** The highest-value lever isn't more cities — it's
   trading only where your edge is largest *and* the market is liquid enough to fill
   without moving the price. Fewer, better bets usually beat more bets, because fees and
   thin-market slippage quietly eat the marginal ones. Use the scoreboard to find the
   edge level above which you're actually profitable, and only trade above it.

2. **Intraday conditioning (the likely real money).** Listed under Phase 5, but probably
   the single highest-profit change in the whole plan. Near settlement, the day's high is
   largely fixed by what's already been observed, and the market is often slow to fully
   price that. A model that conditions on observed-so-far can be near-certain and
   low-risk. If the edge lives anywhere, it's most likely here — treat it as a priority.

3. **Tune the thresholds empirically.** The 8% edge cutoff and the 15% bet-sizing
   fraction are guesses. Once the scoreboard is honest, let it tell you the
   profit-maximizing values instead of using the defaults.

4. **Exit logic (new — not in the current bot).** Today it only enters and holds to
   settlement. But prices move: if you bought at 40c and it's 85c well before settlement,
   selling locks the gain and frees capital instead of risking a late forecast swing.
   Adding the ability to sell early is a real profitability lever — more complex, squarely
   post-foundation.

5. **Expand cities — last, and carefully.** Only after the model is calibrated, and only
   with each new city's correct settlement station verified. More cities widens coverage
   but doesn't improve edge; it's a scale step, not a quality step.

6. **Re-trade as the forecast sharpens.** Instead of one bet per market, the bot could
   add or adjust as the day's forecast firms up. Useful, but only once exit logic and
   selectivity exist.

*Whole-section rule: change one lever at a time and re-read the scoreboard. Keep what
moves net-of-fees profit; discard what doesn't.*

---

## Phase 8 — Arbitrage track (optional, after the foundation is solid)

Arbitrage doesn't depend on the forecast being good, so it's a separate track — but it
needs the **fee math, bet-reading, and settlement-matching from Phases 3 & 6 done first**,
because arb margins are tiny and fake arbs come from mismatched settlement or ignored fees.

- Build an **observe-only** scanner that watches both platforms and logs only genuine,
  fee-cleared, settlement-matched opportunities — no trading.

  **Before the scanner flags anything as a real opportunity, it must pass all of these:**
  - **Settlement match (the whole ballgame).** The two markets must settle on the *exact*
    same rule — same station, same threshold, same day, same rounding. This is the trap:
    two "identical-looking" markets that actually resolve differently aren't an arb at all
    — your hedge isn't locked, and **both legs can lose at once.** If you can't confirm
    they settle identically, it's not an opportunity, full stop.
  - **Fee-cleared.** The locked profit must survive both platforms' fees *and* the spread.
    A 2-cent gap with 1 cent of fees per side is a loss, not an arb.
  - **Liquidity.** Enough size available on both sides to actually fill the hedge without
    moving the price.

- Run it for a couple of weeks. Do real opportunities appear, and at what size?
- Only if they do → consider building execution (hard: needs both legs filled near-
  simultaneously — "legging risk" means if one side fills and the other moves first,
  you're suddenly making a one-sided bet, not an arb — and the bot has nothing for this
  yet).

*Reality check: true cross-platform arbs here are rare, small, and hunted by faster bots.
Treat the scanner as a cheap experiment, not a sure thing.*

---

## Phase 9 — Going live (GATED — not a simple next step)

This phase is conditional and honest about it:

- **Legal gate.** Real trading requires being genuinely resident somewhere these
  platforms are permitted. They are blocked/illegal in Singapore, Malaysia, and Thailand;
  the US and UK are open. A VPN does not change the law. This isn't a step you can just
  do from where you are now.
- **Code gate.** There is currently *no* real-trade execution anywhere in this codebase —
  it only reads and simulates. Going live means building and testing a whole execution
  layer that doesn't exist.
- **Evidence gate.** Only worth doing if Phase 7's scoreboard showed a real, net-of-fees
  edge.
- If all three are met: start with tiny size, hard caps, and a kill switch; scale only if
  live results match the paper results.

---

## How we'll work together

- **Claude Code** runs in your terminal and reads/edits the real files. I propose each
  change and explain it in plain English; **you** approve and run it; you paste back what
  happens (including errors) and we go from there.
- **One change at a time, always keep it running.** You stay in "it works, now improve one
  thing" mode the whole way — easiest to follow and debug.
- I can't run commands or click for you. I'm the guide; you're the hands.

---

## Keep / Fix / Rebuild — per file

| File | Decision | Why |
|---|---|---|
| `kalshi_client.py` | **Keep** | Clean, correct login + read-only data. |
| `database.py` | **Keep + extend** | Solid schema; add a fee field. |
| `settlement.py` | **Keep + fix** | Grading is sound; fix the yes/no label bug, add fees. |
| `scheduler.py` | **Keep + fix** | Good loop; remove BTC job, fix weather loss-stop gap. |
| `main.py` (API/dashboard) | **Keep** | Works; optional small fix to the manual trade button. |
| `config.py` | **Keep + edit** | Drop BTC-only settings; add model + fee settings. |
| `kalshi_markets.py` | **Fix** | Reliable base; add range handling + structured fields. |
| `weather_signals.py` | **Fix** | Re-wire to the new probability source. |
| `signals.py` | **Salvage + delete** | Keep the 2 shared math functions; delete BTC logic. |
| `weather.py` (forecast core) | **Rebuild** | Single-model, raw, wrong-location — rebuild as multi-model, calibrated, station-correct. |
| `weather_markets.py` (Polymarket reader) | **Rebuild** | Fragile title-guessing; rebuild to handle ranges robustly. |
| `crypto.py`, `btc_markets.py`, `markets.py` | **Delete** | Crypto — being cut. |
| frontend files | **Keep** | Display only; no logic problems found. |

*Note: the keep/fix/rebuild calls come from the files we've read (most of the backend).
If a piece turns out messier than it looked once we're in it, we switch that one to a
rebuild — the decision is per-module, not all-or-nothing.*

---

## Known minor issues & cleanup (catch these along the way)

None of these are urgent, but they were found in the review and shouldn't get lost:

- **The database patches its own schema and hides failures.** When it adds a missing
  column it silently swallows any error. Since we'll be adding a fee column (Phase 6),
  watch this: if a schema change appears not to take effect, this silent-failure is the
  first suspect. Worth making it log loudly instead of swallowing.
- **Date-reading assumes the current year** when a market title gives no year. Near the
  turn of the year this can produce a wrong-year date. Harden it when we touch the parser.
- **The weather exposure cap ($500) is hard-coded**, not in the settings file. Move it to
  config so it's tunable like everything else.
- **Dead weight to remove or ignore:** the unused observed-temperature function in the
  forecast file, a defined-but-unused scan-logging table, an AI/Groq hook that's never
  actually called in the weather path, and a configured-but-unscheduled weather-settlement
  timer (harmless — the general settlement sweep already grades weather trades).
- **Verify the Kalshi web address** in the client is the current one before relying on the
  Kalshi side (it's the lower-priority, region-blocked platform anyway).

---

## Rough effort

Phases 0–6 are roughly **8–12 short sessions** for a beginner working with me, spread
however suits you. Phase 7 is mostly waiting. Phases 8–9 are separate efforts you only
start if the evidence justifies them.

The honest finish line for this whole project isn't "it runs" — it's **"the scoreboard
says it beats the price, net of fees."** Everything before Phase 7 exists to get you to
that one honest number.
