# Tier A + B results (started 2026-08-02)

Live tracking doc for the go-live tests in `GOLIVE_TESTPLAN_2026-08-02.md`, run as 4 parallel
Opus agents with Fable review in the main session. If this session dies, a cold session resumes
from here: each agent writes its full detail to `reports/tier_ab/`, this file carries the
reviewed, simple-terms verdicts.

**Standing context:** baseline is $18.34/day survival-credited ($11.97 ex-top-5), decaying
(08-01 = $3.77). Bar is ≥$5/day. Latency-shaped failures = VPS/adapt decision, NOT a kill
(user decision 2026-08-02). Scanner (`negrisk-arb.service`) keeps running throughout.

## Status

| test | agent | status | verdict (simple terms) |
|---|---|---|---|
| A2 legging sim (+30/171/500ms × seq/batch) | 1 | running | — |
| A1 decay decomposition | 2 | running | — |
| A3 resolution verification | 3 | **DONE, reviewed** | PASS — 290/290 resolved boards paid exactly one $1 winner; zero voids; median capital return 10.4h after event end |
| B4 real gas costs | 3 | **DONE, reviewed** | $0.024/trade assumption confirmed (0.4% off); taker fills cost user ZERO gas; relayer makes exits gasless |
| B5 atomic path / early merge | 4 | **DONE, reviewed** | short side has an instant atomic cash-out; buy side provably locked to resolution; no atomic entry exists |
| B6 API limits + VPS region | 4 | **DONE, reviewed** | **BLOCKER: this box is geo-blocked (SG)** — VPS in Amsterdam/Dublin required (NOT London); limits otherwise fine |

## Reviewed verdicts

### B5 — atomic path / early merge (reviewed, PASSED — spot-checked geoblock + merge proof)

- **Short-a-subset just got much better.** `NegRiskAdapter.convertPositions` turns NO on m
  buckets into (m−1)·$1 cash + YES on the rest, in ONE tx, ~$0.021 gas per BOARD, zero protocol
  fee. Full short board → $10 cash instantly, no resolution wait → the **$14/day-per-$100
  recycle column is REAL for the short side**. A partial short fill is harmless (converts to
  cash + a defined YES basket, never a $0 set). A competitor is already doing exactly this on
  our weather boards at min size (tx 0x6d0a937e…6905, Chengdu Aug-3 board).
- **Buy-the-board capital is PROVABLY locked to resolution** (split/merge are per-condition;
  a full YES set has no NOs to merge against — algebraic proof in the report). $2.81/day per
  $100 is the correct buy-side column, full stop. Partial buy-set stays the $0-risk danger.
- **No atomic entry on either side.** Best possible: all 11 signed orders in one `POST /orders`
  batch (max 15, cross-market OK) — one round trip of staleness, but accept/reject is per-order.
  FOK exists per-order only. So legging risk on entry is real and A2 stays decisive.
- Mint-a-full-set costs exactly $1.00 → structural price ceiling on the buy-side edge.
- Real gas: convert-11 ≈ $0.021/board, redeem ≈ $0.011/board — the plan's $0.02/POSITION
  assumption overstates cost ~10× (it's per board, not per leg).

### B6 — API limits + VPS (reviewed, PASSED — geoblock independently verified from this box)

- **BLOCKER: Singapore is close-only on frontend AND API.** `polymarket.com/api/geoblock` from
  this box: `{"blocked":true,"country":"SG"}` (verified twice, agent + main session). Nothing in
  Tier C/D can run from this machine. Fix = ~$6/mo VPS.
- **VPS: Vultr Amsterdam or AWS Dublin** (~10–18ms to the matching engine vs 174ms here — ~10×).
  **NOT London: GB is API-blocked** despite being closest. Origin is AWS eu-west-2 (London),
  stated in Polymarket's own docs and corroborated by RTT measurement. Must verify `geoblock`
  returns false from the new box before committing.
- **ToS caveat (user decision, stated plainly):** geoblocking is by IP, but if the account
  holder is resident in a restricted jurisdiction, VPS-routing violates Polymarket ToS with
  account/funds risk. Not resolvable by this research.
- Limits are NOT the constraint: min size 5 shares, batch `POST /orders` max 15 (11 fits),
  order types GTC/GTD/FOK/FAK + postOnly, per-signer 40 orders/s + 60 burst ≈ 3.6 full boards/s.
  No KYC to trade via API (KYC only for co-location).
- **WebSocket market feed exists and works** (~117 msg/s for ONE board measured) — the 2s
  polling loop is leaving most of the signal on the floor. Detection path VPS+WS: ~2s stale +
  174ms react → ~15ms stale + ~15ms react.
- Consequence for the plan: C8 (end-to-end latency) must NOT be run from this box — it would
  measure the wrong machine.

### A3 — resolution verification (reviewed, PASSED — HK anomaly independently re-verified on Gamma)

- **290 boards checked, 290 clean**: every resolved board the scanner hit 07-27→08-02 paid
  exactly one bucket $1 and the other ten $0. Zero voids/refunds/split payouts. Exhaustiveness
  holds against reality, not just against `board_sanity()`'s logic.
- Token-set drift 0/313 (the 11 tokens never changed after scan time); `negRiskAugmented`
  false on all 368 events — genuinely absent, but worth adding as a `board_sanity()` gate.
- **Capital timing**: money returns median **10.4h** after event end (p95 21.6h). One real
  anomaly: the HK July-31 board (both twins) still unresolved 53h+ after event end with no UMA
  proposal (re-verified 08-02 from the main session) — payout not at risk, but the *lockup tail*
  is longer than the median suggests. Redemption trigger must key off the winning market
  (losing buckets close before the winner, up to ~107min spread).

### B4 — real gas (reviewed, PASSED — converges with agent 4's independent on-chain sample)

- **The replay's $0.024/trade gas charge is fair**: blended over the actual trade mix,
  self-paid batched gas is $0.0239/trade (0.4% off the assumption). No P&L revision needed.
- **Taker fills cost the user zero gas** — operator pays (verified: trader proxy wallets hold
  0 POL and trades settle anyway). Redeem/merge/convert have a **gasless relayer path**
  (`relayer-v2.polymarket.com/submit`, 25/min limit); self-paid they're ~$0.01–0.05/board.
  Batching is mandatory if self-custodying (unbatched 7-leg redemption ≈ 3×).
- Independently re-derived the buy-side lockup conclusion (merge needs YES+NO of the same
  condition → buy-the-board inventory can never merge) — two agents, same answer, different
  methods. Buy side = hold-to-resolution economics, confirmed twice.
- Flag for A2 (unverified by agent 3, schema question): it counted only ~3.7% of buy rows with
  all 11 legs quoted simultaneously — A2 owns the log schema and should confirm or refute.

## Detail files

- `reports/tier_ab/A2_legging.md` — legging sim full method + numbers
- `reports/tier_ab/A1_decay.md` — decay decomposition
- `reports/tier_ab/A3_B4.md` — resolutions + gas
- `reports/tier_ab/B5_B6.md` — atomic path + API limits
- sim code: `backend/data/negrisk_legging_sim.py` (committed only after adversarial review)
