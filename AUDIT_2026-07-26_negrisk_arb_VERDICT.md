# Audit verdict — negRisk arb test (Plan B, B0/B1)

Answers `AUDIT_PROMPT_negrisk_arb.md`. Audited at HEAD `0024143`; fixes landed in `ce6aa76`.
Three parallel agents plus direct verification; everything below was measured against the live
API or the real logs, not reasoned from source.

## 1. Verdict on correctness — the identity is SOUND

Nothing broke it. Verified empirically:

- **Token mapping clean.** 25 boards / 275 markets cross-checked against
  `clob.polymarket.com/markets/{cid}`: 275/275 match, all `["Yes","No"]`, Gamma `clobTokenIds`
  order == CLOB `tokens[]` order.
- **Ask side clean.** CLOB returns asks descending; after the sort, `asks[0]` is the true
  cheapest. The NO book is an exact synthetic mirror — `NO_ask == 1 − YES_bid` on 1090/1090 legs
  — so **the "arb" is algebraically just `sum(YES_bid over S) > 1`**.
- **Partition clean.** 149/149 boards a true partition (11 buckets, open tails both ends,
  `lo == prev_hi + 1`), 0 duplicate tokens, °F 2° bands and °C single-degree both exact.
- **Exactly one winner.** 900/900 closed historical boards resolved `(11 markets, 1 YES, 0
  ambiguous)`; 0 voids, 0 50/50s. Live universe: 0 mixed-state boards.
- **Subset inclusion rule optimal.** `net(S,k) = Σ_{i∈S}(k − cost_i − fee_i) − k` is additive and
  separable, so greedy inclusion is exactly optimal. `best_set_size` concavity claim also holds.

### Defects found (all fixed in `ce6aa76`)

| # | defect | impact |
|---|---|---|
| 1 | **No fee charged anywhere** | every logged row a false positive — see §3 |
| 2 | BUY direction never checked exhaustiveness (`len(legs) >= 3` is not a partition test) | a 3-leg fragment with a gap reports "guaranteed $10, executable" and **loses 100% of stake** |
| 3 | SHORT never checked mutual exclusivity | overlapping buckets report +$110 on a trade that loses $90 |
| 4 | Subsampler's max index is `int(159·len/160) < len−1`, so **max_k always discarded** | latent; constructed case lost $800 of $800.32. Not firing (max 89 candidates vs 160 cap) |
| 5 | `cost_i < k` inclusion test mathematically unreachable (needs avg price ≥ $1.00; 0 of 84,296 live levels qualify) | admitted every leg incl. 99c filler → 0.07–0.35% ROC |
| 6 | `TICK = 0.01` wrong for 51% of markets (they use 0.001) | dead code; removed |

## 2. Verdict on the measurement — it measured a STOCK as a FLOW

Blind to, in order of size: capital absorption, settlement lock, execution risk, fees, diurnal
coverage.

- **Left censoring.** 36% of profit was standing mispricing already live at the first sweep.
  Fixed numerator over growing denominator ⇒ $/day decays as 1/t. Observed in one run:
  **$6,720/day at 2 min → $3,449 at 8 → $2,085 at 24 → $1,909 at 27.**
- **Peak-crediting.** Windows credited at peak, which no trader can reach. **1.69× overstatement.**
- **Re-counting.** Keyed by slug; 30 of 54 boards produced >1 window, each re-credited.
- **Capital never measured portfolio-wide.** The whole arb book absorbs **$5,955 at one instant**;
  the headline implied ~$2.03M cycling — a 340× mismatch.
- **Cadence.** Real loop was 3.5s median, not the 2s the report assumed.

Correction cascade on the fee-blind log: **$1,656/day → $216/day** (still gross of fees).

### Suspicions that were WRONG — do not re-litigate

- **Snapshot coherence (the prompt's prime suspect) is INNOCENT.** A board's 22 tokens are
  contiguous and `chunk_size=500`, so **143/149 boards (96%) arrive in ONE HTTP response**. 102
  coherent re-fetches reproduced the stitched arb at **100.1%**; synthetic 1-second stitch bias
  **−0.003%**. 0 of 102 observed hits were on a straddling board.
- **Execution risk is small.** These books are slow — 0.007–0.012 changes/sec/leg;
  P(all 11 legs unchanged after 171ms) = **0.978**. Haircut 2–12%.
- **Gas is rounding error.** ~$0.019 convert + $0.005 redeem; fills are operator-paid, redemption
  gasless for proxy wallets.
- **Capital lockup is not 1–2 days.** The negRisk adapter's `convertPositions` redeems m NO
  tokens for (m−1) USDC immediately, pre-resolution.
- **`fetch_books` drops nothing.** 3,278/3,278 tokens returned across 49 chunks, 0 non-200. A
  dropped chunk would *deflate*, not inflate.

## 3. The kill shot — Polymarket charges a taker fee

Verified on **all 1,639** daily-temperature markets: `feesEnabled: true`,
`feeType: "weather_fees"`, `feeSchedule {"exponent":1,"rate":0.05,"rebateRate":0.25,"takerOnly":true}`
⇒ `fee = shares · 0.05 · p · (1−p)`, **takers only**; makers pay nothing and earn a 25% rebate.

Per set, `fee = 0.05·(Σq − Σq²) ≈ 0.05·(1 − Σq²) ≈ $0.034`, against a **median observed gross
edge of $0.0068/set** — a 5.3× shortfall. Re-scored live at the size the scanner actually picked:
gross **+$3.98 → net −$9.87 to −$15.60** on $2,938 capital, 0 of 10 boards surviving, under
*both* candidate fee formulas.

**The structural reason:** the fee is a **per-leg tax** while the edge is not. Shorting 9 legs to
collect 0.7c pays nine legs of fee (~3.4c) and loses. This is also why the apparent arb persisted
for up to 31 minutes on a platform full of arb bots — **the persistence was the evidence of an
unmodelled cost.**

### Corrected estimate

A genuinely **fee-aware** optimiser (maximise net, not gross-then-subtract) *does* find survivors
— because it sizes down ~40×. They are rare and microscopic: 1 of 25 sweeps in one run, 9 of 100
in another; best observed **$0.68 net on $7.13 capital** (2 legs, k=8, 9.6% RoC). The only
fee-viable shape is a **small genuinely crossed subset**, the opposite of what maximising gross
selects for.

> **Corrected: single-digit to low-double-digit $/day, capped at ~$8 per position.** Not a
> −$4,300/day trap; it dies of **irrelevance**, not catastrophe.

A hypothesis that same-day "collapsed" boards escape the fee (Σq² → 1) was tested across all 137
quoted boards and **failed**: same-day boards are not collapsed at scan hour (median Σq² 0.403),
of the 6 genuinely collapsed boards **0 have any gross edge**, and gross beat fee in **0/137**.

## 4. Top fixes, by how much they changed the answer

1. **Model the fee** — `WEATHER_FEE_RATE = 0.0` → `WEATHER_TAKER_FEE_RATE`, priced through
   `sizing.taker_fee_per_share` / `taker_fee_on_cash`. Reaches far beyond the arb (see §5).
2. **Optimise NET, not gross** — `best_subset_net` / `best_set_size_net`.
3. **`board_sanity()`** — exclusivity for SHORT, exhaustiveness for BUY. 156/156 live boards pass.
4. **Report methodology** — drop left/right-censored windows, credit at arrival, one execution per
   board, portfolio-wide capital, measure the real sweep interval.
5. **Prefix-sum depth walks** — optimiser 4.7× faster; sweep cycle 3.14s → **2.00s**.

## 5. The finding that outlives the arb

`WEATHER_FEE_RATE = 0.0` fed the **live signal path**, settlement, the maker path, and **all four
Edge-2 backtest harnesses**. Re-pricing the real trade log:

| cohort | n | booked P&L | unbooked fee | corrected |
|---|---|---|---|---|
| all-time weather trades | 120 | −$213.75 | **$235.41** | **−$449.16** |
| post-epoch (Edge-2 test) | 49 | −$286.61 | $29.74 | −$316.35 |

Fee as a fraction of notional is `0.05·(1−p)` — small on Edge-2's post-high **favourites**, large
on cheap **tails**. So the Edge-2 verdict gets 10% worse (already a fail, unchanged), but the
broad pre-Edge-2 tail-buying strategy's loss **roughly doubles** — `AUDIT_2026-06-29.md` is
understated by ~110%. A/B on `edge2_publish_honest --cities denver,atlanta --hours 16`: 99 → 92
tradeable, P&L $19,025 → $18,983 — the fee trims marginal trades but **does not overturn** the
Ha=16 seam.

## 6. Go/no-go

**Against the >$50/day bar: NO — do not build execution.** It fails on return-on-capital and
capacity, not on latency, so a faster box buys nothing.

A defensible bar, given a 2–3 week build (~$5k), ops ~$13/day and $10k at a 15%/yr required
return: **net ≥ $100/day with a bootstrap 95% CI lower bound > $50/day, ≥ 0.5%/day on peak
deployed capital, over ≥ 5 continuous days.** Note the capacity finding applies to any arb
variant: at 0.1–0.2%/day net RoC, $100/day needs $100–200k. **A $10k bankroll cannot amortise
this build at any plausible arb RoC.**

The scanner keeps running on the corrected scoring — it costs nothing and closes the question
properly instead of on an 18-minute sample. **The only fee-free path on this platform is the
maker side** (0% + 25% rebate): an argument for the shadow-maker plan, and decisively against
anything that crosses the spread.
