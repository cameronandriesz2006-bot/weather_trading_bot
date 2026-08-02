# A1 — Is there still an edge to chase? Decomposing the decay

Run 2026-08-02. Read-only against a snapshot of `logs/negrisk_arb.jsonl` taken at
`2026-08-02T04:33Z` (336,732 rows, 158.87h, 271,623 sweeps, 35,568 profitable-board rows,
29,522 shadow probes). Live service untouched.

Baseline reproduced on the snapshot: **952 executions, $122.68 net, $18.53/day**
(`--gas 0.02`, credit-row 2) — matches the test plan's $18.34/day at 3h more data.

---

## 0. Verdict

**(b) with a large slice of (c). Not (a).**

Rough weights on the $15/day fall from the first 3 days to the last 3:

| driver | share of the fall | evidence strength |
|---|---|---|
| **tail lottery** (arrival > $1 flashes drawing small) — i.e. (c) NOISE | **~55%** | strong: 44 flash episodes total; late credited-tail net sits at the 13th bootstrap percentile, i.e. an ordinary bad draw |
| **arrival rate down ~40%** (fewer fee-beating mispricings created) — i.e. (b) | **~37%** | strong that it is real (permutation p=0.003); mechanism **not identified** |
| **fill-race competition** — the user's (a) | **≤8%, and only on flashes** | five survival measures flat on the body; two marginal, non-independent tests on the ≥$1 stratum (n≈20/cell, p=0.025 and p=0.066) |

**The user's (a) is rejected on the body of the data.** Every measure of "does the price still
stand long enough for us" is unchanged over the six days: probability of surviving one 2s sweep
0.712 → 0.648 (Mann-Kendall p=0.72), 0.7s dollar-weighted shadow survival **91.0% → 91.6%**
(n=17,705 vs 11,698 probes), median episode lifetime 8.0s → 8.2s, median depth ratio k(row2)/k(row1)
= **1.000 every single day**. Nothing in the grind is being eaten out from under us faster than it was.

**One exception, flagged loudly because it is the only pro-(a) evidence in the report**: the ≥$1
flash stratum *does* look raced harder. Row-2 survival 17/24 → 8/20 (Fisher p=0.066) and 0.7s
shadow survival 12/19 → 5/20 (Fisher p=0.025), with median surviving depth k_now/k_orig 0.22 → 0.00.
Two measurements agreeing — but on largely the same ~20 episodes per cell, so they are **not
independent**, and the ≤$0.10 stratum moved the opposite way (p=0.058 *improving*). Even taken at
face value this is worth ~$1.2/day of a $15/day fall, because the flash slice was only $10.98/day
of EARLY revenue to begin with. It is a hypothesis to re-read in a week, not a verdict.

**But do not read (b) as "seasonal, it'll come back."** The arrival-rate fall is real and broad,
and the obvious benign mechanisms are ruled out: board universe constant (57 boards per market
date, 49 cities, ~145 swept, all quoted), no code change since 07-27 08:52, and **traded volume on
these boards is flat** ($3.88M → $3.37M, −13%, from Gamma). A −59% fall in fee-beating arrivals on
−13% volume is not "the market went quiet". Three candidates remain and the data cannot rank them:
a **weekly/holiday cycle** (F3 below settles it in two days), a **weather-regime** shift in how
decided the boards are (§7 gives weak supporting evidence), or **competition at the *quoting*
level** — makers pricing tighter so fewer boards ever cross the net-of-fee line. Only the last is
permanent, and it is still the kind **a faster VPS does not fix**, because it removes the thing to
race for rather than the time to race it.

**Do not buy infrastructure on the strength of this decay.** The one place we may be losing races
is the ≥$1 flash slice, and buying that back is worth ~$1.2/day against a bar of $5/day.

---

## 1. The headline series (UTC days, 24.00h coverage each)

`eps` = distinct standing mispricings (slug+side, 300s gap, 120s warmup drop) — the same grouping
`negrisk_arb_pnl.py` uses. `arr$` = value at first sight. `net$` = credited at row 2, gas $0.02.

| day | dow | eps | arr $ | row2 gross $ | net $ | ex-top1 | ex-top5 | P(survive) | med net/ep |
|---|---|---|---|---|---|---|---|---|---|
| 07-27 | Mon | 309 | 67.39 | 39.25 | **34.85** | 26.75 | 20.05 | 0.712 | 0.025 |
| 07-28 | Tue | 245 | 109.36 | 17.29 | **14.07** | 12.02 | 9.31 | 0.657 | 0.026 |
| 07-29 | Wed | 229 | 75.68 | 23.55 | **20.41** | 10.89 | 9.01 | 0.686 | 0.024 |
| 07-30 | Thu | 176 | 32.63 | 11.10 | **8.64** | 7.32 | 5.40 | 0.699 | 0.024 |
| 07-31 | Fri | 181 | 33.22 | 14.50 | **11.94** | 9.50 | 7.76 | 0.707 | 0.029 |
| 08-01 | Sat | 128 | 24.79 | 5.43 | **3.77** | 3.16 | 2.56 | 0.648 | 0.012 |

(07-26 is excluded from all trend work: partial day, 10.32h, and the net-of-fee scorer landed at
13:43 — 2 minutes after the log opens. 08-02 excluded: 4.55h.)

Pooled 3-vs-3:

| window | trades | net | $/day | ex-top1 | ex-top5 |
|---|---|---|---|---|---|
| EARLY 07-27..29 | 538 | $69.33 | **$23.11** | $19.94 | $14.26 |
| LATE 07-30..08-01 | 334 | $24.35 | **$8.12** | $7.30 | $5.88 |
| 08-01 alone | 83 | $3.77 | **$3.77** | $3.16 | $2.06 |

Note that the LATE window still clears the $5/day bar on the ex-top-5 grind, and 08-01 alone does not.

---

## 2. Coverage and scanner health — the decay is not an artifact

| day | sweeps | coverage h | boards/sweep | quoted | fetch_s | hits/sweep | sweeps with a hit |
|---|---|---|---|---|---|---|---|
| 07-27 | 41,102 | 24.00 | 138 | 138 | 1.02 | 0.144 | 13.2% |
| 07-28 | 39,815 | 24.00 | 144 | 144 | 1.03 | 0.237 | 16.4% |
| 07-29 | 40,130 | 24.00 | 146 | 146 | 1.06 | 0.139 | 13.4% |
| 07-30 | 41,067 | 24.00 | 143 | 143 | 1.03 | 0.101 | 9.7% |
| 07-31 | 41,924 | 24.00 | 146 | 145 | 0.99 | 0.105 | 10.0% |
| 08-01 | 41,860 | 24.00 | 147 | 147 | 1.00 | 0.075 | 7.5% |

Controls that clear:

- Coverage is exactly 24.00h on every day counted (gaps >120s: none). `NRestarts=0`, service up since 07-29 06:21Z; before that the scanner ran continuously by hand — no downtime in the log.
- `quoted ≈ boards` throughout: no book-fetch failure suppressing hits.
- `fetch_s` median 0.99–1.06s: our own latency is constant.
- Last change to scoring code was **2026-07-27 08:52** (`8dcea0c`, shadow probe — does not touch scoring). The fee-aware scorer (`ce6aa76`) landed 07-26 13:43. No code-driven step.
- Token cache holds **exactly 57 boards per market date** for 07-26 → 08-03. The universe is not thinning.

---

## 3. Arrival vs survival vs size — the decomposition

Multiplicative: `net/day = (episodes/day) × P(survive to row 2) × (mean net | credited)`.
Log-changes are additive; base = 07-27.

| day | eps/24h | P(cred) | mean net\|cred | = net/24h | Δ total | from **count** | from **survival** | from **size** |
|---|---|---|---|---|---|---|---|---|
| 07-27 | 309.0 | 0.712 | 0.1584 | 34.85 | base | — | — | — |
| 07-28 | 245.0 | 0.657 | 0.0874 | 14.07 | −91% | −23% | −8% | −59% |
| 07-29 | 229.0 | 0.686 | 0.1300 | 20.41 | −54% | −30% | −4% | −20% |
| 07-30 | 176.0 | 0.699 | 0.0703 | 8.64 | −139% | −56% | −2% | −81% |
| 07-31 | 181.0 | 0.707 | 0.0933 | 11.94 | −107% | −53% | −1% | −53% |
| 08-01 | 128.0 | 0.648 | 0.0454 | 3.77 | −222% | −88% | −9% | −125% |

**Survival contributes ~nothing (−1% to −9%).** The decay lives entirely in count and in mean size —
and the "size" term is a *mean over a heavy tail*, which section 5 shows is the tail lottery, not
per-unit erosion. Caveat: `P(cred)` here is **episode-weighted**, so it is dominated by the grind.
The ≥$1 stratum's row-2 survival did fall (0.708 → 0.400) — that is inside the "size" term, and §5
sizes it at ~$1.2/day.

### Trend significance (n=6 days, exact Mann-Kendall)

| series | 07-27 | 07-28 | 07-29 | 07-30 | 07-31 | 08-01 | log slope/day | t | MK p |
|---|---|---|---|---|---|---|---|---|---|
| **eps arriving /24h** | 309 | 245 | 229 | 176 | 181 | 128 | **−15.9%** | −7.95 | **0.017** |
| arrival $ /24h | 67.4 | 109.4 | 75.7 | 32.6 | 33.2 | 24.8 | −26.9% | −3.36 | 0.136 |
| NET $ /24h | 34.8 | 14.1 | 20.4 | 8.6 | 11.9 | 3.8 | −35.6% | −3.67 | 0.056 |
| NET ex-tail (arr ≤ $1) | 17.1 | 9.8 | 9.5 | 7.2 | 8.9 | 3.7 | −23.7% | −3.72 | **0.017** |
| grind $ (arr ≤ $0.10) | 5.6 | 2.2 | 3.7 | 2.7 | 3.9 | 1.3 | −17.3% | −1.61 | 0.469 |
| grind eps /24h | 222 | 173 | 184 | 128 | 128 | 93 | −16.0% | −6.45 | 0.056 |
| **P(survive to row 2)** | 0.712 | 0.657 | 0.686 | 0.699 | 0.707 | 0.648 | −0.7% | −0.66 | 0.719 |
| **median arrival $** | 0.044 | 0.053 | 0.043 | 0.047 | 0.046 | 0.043 | −1.4% | −0.75 | 0.469 |
| median net \| credited | 0.025 | 0.026 | 0.024 | 0.024 | 0.029 | 0.012 | −10.0% | −1.41 | 0.469 |
| **median lifetime s** | 8.5 | 8.0 | 8.0 | 8.1 | 10.8 | 8.0 | +1.7% | +0.53 | 1.000 |
| 1-row share % | 28.8 | 34.3 | 31.4 | 30.1 | 29.3 | 35.2 | +1.4% | +0.65 | 0.719 |
| distinct boards hit /24h | 75 | 74 | 72 | 80 | 72 | 62 | −2.7% | −1.45 | 0.136 |
| **eps per hit board** | 4.12 | 3.31 | 3.18 | 2.20 | 2.51 | 2.07 | **−13.3%** | −5.12 | **0.017** |
| **med buy cost/set** | 0.9660 | 0.9660 | 0.9686 | 0.9708 | 0.9660 | 0.9691 | +0.1% | +1.03 | 0.272 |
| med buy ROC % | 0.737 | 0.807 | 0.587 | 0.764 | 0.727 | 0.619 | −2.6% | −0.86 | 0.469 |

15 series tested — a lone p<0.05 proves nothing. What matters is the **pattern**: everything that
counts *arrivals* trends hard down, and everything that measures the *quality of an arrival* is flat.

Arrival-count decline is not a Poisson accident: χ² vs a uniform daily rate = 95.1 on 5 df
(p ≪ 0.001); permutation test on the ordering of the six counts gives one-sided p = **0.0029**
(floor for n=6 is 1/720 = 0.0014).

Note `eps per hit board` 4.12 → 2.07 while `distinct boards hit` barely moves (75 → 62). **The same
boards keep producing edges; each one just produces half as many.**

---

## 4. Survival, measured five ways — flat

Pooled EARLY (07-27..29) vs LATE (07-30..08-01), 72h each:

| metric | EARLY | LATE | change |
|---|---|---|---|
| eps/24h | 261.0 | 161.7 | **−38%** |
| arrival $/24h | 84.14 | 30.21 | −64% |
| net $/24h | 23.11 | 8.12 | −65% |
| **P(survive to row 2)** | 0.6871 | 0.6887 | **+0%** |
| **1-row share (died in <2s)** | 31.29% | 31.13% | **−0%** |
| **median lifetime** | 8.02s | 8.17s | **+2%** |
| **median arrival $** | 0.0455 | 0.0458 | **+1%** |
| median net \| credited | 0.0249 | 0.0227 | −9% |
| **median buy cost/set** | 0.9668 | 0.9680 | **+0%** |
| **median buy ROC %** | 0.7065 | 0.7093 | **+0%** |
| grind eps/24h (arr ≤ $0.10) | 193.0 | 116.3 | −40% |
| grind mean net/ep | 0.0287 | 0.0311 | +8% |
| distinct slugs / cities | 183 / 49 | 175 / 49 | −4% / 0% |

- **KS test, arrival-$ distribution EARLY vs LATE: D=0.034, p=0.878** (n=783 vs 485). The *shape*
  of the opportunity-size distribution is unchanged; only the *rate* moved. Competition's
  fingerprint is selective removal of the **large** arrivals — the share of episodes over $0.10 went
  26.1% → 28.0% and over $1 went 3.07% → 4.12%, i.e. **up**.
- **KS on lifetimes: D=0.313, p<0.001** — but in the *wrong direction for competition*. Median
  8.02 → 8.17s, p75 52.4 → 66.8s; the rows-per-episode histogram shifts toward **longer**
  episodes (3-5: 21.6→22.7%, 6-20: 18.5→20.2%, 21-100: 11.7→12.6%, 100+: 4.7→5.6%), with only the
  2-row bucket falling (12.1→7.8%). Prices are standing *longer*, not shorter.

### Shadow probe (direct ~0.7s race measurement)

Per-probe, pooled — this is the largest-n survival measurement available:

| set | probes | alive % | $-weighted survival | median delay |
|---|---|---|---|---|
| EARLY | 17,705 | 94.9% | **91.0%** | 0.69s |
| LATE | 11,698 | 94.5% | **91.6%** | 0.72s |

Per-day $-weighted per-probe survival: 93.0, 92.4, 85.2, 88.2, 90.6, **94.8** — the highest value in
the series is 08-01, the worst P&L day.

Per-episode (first probe of each standing mispricing = the moment the replay credits), stratified:

| arrival stratum | set | n | alive % | $-wt survival | median k_now/k_orig |
|---|---|---|---|---|---|
| ≤ $0.10 (the grind) | EARLY | 472 | 59.5 | 73.9 | 1.000 |
| ≤ $0.10 | LATE | 349 | **66.2** | **77.1** | 1.000 |
| $0.10–$1 | EARLY | 138 | 57.2 | 48.2 | 0.661 |
| $0.10–$1 | LATE | 116 | 51.7 | 43.4 | 0.544 |
| ≥ $1 | EARLY | 19 | 63.2 | 19.1 | 0.224 |
| ≥ $1 | LATE | 20 | **25.0** | **6.8** | **0.000** |

**The one competition-shaped result in this whole report is the ≥$1 row**: 12/19 alive → 5/20,
Fisher p=0.025. The replay's own row-2 crediting agrees on a slightly different episode set
(17/24 → 8/20, Fisher p=0.066, §5). Flagged as fragile and not load-bearing:

- n≈20 per cell, and "alive" is a $>0 binary — three of the five late survivors are $0.04, $0.18, $0.30.
- The two tests are **not independent**: they measure the same flashes 0.7s and ~2s after arrival.
  Call it one marginal result, not two.
- It is one of three strata tested and the ≤$0.10 stratum moved the **opposite** way (p=0.058
  *improving*). Two of three strata at p<0.07 in opposite directions is what noise looks like.
- Every ≥$1 late probe is listed in the raw output; the LATE set is 8 boards on 07-30, 9 on 07-31,
  3 on 08-01. No single board or hour drives it.

If real it means flashes specifically are being raced harder. Sizing it: had the LATE tail kept
EARLY's row-2 survival (0.708 not 0.400), late credited tail net scales 8 → 14 episodes, roughly
$1.54 → $2.7/day. **~$1.2/day of a $15/day fall.** A live hypothesis worth re-reading in a week —
not something to spend on.

---

## 5. Tail vs body — where the dollars actually went

| window | tail (arr > $1) | body (arr ≤ $1) |
|---|---|---|
| EARLY | 17 credited episodes → **$10.98/day** | 521 credited episodes → **$12.13/day** |
| LATE | 8 credited episodes → **$1.54/day** | 326 credited episodes → **$6.57/day** |

Of the $14.99/day fall, **$9.44/day (63%) is the tail**. Two things moved inside it, and they must be
separated:

| tail (arr > $1) | EARLY | LATE |
|---|---|---|
| episodes **arriving** | 24 | 20 |
| arrival $ | $182.74 | $51.30 |
| **P(survive to row 2)** | 0.708 | **0.400** (Fisher p=0.066) |
| credited row-2 gross $ | $33.28 | $4.79 |

Flash **arrivals barely fell** (24 → 20). What fell was their *size* (best $43.89 → $10.32; the EARLY
window happens to contain the 1st, 3rd, 4th, 5th and 6th largest arrivals in the whole 7-day log)
and their *row-2 survival* (0.708 → 0.400). Bootstrapping from the 44 pooled tail arrivals:

| quantity | n | observed | pooled-bootstrap p5 / p50 / p95 | percentile of observed |
|---|---|---|---|---|
| EARLY tail arrival $ | 24 | $182.74 | $69 / $123 / $203 | 89.8 |
| LATE tail arrival $ | 20 | $51.30 | $54 / $101 / $176 | **3.3** |
| EARLY credited tail net | 17 | $32.94 | $10.8 / $24.6 / $43.4 | 77.6 |
| **LATE credited tail net** | 8 | $4.63 | $3.0 / $11.2 / $24.7 | **12.7** |

The number that actually enters P&L — credited tail net — sits at the **12.7th percentile** of what
20 draws from the pooled tail distribution produce. That is an unremarkable bad draw. The arrival-$
line is more extreme (3.3rd percentile) and is where the tail-survival effect shows up.

The remaining $5.56/day (37%) is the body, and within
the body it is **entirely a count effect** (521 → 326 episodes, −37%) with the per-episode economics
flat (median net $0.0249 → $0.0227, MK p=0.469; row-2 survival 0.661 → 0.638 in $0.10–$1 with
Fisher p=0.709, and 0.694 → 0.722 in ≤$0.10 with p=0.374).

The daily numbers are dominated by single events throughout — top-1 episode as a share of the day's
net: 15.9% (07-28), **52.2%** (07-29), 16.2% (07-30), 20.3% (07-31), 22.4% (08-01), and 76.6% on
07-26. The 15 largest arrivals in the whole window:

| when | side | arrival $ | row-2 $ | rows | local h | board |
|---|---|---|---|---|---|---|
| 07-28 09:31 | buy | 43.89 | 0.14 | 6 | 17 | wuhan hi 07-28 |
| 07-26 13:51 | buy | 41.12 | 13.80 | 17 | 15 | milan hi 07-26 (the audited flash) |
| 07-29 10:00 | buy | 33.44 | 0.43 | 4 | 13 | jeddah hi 07-29 |
| 07-28 04:32 | buy | 22.52 | 2.07 | 2 | 13 | tokyo hi 07-28 |
| 07-29 07:53 | buy | 12.67 | 9.53 | 25 | 16 | tokyo hi 07-29 |
| 07-28 09:34 | buy | 10.89 | 0.11 | 59 | 12 | moscow hi 07-28 |
| **08-01 05:03** | buy | **10.32** | 0.13 | 4 | 13 | kuala-lumpur hi 08-01 |
| 07-27 06:01 | buy | 9.25 | 8.12 | 25 | 8 | warsaw hi 07-27 |
| 07-29 07:00 | buy | 7.87 | 0.02 | 2 | 16 | busan hi 07-29 |
| 07-26 14:34 | buy | 6.51 | 6.57 | 136 | 16 | milan hi 07-26 |
| 07-27 05:16 | short | 6.37 | 0.00 | 1 | 14 | tokyo lo 07-29 |
| 07-27 09:30 | buy | 5.60 | 2.64 | 7 | 15 | lucknow hi 07-27 |
| 07-30 05:12 | short | 5.42 | 0.00 | 1 | 1 | nyc hi 07-30 |
| 07-31 11:50 | short | 4.60 | 0.00 | 1 | 23 | wellington hi 08-02 |
| 07-27 19:30 | buy | 3.94 | 0.00 | 1 | 3 | shanghai lo 07-28 |

Note 08-01 **did** draw a $10.32 flash — it simply died before row 2, as flashes routinely do on
every day in the sample (8 of the top 15 credit at ≤$0.15). Flash *arrivals* are not gone: 24
arrived in the EARLY window and 20 in the LATE one; only the *credited* count fell 17 → 8.

**The "cities disappeared" story is the tail in disguise.** wuhan's early arrival $48.21 → late
$0.77, tokyo $43.81 → $1.58, busan $12.70 → $0.56 — but wuhan is *one* episode, tokyo is *two*.
The grind is broad and its decline is uniform: 579 grind episodes over 49 cities early → 349 over
the same 49 cities late; **median per-city late/early ratio 0.647 vs overall 0.603.** No city
composition shift, just a proportional thinning everywhere. 38/49 cities down, 11/49 up.

---

## 6. What did shift: the "stale afternoon quote" signature weakened

The 07-27 audit found 95.5% of P&L on same-local-day boards, 90.7% after local noon. Now:

| window | net | same-local-day | same-day **and** local ≥12:00 |
|---|---|---|---|
| EARLY | $69.33 | 94.5% | 75.3% |
| LATE | $24.35 | **77.1%** | **57.5%** |
| EARLY, body only (arr ≤ $1) | $36.39 | 90.1% | 75.7% |
| LATE, body only | $19.72 | **71.7%** | **47.5%** |

Local-hour arrival rates (per 24h) show the money window is still the money window, just thinner —
local 12:00–17:00 falls 172.7 → 95.0 eps/24h (−45%) against an overall −38%, so its share of
arrivals slips 66.2% → 58.8%. That is a mild thinning, not a disappearance. What actually moved the
*net* share is that the afternoon flashes stopped landing, so next-day and pre-noon grind boards
make up more of a much smaller total. Another face of the tail effect, not a new edge.

---

## 7. Supply side — the market is not quieter, and books are not tighter

Two independent checks, both pointing away from "the opportunity source dried up naturally".

**Traded volume (Gamma, per event, all 546 cached boards):**

| market date | boards | total volume | median board |
|---|---|---|---|
| 2026-07-26 | 56 | $3,411,895 | $51,191 |
| 2026-07-27 | 57 | $3,876,416 | $47,383 |
| 2026-07-28 | 57 | $3,542,032 | $46,773 |
| 2026-07-29 | 57 | $3,652,410 | $46,763 |
| 2026-07-30 | 57 | $3,472,623 | $43,425 |
| 2026-07-31 | 57 | $3,530,328 | $45,986 |
| 2026-08-01 | 57 | $3,366,343 | $48,948 |

**Volume is flat: −13% peak-to-trough while our arrival rate fell −59%.** So this is not "everyone
went on holiday". The order flow that creates stale quotes is still there; fewer of the resulting
prices clear our net-of-fee bar.

**Fee-blind gross mispricing (`illusion`, logged every sweep over the whole board universe):**

| day | dow | mean illusion $/sweep | median | hits/sweep |
|---|---|---|---|---|
| 07-27 | Mon | 4.00 | 3.56 | 0.144 |
| 07-28 | Tue | 3.08 | 2.90 | 0.237 |
| 07-29 | Wed | 3.55 | 3.06 | 0.139 |
| 07-30 | Thu | 4.03 | 3.58 | 0.101 |
| 07-31 | Fri | 3.80 | 3.22 | 0.105 |
| 08-01 | Sat | **7.32** | **6.28** | 0.075 |
| 08-02 | Sun | **7.19** | 6.01 | 0.017 |

Gross crossing dollars nearly **doubled** on the two worst days. If makers were tightening quotes
(the classic competition mechanism) this number would fall, not rise. Across 27 six-hour blocks the
correlation between illusion and hits/sweep is only mildly negative (Pearson −0.18, Spearman −0.12),
so this is suggestive, not conclusive — but it is squarely inconsistent with "spreads compressed".

The mechanical reading: gross slack is up but it now sits on legs where the per-leg taker fee
(`0.05·p·(1−p)`) exceeds it. The proxy for that is fee/cost on hit boards — implied mean(1−p) on buy
rows moved 0.26 (07-27) → 0.50–0.66 (07-28 onward), i.e. **the boards showing edge became less
decided**, exactly the regime in which the fee is largest. That is a coherent regime story and it is
consistent with the same-day-afternoon share falling in §6 — but it rests on a selection-biased
statistic (only net-positive boards are logged) and I would not put weight on it.

---

## 8. Is 08-01 just one bad draw?

Three tests, and they disagree in an informative way.

**Within-day bootstrap** (resample that day's credited episodes, 10k draws):

| day | observed net | boot p5 | p50 | p95 | vs pooled episode sizes at that day's n → percentile |
|---|---|---|---|---|---|
| 07-27 | 34.85 | 20.65 | 33.65 | 52.69 | 91.8% |
| 07-28 | 14.07 | 9.44 | 13.79 | 19.53 | 37.6% |
| 07-29 | 20.41 | 8.94 | 19.62 | 39.30 | 74.1% |
| 07-30 | 8.64 | 5.68 | 8.50 | 12.14 | 19.4% |
| 07-31 | 11.94 | 7.82 | 11.72 | 17.23 | 48.2% |
| 08-01 | **3.77** | 2.44 | 3.71 | 5.28 | **2.1%** |

The last column holds the day's episode *count* at its actual value and draws episode *sizes* from
all five days. 08-01 sits at the 2.1st percentile — so it was low on **both** count and size, not
just count. Mann-Whitney on net-per-credited-episode, 08-01 vs the other five days pooled:
median $0.0117 vs $0.0256, z = −2.08, **p = 0.037** — but arrival-$ per episode is *not* different
(median $0.0430 vs $0.0458, p = 0.682) and lifetimes are not different (p = 0.332). So 08-01's
smaller per-episode net is a crediting/haircut draw on ~83 episodes, not smaller opportunities.

**Day-of-week.** 07-27 Mon → 08-01 Sat is monotone-ish and 08-01 is a Saturday, so a weekly cycle is
the natural first guess. Two things cut against leaning on it:

- Mon–Fri alone still trends −14.0%/day (t = −5.51). The decline was well under way before the weekend.
- The one earlier weekend in the log runs the **other** way. Matching the 12:00–24:00Z window across days:

| day | dow | coverage | eps | eps/h | arr $ | net $ |
|---|---|---|---|---|---|---|
| 07-26 | **Sun** | 10.32h | 90 | **8.73** | 60.09 | 26.53 |
| 07-27 | Mon | 12.00h | 100 | 8.33 | 22.24 | 14.46 |
| 07-28 | Tue | 12.00h | 63 | 5.25 | 11.09 | 1.90 |
| 07-29 | Wed | 12.00h | 82 | 6.83 | 5.87 | 2.72 |
| 07-30 | Thu | 12.00h | 53 | 4.42 | 7.10 | 1.84 |
| 07-31 | Fri | 12.00h | 78 | 6.50 | 16.53 | 6.66 |
| 08-01 | **Sat** | 12.00h | 48 | **4.00** | 6.81 | 1.37 |

The previous Sunday was the **busiest** afternoon in the sample. That is n=1 on a partial day that
also contained the Milan flash, so it does not kill the weekend hypothesis — but it means the
weekend story currently has one supporting observation (Sat 08-01) and one contradicting one
(Sun 07-26).

---

## 9. What the data can and cannot say

**Can say, with confidence:**

1. We are **not** losing races we used to win *on the grind*. Five survival measures on 1,268
   episodes and 29,403 probes are flat or improving. Latency is not the binding constraint on the
   bulk of the observed decay.
2. The per-unit economics of a hit are **unchanged**: same arrival size distribution (KS p=0.88),
   same per-set cost (0.9668 → 0.9680), same ROC (0.707% → 0.709%), same lifetime.
3. Fee-beating mispricings are **arriving about 40% less often**, broadly across all 49 cities and
   across the same set of boards, and this is far outside sampling noise (permutation p=0.003).
4. About 60% of the *dollar* fall is the tail: flash arrivals held up (24 → 20) but drew small and
   credited worse. The credited tail net sits at the 12.7th bootstrap percentile — an ordinary bad
   draw, carrying essentially no statistical weight either way.

**Cannot say:**

1. **Why** the arrival rate fell. Volume flat, universe constant, code constant — so "the market
   went quiet" and "the board mix changed" are both ruled out as sufficient explanations. The
   remaining candidates are (i) a weekly/holiday cycle, (ii) a weather-regime effect on how decided
   boards are, (iii) makers quoting tighter relative to the fee. **Six days and one weekend cannot
   separate these**, and (iii) is the one that does not come back.
2. Whether flashes specifically are being raced harder (the ≥$1 stratum, two non-independent tests
   at p=0.025 and p=0.066, ~20 episodes per cell). Worth ~$1.2/day if true — real but small.
3. Anything about the level of the run-rate to better than roughly a factor of two. Bootstrap over
   the whole window's episodes puts the honest interval wide; the LATE window's $8.12/day, ex-top-5
   $5.88/day, is the most defensible current estimate and it sits right on the $5/day bar.

---

## 10. What would falsify this

Ordered by how fast they resolve. All free — the scanner is already collecting.

| # | If the answer is… | then in the next 3–7 days… | already-observed baseline |
|---|---|---|---|
| F1 | **(a) fill-race competition** | `P(survive to row 2)` drops below ~0.60 and per-probe $-weighted 0.7s survival drops below ~85% | 0.648 and **91.6%** — currently the strongest disconfirmation in the report |
| F2 | **(a) at the quoting level** (the worrying one) | median buy `cost/set` creeps 0.968 → 0.98+, median buy ROC 0.71% → under 0.4%, and `illusion` **falls** | 0.9680 / 0.709% / illusion **rising** — all three currently point away |
| F3 | **(b) weekly cycle** | Mon 08-03 / Tue 08-04 arrival counts rebound to ≥230 eps/24h | Mon 07-27 = 309, Sat 08-01 = 128 |
| F4 | **(b) structural, permanent** | Mon 08-03 / Tue 08-04 stay at ≤160 eps/24h | same |
| F5 | **(c) tail noise** | flash arrivals keep coming at ~6–7/day and one again credits over $5 | arrivals EARLY 8.0/day → LATE 6.7/day (barely moved); credited 5.7 → 2.7/day |
| F6 | **flashes raced harder** (the one live (a) sub-hypothesis) | ≥$1 row-2 survival stays under ~0.45 over the next ~20 flash episodes | EARLY 0.708 → LATE 0.400 |

**F3 vs F4 is the single decision-relevant test and it costs nothing but two more days.** Until it
reads, treat the run-rate as the LATE window's **$8/day (ex-top-5 $5.88/day)**, not the full-window
$18.53/day.

Recommendation for the plan: this does **not** kill the arb (A1's kill condition was "if it's
competition, it continues, and nothing below matters" — it is not competition as defined). Proceed
to **A2, the legging simulation**, as planned, and re-read the F3/F4 arrival count on 08-04.

---

### Reproduction

Snapshot: `/tmp/claude-0/-root-weather-trading-bot/32608018-29d1-4057-ace5-c17b14fe4888/scratchpad/arb.jsonl`
(copy of `logs/negrisk_arb.jsonl` at 2026-08-02T04:33Z). Analysis scripts `a1.py`–`a6.py`, `vol.py`,
`tzmap.py` in the same directory. Baseline check:

```
venv/bin/python -m backend.data.negrisk_arb_pnl --log <snapshot> --gas 0.02
venv/bin/python -m backend.data.negrisk_shadow_report --log <snapshot>
```

Episode grouping throughout matches `negrisk_arb_pnl.executions` exactly: per slug+side, 300s gap,
120s warmup drop, credit at row 2, gas $0.02/position. Timezones for the 49 cities are hand-mapped
in `tzmap.py` (13 of them cross-checked against `backend/data/weather.py:CITY_CONFIG`); local-hour
figures are only as good as that map, but no conclusion in this report turns on it.
