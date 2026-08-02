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
| A2 legging sim (+30/171/500ms × seq/batch) | 1 | **DONE, reviewed** | CONDITIONAL PASS — batch+FOK only ($6.65/day buy side, zero broken sets); sequential is fatal; go/no-go now hinges on unmeasured per-order rejection rate |
| A1 decay decomposition | 2 | **DONE, reviewed** | NOT competition — arrivals fell ~40% (cause unknown) + tail luck; grind survival flat; run-rate now $8.12/day (ex-top-5 $5.88), ON the $5 bar |
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

### A1 — decay decomposition (reviewed, PASSED — 91%-vs-28% survival figures reconciled: per-probe vs per-episode views, both in the report, same trend)

- **Competition (a) is REJECTED on the body of the data.** Arrivals fell 261→162 episodes/24h
  (−38%, permutation p=0.003) while every survival measure stayed flat: row-2 survival
  0.687→0.689, grind per-episode 0.7s $-wt survival 73.9%→**77.1%** (improved), median depth
  ratio 1.000 every day, lifetime 8.0s→8.2s. If bots were eating our grind, survival would
  fall — it didn't. Decomposition of the fall: ~55% tail lottery (flashes drew small — 13th
  bootstrap percentile, an ordinary bad draw), ~37% arrival-rate decline, ≤8% racing.
- **The one pro-competition signal: the ≥$1 FLASHES are being raced harder** (per-episode $-wt
  survival 19%→~0%, p≈0.03–0.07, ~20 episodes/cell, non-independent tests). Worth ~$1.2/day.
  Consistent with the audit's Milan finding. This is where a VPS+websocket would fight; the
  grind doesn't need it.
- **The arrival-rate fall is real and its mechanism is UNIDENTIFIED** — Gamma volume on these
  boards only −13% while fee-beating arrivals −59%, and the fee-blind `illusion` metric
  *doubled*. Not "market went quiet", not "spreads compressed". Do not read as "seasonal,
  it'll come back."
- **Use $8.12/day (ex-top-5 $5.88/day) as the run-rate** — the LATE-window (07-30→08-01)
  number, not the full-window $18.34. The grind now sits ON the $5/day bar, not above it.
- **Free tiebreak already running:** Mon 08-03 / Tue 08-04 arrival counts. ≥230 eps/24h →
  weekly cycle (Sat was 128, prior Mon 309); ≤160 → structural decline. Weekend hypothesis has
  exactly 1 supporting + 1 contradicting observation — unsettled.

### A2 — legging simulation (reviewed, PASSED — code audited line-by-line in the main session, headline reproduced to the cent)

Fable review notes: evidence rule is strictly no-look-ahead (first observation AFTER leg landing,
surviving prices only), the two-moment per-leg price fit is algebraically correct and reproduces
logged fee to 7e-15, worst-case pays $0 on residue, and the batch blind spot is priced by an
explicit `--batch-leg-fail` knob rather than hidden. Minor nit only (batch-mode abort accounting
ignores post-failure legs; not a headlined column). The lenient variant self-refutes (exceeds the
un-haircut replay = the tell) and is correctly quarantined.

- **Sequential legging is fatal at ANY latency — this is a design mandate, not a kill.** At this
  box's 171ms: 13.6% of executions break, $127/day of unhedged weather exposure carried to earn
  $15.40/day of arb, expected value already negative (−$4.02/day), worst day −$367. The exact
  unhedged weather bet that lost $477.85, recreated by accident. Never send legs one at a time.
- **Batch (all 11 in one `POST /orders`, per-order FOK) removes every break the data can see:**
  58.5% fill in full, 41.5% miss for free, **$6.65/day buy side** (+$3.13 short = **$9.78/day**),
  worst day +$0.49. Latency tier barely matters for the grind (30ms→500ms: $6.69→$6.53) — the
  batch design is latency-robust; speed matters for flashes and detection, not the grind.
- **The cost of safety is half the edge**: FOK refuses shrunken books, which is where the big
  money was ($6.65 floor vs $14.58/day GTC-partial ceiling — the latter reintroduces quantity-
  imbalance risk the log can't see).
- **Abort/unwind is never worth it**: ~10¢/set of spread+fee against a 0.72¢/set edge. If a set
  breaks, hold the residue (expected damage ≈ spread+fee, not notional); the fix is not breaking.
- **THE new binding constraint: per-order rejection rate inside a batch — unmeasurable from any
  log.** At 0.5% → $5.69/day total (marginal); at 1% → $3.64 (FAIL); at 2% → negative. Eleven
  independent legs amplify: 1% per-order = 6.5% broken sets. **D11's primary output must be this
  rate, not P&L.**
- Break rates are floors (2s snapshots can't see sub-second leg picking; hazard cross-check says
  so), and the 30ms tier contains no real measurement — stated by the tool itself.

## Synthesis — all six tests, read together (2026-08-02)

**No kill. Conditional pass. The go/no-go now hangs on two cheap, specific measurements.**

1. **The honest stacked number straddles the bar.** Full-window: batch+FOK $9.78/day → pass.
   But A1's late-window run-rate ($8.12/day at 100% crediting) × the same batch haircut ≈
   **$4.3/day → fail**. Whether the true run-rate is the full window or the late window is
   exactly the Mon 08-03/Tue 08-04 arrival-count tiebreak (≥230 eps/24h = weekly cycle, ≤160 =
   structural). **Free, already collecting, decides the bar question.**
2. **Execution design is now fully determined, not open:** batch of 11 per-order-FOK orders in
   one `POST /orders`, never sequential, never abort; short side unwinds via `convertPositions`;
   redeem/convert via relayer (gasless). Buy-side capital locked ~10-24h; short-side recycles
   in seconds ($14/day-per-$100 column real for shorts only).
3. **The Tier-C/D gate is a $6/mo VPS** (Amsterdam/Dublin; NOT London/GB; verify geoblock from
   the new box; ToS/residency caveat is the user's call). This box cannot trade at all.
4. **D-tier should start with the SHORT side** (proposed change to the plan): benign partials,
   instant recycle, a competitor already validates the mechanism at min size — measure the
   per-order rejection rate there before risking the buy side's $0-cliff.
5. What would still kill it: rejection rate ≥1% (D11), structural arrival decline (Mon/Tue),
   or the late-window grind staying under ~$6/day with no flash tail to carry it.

## Detail files

- `reports/tier_ab/A2_legging.md` — legging sim full method + numbers
- `reports/tier_ab/A1_decay.md` — decay decomposition
- `reports/tier_ab/A3_B4.md` — resolutions + gas
- `reports/tier_ab/B5_B6.md` — atomic path + API limits
- sim code: `backend/data/negrisk_legging_sim.py` (committed only after adversarial review)
