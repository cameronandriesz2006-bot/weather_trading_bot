# Archive — the weather forecasting bot (failed)

Everything in here belongs to a strategy that **failed its go/no-go and no longer trades**. It is
kept for provenance, not for reference. **If you are auditing the negRisk arb work, you do not
need to read anything in this folder.**

## What failed, in one paragraph

The bot forecast daily high/low temperatures (GFS+ECMWF+ICON ensemble via Open-Meteo), converted
that into a probability for each Polymarket temperature bucket, and bet when its number
disagreed with the book. Two verdicts killed it. **2026-06-29** (`docs/AUDIT_2026-06-29.md`): the
broadly-deployed bot had no edge — its day-ahead distribution was 3-4× flatter than the market's,
manufacturing fake NO bets; the apparent "parity" was an in-sample-σ + look-ahead + Asia-leak
artifact. The one surviving seam — the inland same-day post-high nowcast, "Edge-2" — was isolated
and run live, and **failed its own go/no-go on 2026-07-26** at n=49: P&L −$287 with a CI not above
zero, Brier vs market a wash, and a fourth separate failure traced to a stale observation anchor.
The taker leg was switched off the same day (`WEATHER_TAKER_ENABLED=false`). Re-priced with the
taker fee that was later discovered to be missing, the all-time record is **−$449 over 120 trades**.

## Layout

| path | what |
|---|---|
| `research/` | 17 backtest / calibration / diagnostic harnesses (Edge-2 backtests, bias & σ refits, day-ahead liquidity studies, fillability report) |
| `research/edge1-freemoney/` | The **Edge-1** "locked-out bucket" scanner and its deps. A *different, also rejected* arb — measured at ~$15-30/mo at realistic latency. Named `arb_scan.py`; do not confuse it with the live negRisk work in `backend/data/negrisk_arb_*.py`. |
| `docs/` | The 06-29 audit, the original build plan, and the stale README / ARCHITECTURE / RESEARCH docs (last touched 2026-06-15) |
| `research_logs/` | Raw output from the backtest runs above |

## These scripts no longer run in place

They use absolute imports (`from backend.data.calibration_backfill import ...`) that only resolve
from `backend/data/`. To run one, move it back:

```bash
git mv archive/weather-bot/research/edge2_publish_honest.py backend/data/
```

Nothing live imports any of them — verified by import-closure analysis before archiving.

## What did NOT move, and why

`weatherbot.service` is **still running** on this machine in shadow mode (prices and settles, opens
no positions). Its code therefore stays in place: `backend/api/`, `backend/core/`,
`backend/data/weather.py`, `backend/data/weather_markets.py`, `backend/data/orderbook.py`,
`backend/data/kalshi_*.py`, `run.py`, `main.py`, `frontend/`, `tests/`, `tradingbot.db`.

Two of those files are also load-bearing for the arb scanner: **`orderbook.py`** (book fetch +
VWAP walk) and **`weather_markets.py`** (`parse_bucket_label`). They are not archivable at all.

All 123 trades are settled and there are **zero open positions**, so nothing is waiting on the
service — it can be stopped whenever you want, and archiving the rest of the tree becomes trivial
once it is.

## The one finding that outlived the strategy

`WEATHER_FEE_RATE = 0.0` was wrong. Polymarket **does** charge a weather taker fee:
`feeType: "weather_fees"`, `{"exponent":1,"rate":0.05,"rebateRate":0.25,"takerOnly":true}` ⇒
`fee = shares · 0.05 · p · (1−p)`, **takers only** — makers pay nothing and collect a 25% rebate.
Verified on all 1,639 daily-temperature markets. It fed the live path, settlement, and all four
Edge-2 backtest harnesses in `research/`, so **every number in this archive that predates
2026-07-26 is optimistic**. See `../../CLAUDE.md`.
