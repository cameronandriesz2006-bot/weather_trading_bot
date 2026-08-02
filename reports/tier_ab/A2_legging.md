# A2 — The legging simulation: can we actually get all 11 legs?

Run 2026-08-02. Read-only against `logs/negrisk_arb.jsonl`, pinned with
`--until 2026-08-02T04:35:00+00:00` (158.90h, 271,695 sweeps, 27,840 profitable buy-board rows,
22,184 buy shadow probes). The live service was not touched.

Tool: `backend/data/negrisk_legging_sim.py`. Reproduce with

```
venv/bin/python -m backend.data.negrisk_legging_sim --until 2026-08-02T04:35:00+00:00
```

Baseline this test is measured against — `negrisk_arb_pnl.py`, same window, same episode
grouping, row-2 crediting, `--gas 0.02`: **952 executions, $18.53/day total = $15.40/day buy side
+ $3.13/day short side.** Legging risk applies to the buy side; this report is buy-only (§7.1).

---

## 0. Verdict

**Legging risk does not kill the arb, but it removes the freedom to be sloppy, and it moves the
go/no-go onto a number nobody has measured yet.**

Three findings, in order of how much they matter:

1. **Sequential legging is fatal at every latency this machine can reach.** Firing 11 orders one
   after another at the measured 171ms RTT breaks **13.6%** of executions, carries **$127/day of
   unhedged directional weather exposure** to harvest $15.40/day of arb, and the expected-case
   P&L is already **negative (−$4.02/day)** before the tail is counted. Worst single day
   **−$366**. This is not a tuning problem — it is the same unhedged weather bet that lost
   $477.85 here, re-created by accident.

2. **Batching all 11 legs into one `POST /orders` round trip removes the measurable legging
   risk entirely — and costs half the edge.** With per-order fill-or-kill (what the API actually
   does) the outcome becomes strictly binary: all 11 or nothing. 58.5% of attempts fill in full,
   41.5% are free misses, **$6.65/day**, no broken sets, worst day **+$0.49**. Add the short side
   and the strategy is ~**$9.78/day** — still over the user's $5/day bar, on **43%** of the
   $18.53/day the replay claims. The gap is not legging loss; it is FOK rejecting the trades
   whose books shrank, which is exactly where the big money was.

3. **The answer now hinges on one unmeasured number: the per-order rejection rate inside a
   batch.** `POST /orders` is one round trip but eleven independently matched orders — there is
   no cross-order FOK — and no board-level 2s log can see ten-of-eleven coming back. At a **0.5%**
   per-order rejection rate the buy side falls to $2.56/day; at **1%** to $0.51/day; at **2%** it
   is negative and the worst day is −$397. **The strategy survives only if that rate is well
   under 1%.** That is a D10/D11 measurement, not a simulation.

**Recommendation: A2 is a conditional pass.** Buy-the-board is executable *only* as an atomic
11-order batch with per-order FOK, never sequentially, and D11 must be instrumented to report the
per-order rejection rate as its primary output — not the P&L.

---

## 1. What the P&L replay assumes and this test does not

`negrisk_arb_pnl.py` drops any episode shorter than its `--credit-row` as "missed — no position,
no cost". That is a free-miss assumption and it is doing real work: over this window it is the
difference between **738 execution attempts and 585 credited trades**. 153 single-row episodes
(20.7%) are boards that vanished within one sweep. In the replay they cost nothing. In reality we
would already have had 11 orders on the wire.

A second, smaller free ride: the replay's 300s episode gap silently bridges **dead sweeps inside
an episode**. On **98 of 585 (16.8%)** multi-row episodes, the very next sweep after row 1 shows
no board row at all — the board was not buyable then — and the replay still credits the fill at
the next row it *does* find. This simulator treats that sweep as what it is: evidence the board
was gone.

This test fires on all 738.

---

## 2. Method

### 2.1 Timing model

```
t0            the scanner's board row. Its prices are ALREADY ~1s stale at their own timestamp:
              `ts` is stamped after `fetch_books` (median fetch_s 1.018s, p90 1.921s).
+ reaction    our decide-and-sign time, default 0.53s (justified below)
+ k * L       leg k reaches the matching engine (sequential)
              or, in batch mode, all 11 legs at t0 + reaction + L
```

**Reaction latency = 0.53s, derived from the scanner's own instrumentation, not assumed.** The
shadow probe's `delay_s` field measures elapsed time from a board row's `ts` to the completion of
a re-fetch of exactly that board — i.e. this box's full score → decide → issue → round-trip →
parse cycle. Median over 29,541 probes: **0.705s** (p10 0.550, p90 0.838). Subtracting the
measured 0.171s RTT to the CLOB leaves **0.53s of non-network turnaround**. That is charged to
every latency tier, because being colocated does not make Python faster. §5.1 sweeps it; the
conclusions are not sensitive to it except at L=30ms, where nothing is measurable anyway.

**Latency tiers.** 30ms = a colocated VPS (~15ms measured) plus margin; 171ms = this box's
measured RTT (~174ms); 500ms = pessimistic.

**Modes.** `batch` = all 11 legs in one `POST /orders` (up to 15 signed orders, cross-market, one
round trip — verified at API level in B6). `sequential` = one order at a time; the pessimistic
bound, and what a naive first implementation does.

### 2.2 Fill-evidence rule (the conservative core)

A leg counts as **filled only if an observation of that board at or after its landing time still
shows a buyable set**, and it then pays that **surviving** price, never the arrival price.
Observations come from the log:

* **the ~2s sweep grid.** A sweep row with no board row for that slug is a *measurement*, not a
  gap: the scanner writes a sweep row unconditionally, so absence means "this board was not
  net-of-fee buyable at that instant". Median sweep gap 2.01s (p90 2.68s).
* **the shadow probe** — a re-fetch and re-score of every hit board ~0.705s later.

Whichever comes first at or after the leg's landing time is the evidence. Using the *next*
observation rather than the previous one is the conservative direction: books decay.

**Fill mode = FOK by default** (B6: each order is individually fill-or-kill). A leg fills at full
size or not at all; a book that shrank below our size is a **miss**, not a smaller fill.
`--fill-mode partial` models the GTC-marketable-limit alternative, where a shrunken book yields a
smaller fill and the set stays hedged at reduced size. Both are reported (§4).

**Absence is ambiguous, so both readings are run.** The scanner logs only net-*positive* boards,
so "no row" can mean either "the legs are gone" or "the legs are still there, a shade dearer".

* **conservative (default, headlined):** absence ⇒ the leg does not fill.
* **lenient (labelled):** absence ⇒ the leg fills at the last price seen **+ one tick**
  ($0.001, the observed price granularity on these books). This is the "chase" policy.

### 2.3 Splitting a set's cost across its 11 legs

The log stores board-level aggregates only — `k`, total `cost`, total `fee` — never per-leg
prices or ladders. But the fee is not free information. Since
`fee = k · 0.05 · Σ pᵢ(1−pᵢ)`, every board row hands us **two exact moments** of its own price
vector:

```
S1 = Σ pᵢ  = cost / k                S2 = Σ pᵢ² = S1 − (fee/k)/0.05
```

`board_shape()` fits the one-parameter family matching both exactly — a favourite at price `a`
plus ten legs at `b` — by solving `11a² − 2·S1·a + S1² − 10·S2 = 0`.

* **Validated:** over all 27,806 buy rows inside the fired episodes, the reconstructed per-leg
  prices reproduce the logged `cost` and `fee` to **7×10⁻¹⁵ dollars** (i.e. exactly). Zero fits
  failed. Separately, no execution in any configuration ever loses more than it paid.
* **Why it matters:** the obvious alternative — split the cost 1/11 uniformly — overstates the
  fee by ~60% (0.0414 vs 0.0253 per set) and would have made **every single set look like a
  loss**. That was the first version of this tool and it was wrong.
* **What it says about these boards:** median favourite **66.5¢**, median other leg **3.0¢**
  (p10/p90 favourite 52.1¢ / 94.1¢). These are nearly-decided same-day boards, which independently
  corroborates the 07-27 audit's finding that 95% of P&L comes from same-day, post-local-noon
  boards.

The moments cannot say *which board index* the favourite occupies. `--leg-order naive` samples it
from a live cross-section of 114 open boards (measured 2026-08-02; the favourite is index 4–7 on
79% of boards, centred at 5–6). `expensive-first` / `cheap-first` bracket the question (§5.2).

### 2.4 Outcome accounting

Per execution, with `sizing.taker_fee_per_share` used for every fee (never a flat rate):

```
complete sets = min over legs of filled quantity
residue_i     = q_i − complete            (the unhedged part)
P&L           = payout − Σ q_i·p_i − Σ q_i·fee(p_i) − gas
```

Gas is $0.02 per execution, charged whenever anything was acquired — the same convention as the
audited replay. Payout is reported under three conventions:

| convention | residue valued at | reads as |
|---|---|---|
| **worst** | **$0** — the missed leg is the winner | the tail, and the number that matters for ruin |
| **expected** | the **mid** (ask − ½ spread) | risk-neutral: a leg's price *is* its win probability, so a residue is worth roughly what it cost |
| **abort** | the **bid**, minus taker fee on the way out | "never carry unhedged risk" |

The realised winner was **not** used. It is obtainable (slug + `groupItemTitle` + Gamma), but the
leg that fails in this model is determined by *timing position*, not identity, while the price
attached to that position is reconstructed — so a realised-outcome P&L would be half-real and
would read as more authoritative than it is. Verifying that boards actually resolve to exactly
one winner is A3's job. The expected-case convention is insensitive to the price split anyway
(see the martingale identity in §7.3).

**Abort rule.** A leg that fills at a uniformly *smaller* size is not a failure — the set is
simply smaller and still fully hedged. So abort triggers on a leg that returns **nothing**: at
that point no complete set can be formed, we stop sending the rest and dump everything already
held into the bid, paying taker fee again. If every leg returned something, we keep the complete
sets and unwind only the residue. Bids come from a measured spread curve (§3), because the log
contains no bid data at all.

---

## 3. Assumptions, every one of them

| # | assumption | value | basis | direction of error |
|---|---|---|---|---|
| 1 | reaction latency | 0.53s | measured: median shadow `delay_s` 0.705s − 0.171s RTT | swept in §5.1 |
| 2 | per-order round trip | 30 / 171 / 500 ms | 171 = measured RTT (~174ms); 30 = VPS + margin | the grid *is* the sensitivity |
| 3 | evidence = next observation at or after landing | — | conservative by construction | conservative |
| 4 | absence of a board row = board not buyable | — | scanner writes sweep rows unconditionally | conservative; lenient variant inverts it |
| 5 | per-leg prices | two-moment fit | reproduces logged cost + fee exactly (7e-15) | none on totals; shape is one of many fits |
| 6 | favourite's board index | sampled from live 114-board cross-section | measured 2026-08-02 | ±6% on worst-case (§5.4) |
| 7 | bid-ask spread | 0.35¢ (≤1¢), 1¢ (≤5¢), 2¢ (≤60¢), 3.85¢ (>60¢) | medians over 887 live quoted legs | during a hit the ask is unusually low, so the true spread is probably **wider** ⇒ abort and expected-case are **optimistic** |
| 8 | tick size | $0.001 | observed live (0.001, 0.002, 0.008 …) | only affects the lenient variant |
| 9 | gas | $0.02 per execution | same as the audited replay; B4 will replace it | shared with the baseline, so the comparison is fair |
| 10 | shadow-row cost | `1 − fee/set(arrival) − net/set` | shadow rows carry profit and k but not cost | small; §5.3 runs without shadow rows entirely |
| 11 | per-order rejection inside a batch | **0 by default** | *not measurable from this log* | **this is the big one** — swept in §5.5 |
| 12 | order size | the arrival row's `k` | what the scanner said was available | a smaller, more conservative size would fill more often and earn less |

Two board rows in the fired set carry `profit ≤ 0` (17 across the whole log); they produce 4
executions with P&L slightly below −gas. Immaterial, but not silently dropped.

---

## 4. Results

Conservative variant is the headline. Naive leg order, reaction 0.53s, gas $0.02/execution,
FOK fills. **Baseline to beat: $15.40/day buy side.**

### 4.1 FOK — what the API actually does (headline)

| mode | L | full % | **broken %** | none % | $/day worst | $/day expected | $/day abort | worst day |
|---|---|---|---|---|---|---|---|---|
| **batch** | **30ms** | **59.9** | **0.0** | 40.1 | **+6.69** | **+6.69** | +6.69 | **+0.49** |
| **batch** | **171ms** | **58.5** | **0.0** | 41.5 | **+6.65** | **+6.65** | +6.65 | **+0.49** |
| **batch** | **500ms** | **57.0** | **0.0** | 43.0 | **+6.53** | **+6.53** | +6.53 | **+0.49** |
| sequential | 30ms | 56.1 | 5.3 | 38.6 | −29.90 | +3.63 | +3.26 | −104.22 |
| sequential | 171ms | 47.6 | **13.6** | 38.9 | −123.98 | −4.02 | −9.17 | −366.63 |
| sequential | 500ms | 37.5 | **23.4** | 39.0 | −149.34 | −6.97 | −12.78 | −350.01 |

Lenient variant (absence = fill at last price + one tick):

| mode | L | full % | broken % | $/day | note |
|---|---|---|---|---|---|
| batch | 30 / 171 / 500ms | 100 | 0 | +36.22 / +34.70 / +22.08 | **not a credible outcome** |
| sequential | 30 / 171 / 500ms | 100 | 0 | +29.82 / +22.88 / +18.84 | **not a credible outcome** |

The lenient numbers exceed the $15.40/day baseline, which is the tell. Under FOK, "always fillable
at last price + a tick" collapses into approximately the pre-audit zero-latency crediting the
2026-07-27 audit already rejected ($106/day → $18/day). Read them only as a bound: they show that
absence *cannot* mean "still fillable a shade dearer", because if it did the arb would be worth
more than the un-haircut replay said. **Do not quote the lenient column.**

### 4.2 GTC marketable-limit (partial share fills allowed) — the design alternative

| mode | L | full % | reduced % | **broken %** | none % | $/day worst | $/day expected | $/day abort | worst day |
|---|---|---|---|---|---|---|---|---|---|
| batch | 30ms | 59.9 | 8.5 | 0.0 | 31.6 | +14.53 | +14.53 | +14.53 | +2.15 |
| batch | 171ms | 58.5 | 8.5 | 0.0 | 32.9 | **+14.58** | **+14.58** | +14.58 | +2.15 |
| batch | 500ms | 57.0 | 8.9 | 0.0 | 34.0 | +13.47 | +13.47 | +13.47 | +2.15 |
| sequential | 30ms | 56.1 | 6.9 | 6.6 | 30.4 | −18.82 | +10.76 | +8.95 | −74.84 |
| sequential | 171ms | 47.6 | 4.9 | 16.9 | 30.6 | −130.06 | +1.69 | −5.65 | −234.94 |
| sequential | 500ms | 37.5 | 2.7 | 29.1 | 30.6 | −118.47 | −2.77 | −8.47 | −246.12 |

"reduced" = all 11 legs filled but at a smaller size: **still a locked arb**, just smaller.

**The FOK-vs-GTC gap is $6.65 vs $14.58/day and it is not legging loss.** It is FOK rejecting the
8.5% of executions where the book shrank between our read and our order — and those carry more
than half the buy-side P&L, because the biggest opportunities are precisely the ones being eaten
fastest (the audit's Milan flash lost 60% of its depth in 2.05s). GTC keeps that money but
reintroduces per-leg quantity imbalance the log cannot see (§7.2). The honest reading is that
$6.65/day is the floor of a safe design and $14.58/day is the ceiling of a riskier one.

### 4.3 Unhedged exposure carried, and what abort costs

| mode | L | hedged P&L only | unhedged stake carried | abort vs hold |
|---|---|---|---|---|
| batch (any L) | — | $6.5–6.7/day | **$0/day** | identical |
| sequential | 30ms | $6.36/day | $35/day | −$0.37/day |
| sequential | 171ms | $5.26/day | **$127/day** | −$5.15/day |
| sequential | 500ms | $3.74/day | **$150/day** | −$5.81/day |

Read the middle column as: *to earn $15/day of arb, sequential legging at this box's latency
leaves us holding $127/day of naked directional weather exposure.* At 171ms that is 100 broken
executions over 6.6 days, median unhedged stake $4.58, p90 $19.52, max **$97.24**; the worst day
(2026-07-27) is −$366.63 against a whole-window buy-side gross of $102.

**Abort is not a fix.** Unwinding pays the full spread plus a second taker fee, and the measured
spread on these books is enormous relative to the edge: summed across 11 legs it is roughly
**10¢ per set against a median per-set edge of 0.72¢**. Aborting converts a ±$124/day tail into a
certain −$9.17/day charge on a $15.40/day gross. It buys variance reduction at about half the
edge, and it is strictly worse than simply not legging sequentially.

### 4.4 Legging risk is concentrated in the trades that carry the P&L

The fifteen worst broken executions (sequential 171ms, `--detail`) are led by
`shanghai-on-july-27` at k=100 ($97.24 unhedged, 8 legs of 11) and `london-on-july-26` at k=91.8
($90.75, 8 of 11). This is the same adverse selection the 07-27 audit found from the other side:
**what stands still long enough to leg into is the $0.02–0.06 grind; anything sizable is being
consumed while we are still filling.** The largest opportunities are simultaneously the most
profitable and the most likely to leave us holding an unhedged bag, so the risk is not spread
evenly across the 738 attempts — it is loaded onto the twenty or so that matter.

---

## 5. Sensitivities

### 5.1 Reaction latency (sequential, conservative, GTC mode, `broken% / $exp / $worst`)

| reaction | L=30ms | L=171ms | L=500ms |
|---|---|---|---|
| 0.20s | 0.1% / +14.42 / +13.20 | 16.7% / +2.04 / −120.76 | 29.8% / −2.87 / −113.76 |
| 0.35s | 2.3% / +12.60 / −9.62 | 17.6% / +2.03 / −125.23 | 27.6% / −2.13 / −104.65 |
| **0.53s** | **6.6% / +10.59 / −24.98** | **16.9% / +1.61 / −132.53** | **29.1% / −2.55 / −111.29** |
| 0.70s | 4.6% / +12.25 / −2.34 | 14.6% / +2.63 / −117.69 | 30.2% / −2.27 / −103.96 |
| 0.90s | 0.4% / +13.46 / +13.39 | 15.9% / +2.44 / −108.17 | 30.4% / −2.21 / −102.13 |
| 1.20s | 0.1% / +13.46 / +13.38 | 16.9% / +3.68 / −79.35 | 31.2% / −2.38 / −96.38 |

**Read the L=30ms column as a warning, not a result.** Its broken rate swings 0.1% → 6.6% → 0.1%
as the reaction latency moves the 0.33s leg burst across the shadow probe at 0.705s. That boundary
is an artefact of *when we happened to take a photograph*, not a market event. Confirmation:
running with `--ignore-shadow` (uniform 2s evidence) gives **0.0% broken at 30ms in both modes** —
all 11 legs land inside one snapshot interval, so the log has literally no information about them.
The 171ms and 500ms columns are stable across the sweep, because there the burst genuinely spans
several observations.

### 5.2 Leg ordering (sequential, conservative, FOK)

| order | L=171ms broken% | $/day worst | $/day expected | worst day |
|---|---|---|---|---|
| naive (favourite where the market puts it) | 13.6% | −123.98 | −4.02 | −366.63 |
| expensive-first (favourite leg first) | 13.6% | −145.34 | −4.60 | −395.36 |
| cheap-first (favourite leg last) | 13.6% | −38.14 | −1.71 | −111.88 |

**Ordering does not change how often you break — it changes the damage by up to 4×.** Buying the
tails first leaves you holding ~30¢ of dust per set when the board dies instead of the 67¢
favourite, and dust also carries far less fee (0.05·p·(1−p) peaks at p=0.5). The mirror image is
that cheap-first maximises the *probability* of holding a worthless bag; it minimises dollars, not
odds. Note this cuts against the intuitive rule "grab the risky leg first".

**A literal thinnest-book-first ordering is not reconstructible** — the log stores no per-leg
ladders, so leg-level depth does not exist in the data. The live cross-section says the thinnest
legs are the 5¢–20¢ ones (median 57–75 shares within +10% of top, vs 854 for the 20–60¢ band and
often >10,000 for sub-1¢ dust), so thinnest-first is neither of the two brackets above. That is an
open question for C-tier with real books.

### 5.3 Evidence source

`--ignore-shadow` (2s sweep grid only; makes the evidence uniform across the window, since the
shadow probe only exists from 2026-07-27T08:06Z):

| mode | L | broken% | $/day worst | $/day expected |
|---|---|---|---|---|
| batch | any | 0.0% | +6.53 | +6.53 |
| sequential | 171ms | 11.4% (vs 13.6%) | −125.58 (vs −123.98) | −3.36 (vs −4.02) |
| sequential | 500ms | 23.6% (vs 23.4%) | −155.08 (vs −149.34) | −7.33 (vs −6.97) |

Conclusions unchanged. The shadow rows sharpen the 30ms tier into noise (§5.1) and barely move
the others.

### 5.4 Seed (the favourite's board index is the only random element)

Over 5 seeds, sequential 171ms conservative: broken% **16.9% in every case**; worst-case $/day
ranges −$120.20 to −$136.09; expected +$1.47 to +$1.94. Not load-bearing.

### 5.5 Per-order rejection inside a batch — **the decisive unknown**

Batch, 171ms, conservative, FOK. `--batch-leg-fail` applies an independent per-order rejection
on top of the snapshot evidence:

| per-order rejection | full % | broken % | $/day worst | $/day expected | worst day |
|---|---|---|---|---|---|
| **0** (measured floor) | 58.5 | 0.0 | +6.65 | +6.65 | +0.49 |
| 0.5% | 55.0 | 3.5 | −53.34 | +2.56 | −226.53 |
| 1% | 52.0 | 6.5 | −75.03 | +0.51 | −266.13 |
| 2% | 45.5 | 13.0 | −145.69 | −5.67 | −397.40 |
| 5% | 31.8 | 26.7 | −302.94 | −19.16 | −660.38 |

Eleven independent legs is an unforgiving structure: a 1% per-order rejection rate produces a
6.5% broken-set rate (`1 − 0.99¹¹ = 10.5%`, less the executions that miss entirely anyway).
**Adding the short side's $3.13/day, the strategy clears the $5/day bar at 0% and 0.5% rejection,
and fails it at 1%.**

### 5.6 Arithmetic reconciliation against `negrisk_arb_pnl.py`

Forced into the replay's own frame (`--fill-mode partial --ignore-shadow --mode batch
--reaction 0.90 --latency 1`, so every leg's evidence is exactly episode row 2), the simulator
books **$89.198** over the 585 multi-row episodes. Hand-computing `min(k₁,k₂)·net_per_set(row 2) −
gas` over the **487** episodes whose next *sweep* is row 2 gives **$89.172** — agreement to
$0.03. The remaining $5.37 of the replay's $94.54 is the **98 dead-sweep holes** of §1: episodes
where the sweep immediately after arrival showed no buyable board and the replay credited the next
row it found anyway. That $5.37 is not an error in the replay's arithmetic; it is the free-miss
assumption being priced.

### 5.7 Independent cross-check on the fill rates

A memoryless-hazard check, calibrated on the shadow probe (408/593 = 68.8% of episodes still
alive at 0.705s ⇒ λ = 0.530/s), gives P(a board state-change lands inside the leg burst) of
**10.9% / 41.1% / 53.8%** at L = 30 / 171 / 500ms — versus this simulator's 5.3% / 13.6% / 23.4%.
The exponential overstates the long-horizon hazard (survival on these boards is heavy-tailed: 337
of 739 episodes stand for ten sweeps or more), so it is an upper bound, not a rival estimate. But
it points the same way as §7.2: **the simulator's break rates are floors.**

---

## 6. What this does to the go/no-go arithmetic

| design | buy side | + short side | vs $18.53/day replay | vs $5/day bar |
|---|---|---|---|---|
| replay, legging assumed away | $15.40 | $18.53 | 100% | pass |
| **batch + FOK, 0% rejection** | **$6.65** | **$9.78** | 53% | **pass** |
| batch + FOK, 0.5% rejection | $2.56 | $5.69 | 31% | marginal pass |
| batch + FOK, 1% rejection | $0.51 | $3.64 | 20% | **fail** |
| batch + GTC (partial fills) | $14.58 | $17.71 | 96% | pass — but §7.2 |
| sequential, 171ms (expected case) | −$4.02 | −$0.89 | — | **fail** |
| sequential, 171ms (worst case) | −$123.98 | −$120.85 | — | **fail** |

Two further points the plan should carry forward:

* **The decay in A1 compounds with this.** The test plan's most recent full day was $3.77/day at
  100% crediting. Half of that is $1.9/day. The margin for a non-zero rejection rate is thin.
* **Capital.** The batch design ties up ~$830/day of turnover in this window's opportunities; the
  hold-to-resolution peak from the replay is $5,342. Legging does not change that, but it means a
  micro-live test at $25–30 will sample only the smallest opportunities and will therefore
  under-sample exactly the shrinking-book cases where FOK rejects.

---

## 7. Limitations — read these before quoting any number above

### 7.1 Scope: buy side only
All of the audit's 5,269 buy rows are 11-leg boards, and only the buy identity has a cliff — miss
a leg and the set can pay $0. A partial SHORT is benign: the negRisk adapter's `convertPositions`
turns k of 11 NO legs into (k−1)·$1 plus YES on the complement in one ~$0.02 transaction, so there
is no $0 state to reach; a short that legs out loses the marginal profit of the missing legs, not
the stake. The short side is $3.13/day of the $18.53/day and is carried through this report at
face value, unmodelled.

### 7.2 THE central limitation: 2s snapshots vs sub-second leg timing
The log's evidence resolution is 0.7–2s. Leg timing at L=30ms is 0.03s. **This tool cannot see one
leg of eleven being picked off inside a snapshot interval.** Concretely:

* at **L=30ms** all 11 legs land within 0.33s and share one observation. The model degenerates to
  "did the whole board survive 0.7s" — board-level, not leg-level. §5.1 shows its apparent 6.6%
  break rate is a probe-alignment artefact and `--ignore-shadow` gives 0.0%. **There is no
  measurement of 30ms-scale legging risk in this dataset, only an absence of one.**
* in **batch mode this is true at every latency**, which is why batch shows 0.0% broken by
  construction. That is not a finding, it is a blind spot, and §5.5 is the honest response to it.
* only **L=500ms sequential** (5.5s ≈ 3 sweeps) spans enough observations for genuine leg-by-leg
  gradation.

Every fill rate above is therefore an **upper bound** and every break rate a **lower bound**.
C8 (real end-to-end latency) and D10/D11 (real fills) can still overturn this.

### 7.3 The martingale assumption behind the expected case
The expected-case column values an unhedged residue at the mid, i.e. it assumes a leg's price is
its win probability. Two reasons it is optimistic, both unquantified:

* **adverse selection.** Legs vanish precisely when informed flow is hitting them, so the leg we
  fail to buy has a true probability *above* its stale quote. The expected case is therefore a
  ceiling on the expected case.
* **spread calibration.** The spread curve was measured on a live cross-section at a random
  moment, not during a hit. During an arb window the ask is unusually *low*, so the true spread
  is probably wider, making both the mid valuation and the abort recovery optimistic.

Note the identity that makes this column robust to the price-split assumption: if p = price, then
E[residue payout] = Σ residue·p ≈ what we paid for it, so the *expected* damage of a broken set is
the spread + the fee + the forgone arb, not the notional. It is the **variance** that is fatal,
which is why the worst-case column is headlined alongside it.

### 7.4 Not tested here
* realised outcomes (did the missed leg actually win) — deliberately, see §2.4; A3's job.
* real gas (B4 will replace the $0.02).
* rate limits on an 11-order burst (B6).
* whether an atomic mint/convert path removes the problem entirely (B5). If it does, everything in
  this report about legging is moot and the answer reverts to the $15.40/day baseline.

---

## 8. One-paragraph read

Legging risk is real, is concentrated entirely in *how* the orders are sent, and costs somewhere
between 4% and 100% of the buy-side edge depending on that choice. Sending 11 orders one at a time
is indefensible at any latency this project can buy: at the measured 171ms it breaks 13.6% of
executions, carries $127/day of unhedged weather exposure against $15.40/day of arb, and is already
negative in expectation before the tail. Sending them as one batch of per-order fill-or-kill orders
removes every break this dataset can see and leaves $6.65/day buy-side ($9.78/day with the short
side) — over the bar, on 53% of what the replay claims, with the gap being FOK correctly refusing
the trades whose books had already moved. But batch mode is exactly where the log goes blind: eleven
orders are matched independently, and at a 1% per-order rejection rate the buy side falls to
$0.51/day and the strategy misses the bar. That single number is now the binding constraint on the
whole strategy, no simulation can produce it, and D11 should be designed to measure it rather than
to make money.
