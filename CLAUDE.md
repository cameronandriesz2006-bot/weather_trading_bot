# CLAUDE.md — Polymarket bot

The previous version of this file (~15KB, almost entirely weather-bot operating detail) is in
`git log` — that strategy is now archived, and this file leads with what is actually open.

## Where this project actually is (2026-08-02)

Two strategies have been tried on Polymarket. **One is dead, one is open.**

| | status |
|---|---|
| **Weather forecasting bot** (Edge-2) | **FAILED** its go/no-go 2026-07-26 at n=49, −$287. Taker leg off; the service still runs in shadow mode (prices + settles, opens nothing). Everything about it is in `archive/weather-bot/` — **you almost certainly do not need to read it.** |
| **negRisk arb** | **TIER A+B DONE 2026-08-02 — conditional pass, no kill.** Honest executable number: **$9.78/day** full-window (batch+FOK $6.65 buy + $3.13 short) but only ~**$4.3/day** on the late-window run-rate — straddles the $5 bar. Execution design is now FIXED by measurement: batch of 11 per-order-FOK orders in one `POST /orders`, NEVER sequential (13.6% broken sets, −EV), never abort. Two things decide go/no-go: Mon 08-03/Tue 08-04 arrival counts (weekly-cycle vs structural decline) and the per-order rejection rate (D11's real output). **This box is GEO-BLOCKED (SG, close-only)** — Tier C/D require a ~$6/mo VPS (Amsterdam/Dublin, NOT London). Short side: `convertPositions` gives instant cash recycle + benign partials (start D-tier there). Buy side: capital provably locked to resolution. All of it: `TIER_AB_RESULTS_2026-08-02.md`. |

**Read `TIER_AB_RESULTS_2026-08-02.md` first** — it carries the reviewed Tier-A/B verdicts and
supersedes the *numbers* in `GOLIVE_TESTPLAN_2026-08-02.md` (whose tier structure and standing
decisions still stand). The audit verdicts (`AUDIT_2026-07-27…`, `AUDIT_2026-07-26…`) still hold
for method and for what must not be re-litigated (token mapping, partition structure, snapshot
coherence, the fee itself).

**Next action: read Mon 08-03/Tue 08-04 arrival counts** (free tiebreak on the decay: ≥230
episodes/24h = weekly cycle → bar passes; ≤160 = structural → marginal). VPS purchase + Tier C
build are gated on that read and on the user's ToS/residency call.

## The arb, in plain terms

A Polymarket daily-temperature event is 11 mutually-exclusive, exhaustive buckets — exactly one
must win. Two order-book identities follow, needing no weather knowledge at all:

- **BUY the board** — if all 11 YES asks sum to under $1, buy them all and one pays $1. Requires
  **exhaustiveness**: skip a leg and the trade can pay nothing.
- **SHORT a subset** — buying NO on m mutually-exclusive buckets pays at least m−1. Requires
  **exclusivity** only.

`board_sanity()` proves both per board before anything is scored; 156/156 live boards pass.

**The taker fee is the whole story.** `fee = shares · 0.05 · p · (1−p)`, **takers only** (makers
pay 0 and earn a 25% rebate) — `feeType: "weather_fees"`, verified on all 1,639 markets. It is a
**per-leg tax while the edge is not**, so shorting 9 legs to collect 0.7¢ pays ~3.4¢ of fee and
loses. This is why the "arb" stayed visible for 30+ minutes on a platform full of arb bots: the
persistence *was* the evidence of an unmodelled cost. Never price it as a flat rate — a flat
fraction cannot express the shape. Use `sizing.taker_fee_per_share` (price units, for
gating/sizing) or `taker_fee_on_cash` (dollars, for booking). As a fraction of notional it is
`0.05·(1−p)`: ~0.5% on a 90¢ favourite, ~4.5% on a 10¢ tail.

**On selection there is no loss mode** — the scanner emits only net-of-fee-positive rows at real
depth. All loss risk is in execution: a partial fill on an 11-leg set can pay **$0**.

## Live services on this machine

| service | what | notes |
|---|---|---|
| `negrisk-arb.service` | sweeps all ~140 boards every 2s, `--loop 2 --quiet` | **the only trading process left.** Running since 2026-07-29 06:21; writes `logs/negrisk_arb.jsonl` (~55MB). **Do not stop it — the sample is the asset.** |
| `claude-remote.service` | phone access to Claude Code in this repo | |

`weatherbot.service` was **stopped and disabled 2026-07-27** — 123 trades, all settled, −$477.85
final. Its code and DB are in `archive/weather-bot/`. Do not restart it.

## Hard constraints (do not violate)

- **SIMULATION ONLY.** `SIMULATION_MODE` stays `True`. There is **no live-execution path** — going
  live is a build (order signing / submission / reconciliation), deferred until a simulation
  proves an edge.
- **`.env` overrides `config.py`** (pydantic-settings). Any config change must check `.env` first.
- **Preserve `calculate_edge` / `calculate_kelly_size`** in `backend/core/sizing.py`.
- Control endpoints (`/api/bot/*`, `/api/run-scan`, `/api/settle-trades`) are **unauthenticated** —
  gate before any non-local deploy.

## Architecture quick map

Arb (current):

- `backend/data/negrisk_arb_scan.py` — the scanner: board enumeration, `board_sanity`, depth-honest
  net-of-fee optimiser (`best_subset_net` / `best_set_size_net`), sweep loop, and `shadow_probe`
  (re-fetches every hit board ~1s after scoring, logs `shadow` rows = measured fill survival).
- `backend/data/negrisk_arb_pnl.py` — replays the log as executions; `--until` pins a window for
  reproducibility, `--episode-gap` / `--gas` / `--credit-row` expose the judgment calls
  (default crediting is the audited row-2-survival, not arrival).
- `backend/data/negrisk_shadow_report.py` — shadow-probe survival report (per-probe and
  per-episode); the input to the go/no-go (first read 2026-08-02 — see the test plan).
- `backend/data/negrisk_legging_sim.py` — test A2: replays every buy episode as an 11-order
  execution (sequential vs batch, FOK vs partial, swept latencies); the reason batch-only is a
  hard rule. Audited + reproduced 2026-08-02; method in `reports/tier_ab/A2_legging.md`.
- `backend/data/negrisk_arb_report.py` — window/opportunity reporting.
- `backend/data/orderbook.py` — live CLOB book fetch + VWAP fill walk.
- `backend/core/sizing.py` — `taker_fee_per_share` / `taker_fee_on_cash` (canonical fee math),
  `calculate_edge` / `calculate_kelly_size`.

Four files survive from the weather bot **because the arb needs them** and are not archivable:
`orderbook.py`, `weather_markets.py` (`parse_bucket_label`), `weather.py` (lazily imported by
`weather_markets.py`, and it loads its `station_bias*.json` neighbours by path), and `sizing.py`.
Everything else — the FastAPI app, scheduler, settlement, signals, dashboard, test suite, trade
DB — is in `archive/weather-bot/`. See its README.

## Working agreement

One change at a time, explained in plain English, keep the services running. Answers stay
concise. Judge on measured numbers, not on whether it runs — and say plainly when a number is
carried by a single observation.
