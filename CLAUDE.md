# CLAUDE.md — Polymarket bot

The previous version of this file (~15KB, almost entirely weather-bot operating detail) is in
`git log` — that strategy is now archived, and this file leads with what is actually open.

## Where this project actually is (2026-07-27)

Two strategies have been tried on Polymarket. **One is dead, one is open.**

| | status |
|---|---|
| **Weather forecasting bot** (Edge-2) | **FAILED** its go/no-go 2026-07-26 at n=49, −$287. Taker leg off; the service still runs in shadow mode (prices + settles, opens nothing). Everything about it is in `archive/weather-bot/` — **you almost certainly do not need to read it.** |
| **negRisk arb** | **OPEN — awaiting audit.** A P&L replay of 15.4h of live scanning says $106/day headline, but one 2-second trade is 60% of it and fill realism is untested. `ARB_PNL_2026-07-27.md`. |

**If you are the audit session: read `ARB_PNL_2026-07-27.md` first.** It has the method, the
numbers, and a ranked list of where to attack them. Then
`AUDIT_2026-07-26_negrisk_arb_VERDICT.md` for what was already verified and should not be
re-litigated (token mapping, partition structure, snapshot coherence, the fee itself).

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
| `negrisk-arb.service` | sweeps all ~135 boards every 2s, `--loop 2 --quiet` | 0 restarts since 2026-07-26 13:41; writes `logs/negrisk_arb.jsonl` |
| `weatherbot.service` | the failed bot, shadow mode, port 8000 | **0 open positions** (all 123 trades settled) — nothing depends on it staying up |
| `claude-remote.service` | phone access to Claude Code in this repo | |

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
  net-of-fee optimiser (`best_subset_net` / `best_set_size_net`), sweep loop.
- `backend/data/negrisk_arb_pnl.py` — replays the log as executions; `--until` pins a window for
  reproducibility, `--episode-gap` / `--gas` expose the judgment calls.
- `backend/data/negrisk_arb_report.py` — window/opportunity reporting.
- `backend/data/orderbook.py` — live CLOB book fetch + VWAP fill walk. **Shared with the weather
  bot; not archivable.**

Weather bot (shadow mode — `backend/api/main.py`, `backend/core/*`, `backend/data/weather*.py`,
`backend/data/kalshi_*.py`, `backend/models/database.py`, `run.py`, `frontend/`, `tests/`). Left
in place only because the service is still up. See `archive/weather-bot/README.md`.

## Working agreement

One change at a time, explained in plain English, keep the services running. Answers stay
concise. Judge on measured numbers, not on whether it runs — and say plainly when a number is
carried by a single observation.
