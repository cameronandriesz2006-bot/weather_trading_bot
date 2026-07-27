# Audit verdict — negRisk arb P&L replay (2026-07-27)

Adversarial audit of `ARB_PNL_2026-07-27.md` ($106.30/day headline; $65.84/day on a $100
bankroll). Three parallel agents (execution realism / data integrity / independent
reconstruction), each of which **independently reproduced the claimed numbers to the cent**
before attacking them — so everything below is a dispute about assumptions, not arithmetic.
Per `AUDIT_2026-07-26_negrisk_arb_VERDICT.md`, token mapping, partition structure, snapshot
coherence and the fee were treated as settled and were not re-litigated.

## 1. The answer in one line

**The ~$60/day-on-$100 is not real — the defensible recurring number is roughly $5–10/day, it
needs $1,000+ of locked capital rather than $100 (on an actual $100 bankroll it is ~$1.50/day),
and even that assumes an untested fill.**

## 2. What actually happened

- **The simulator banked prices the instant it saw them, with zero execution latency** — even
  though the logged prices were already ~1s old at their own timestamp (the scanner stamps `ts`
  *after* the book fetch) and no order could reach the book before the next 2-second sweep.
  Requiring the price to survive one sweep, paying the surviving (worse) price, and charging
  gas → **~$59/day of the $106 was fake** ($106.30 → $47.03).
- **One 2-second trade carried 60% of all profit** (Milan, a same-day board at 15:51 local,
  when the day's high was essentially decided; someone briefly sold the winning bucket for
  pennies). Its depth collapsed 60% in 2.05s — direct evidence faster bots were eating it while
  we would still have been filling 11 legs. Credited at what survived, $41.12 → $13.80; and
  such flashes were observed **once in ~19h of scanning** (zero in the comparable prior
  afternoon) → **another ~$21/day rests on an n=1 event** ($47.03 → $25.62 ex-top-1).
- **The "$66/day on $100" was ~98% that single flash.** On a $100 bankroll, hold-to-resolution,
  excluding Milan, the greedy sim earns **$1.40–1.51/day** — small capital sits locked ~a day
  in sub-2%-return positions while the grind passes by.
- **The remaining grind is small and fragile**: ex-top-5 it is **$9–11/day**, 95% of P&L comes
  from same-day nearly-decided boards (90.7% after local noon — this is "pick off stale quotes",
  not broad board arb), and legging risk on 11-leg buys — a partial fill can pay $0 — is
  unmodelled and bounded at **−$2 to −$8/day**, the same order as the grind itself.
- **What was NOT wrong** (state it plainly): the accounting is honest. Exact reproduction ×3,
  no lookahead, no resolution peeking, no double-counting, no mark-to-market inflation, clean
  timestamps, no survivorship bias — and the identity **paid**: every resolved top board had
  exactly one winner (Milan's 31°C bucket won; 93.21 sets at $50.67 would genuinely have paid
  $93.21 *if filled*). The code didn't cheat; the crediting assumptions did.

## 3. The real numbers

| metric | reported | true (base case) |
|---|---|---|
| daily P&L (at ~$2.5k capital) | $106.30/day | $47/day **only if** the 2s flash repeats ~daily; **$7–11/day recurring** (pre-legging-risk) |
| daily P&L on $100 | $65.84/day | **~$1.50/day** ex-flash ($23/day if you'd have won the race for the flash) |
| realized vs unrealized | treated as banked at entry | $0 cash until each board resolves (~1 day); no unrealized-mark inflation found; payouts verified real |
| win rate | 100% implied ("no loss mode") | 56% of trades still pay after latency+gas; 24% of targets vanish in <2s (missed, $0); true losses (partial fills) unmeasured |
| max capital the strategy absorbs | ~$2,500 | ~$2,500 confirmed (peak locked $1,782 base case) |

Worst case ≈ **$0/day** (3rd-sweep fills, no tail, legging charged). Best case ≈ **$100/day**
(every reported assumption holds AND flashes recur daily — nothing in the data supports this).
Bootstrap over episodes: 90% interval **$31–231/day** — one day of data cannot pin the number.

## 4. What we fixed

- Fixed the replay's zero-latency fills: `negrisk_arb_pnl.py` now credits a trade only if its
  price survived to the next 2s sweep, at the surviving price (`--credit-row`, default 2;
  `--credit-row 1 --gas 0` reproduces the pre-audit headline for the record).
- Turned gas on by default ($0.024/position) so the $0.055-median trade is no longer costless.
- Flagged the "merge capital back in 10 min" bankroll column as invalid until buy-side set
  merging is verified (93% of P&L is buy-side; only hold-to-resolution stands).
- Annotated `ARB_PNL_2026-07-27.md` and `CLAUDE.md` so the rejected headline cannot be quoted
  without this audit.
- Scanner and accounting needed no fixes — verified clean.

## 5. What to do next

1. **Keep the scanner + probe running** (they already are; cost nothing). The sample keeps
   growing regardless of the decisions below — flash frequency (is the tail ~daily or
   ~monthly?) and weekday coverage need weeks and keep accruing in the background.
2. **Build the shadow-fill probe** — **DONE, live 2026-07-27 08:06Z**: `shadow_probe` in
   `negrisk_arb_scan.py` re-fetches every hit board's books ~1s after scoring and logs a
   `shadow` row (orig vs still-there profit); summarise with `negrisk_shadow_report.py`
   (per-probe = book flicker; per-episode = the replay haircut). The audit's own 10-min
   manual probe measured **39% dollar-weighted survival at ~1s** on 7 hits — the go/no-go
   reads this at scale instead.
3. **Verify buy-side early merge** (can a complete 11-leg YES set be converted to USDC
   pre-resolution?). If not, capital turns over ~once/day and small-bankroll operation is dead
   regardless of edge.
4. **Go/no-go read on 2026-07-28** (user decision 2026-07-27: ~24h of probe data, a week
   of simulation was too long), bar set by the user: if shadow-fill-verified P&L ≥
   **$5/day** (tail counted only as actually measured), the arb stays alive — build
   execution and run a **micro-live measurement test: $25–30 bankroll, minimum-size
   (5-share) orders on every hit, ~1 week / 40–100 attempted fills** (capital is for
   measurement, not income; expected cost of the experiment ≈ $0–10). That test measures
   what no simulation can: race win-rate, partial-fill rate/cost, market adaptation. Only
   if those hold does real capital scale toward the ~$2.5k ceiling. If < **$5/day**,
   **stop**, and keep only the maker-side idea (makers pay no fee and earn a 25% rebate).
   Caveat for the 24h read: it answers grind survival well (hundreds of probed episodes);
   it cannot answer flash frequency — that stays n≈1 and keeps accruing per step 1.

## Appendix (technical)

**Crediting-rule restatement** (fixed tool, `--until 2026-07-27T05:07:00+00:00`, gas $0.024):

| rule | trades | $/day | ex-top-1 | ex-top-5 | $100 hold | peak locked |
|---|---|---|---|---|---|---|
| row 1, gas 0 (reported) | 158 | 106.30 | 42.35 | 24.75 | 65.84 | $3,178 |
| row 2 + gas (base) | 120 | 47.03 | 25.62 | 11.36 | 23.15 | $1,782 |
| row 3 + gas (worst) | 100 | 40.70 | 22.57 | 9.30 | 19.57 | $1,381 |

**Latency mechanics**: `negrisk_arb_scan.py:511–513` stamps `ts` after `fetch_books` (median
fetch_s 1.01s); old replay credited `ep[0]` (`negrisk_arb_pnl.py`, pre-audit). 38/158 episodes
(24%) were single-row — price gone within ~2s, all verified against a live next sweep ≤3.3s
away. 92.5% of arrival-dollars sit in episodes with ≥2 rows, but at collapsed prices/sizes only
48% of the dollars survive to row 2.

**Milan flash anatomy** (`highest-temperature-in-milan-on-july-26-2026`, 13:51:11Z): yes-ask-sum
0.296 (k=93.21, $41.12 net) → +2.05s 0.596 (k=37.64, $13.80) → +2.08s 0.597 ($11.68) → gone →
$0.38 standing residue. Depth −60% in 2.05s. Not a feed artifact: 3 independent full snapshots,
only board hitting that sweep, `illusion` continuous, and missing books can only *suppress* a
buy row (`orderbook.py:133–146` → `complete_yes=False` → scan.py:452), never create one. The
board resolved 31°C — the flash was real free money for whoever won the race. A second Milan
episode at 14:34:41Z ($6.51 arrival / $6.57 row-2, cost $513.76) stood ~4.5 min and survives
all rules. Milan board total = 69.7% of all P&L.

**Regime split** (Agent 2): same-local-day boards $65.24 = 95.5% of P&L; local-time ≥12:00 =
90.7%; station-local 15:00 hour alone $42.44. Next-day boards: $3.11 = 4.5%. Gross-phase log
(10:11–13:40Z same day, 83,772 board rows, more permissive scorer): zero buys with per-set cost
< 0.90 — Milan-class flashes are not ~hourly; base rate 1/19h observed.

**Live shadow-fill probe** (05:37–05:47Z, 23 new hits, 7 probed ~1s later with the scanner's own
`evaluate_event`): 4/7 gone, 1/7 halved, 2/7 standing; 39% dollar-weighted survival —
independently corroborates the ~50% latency haircut.

**Forensics that cleared**: duplicate-fingerprint episode recounting $0.01 (1 episode);
buy+short overlap 1 pair, disjoint liquidity (buy eats YES asks, short eats NO asks = mirrored
YES bids); 0/5,382 rows with sign/fee/roc inconsistencies; 0 non-executable credited; 0
timestamp regressions in 32,232 rows, 0 heartbeat gaps >60s (max 31.9s); universe re-enumeration
mid-run contributed ≤$1.41 (3%); the 1,077-hit 01:00Z hour carries only $3.76 episode-credited.
`resolution_time` +26h approximation is 1.5–3.2h conservative (real resolutions ~22:52Z).

**Costs**: gas −$5.90/day on headline but median trade $0.055 → $0.031 (−44%); $0.024 was
measured on the convert path, slightly overstates buy-side (fills operator-paid, redemption
gasless). Legging: all 5,269 buy rows are 11-leg boards; 1–2% severe-partial at ~−$2.5 avg ≈
−$2 to −$8/day, strictly negative, unmodelled. Adverse selection is structural: what stands
long enough to fill is the $0.02–0.06 grind (k stable, median k2/k1 = 1.00); everything sizable
is consumed in 1–2 sweeps.

Agent scripts: session scratchpad (`recon.py`, `recon2.py`, `audit_log.py`, `audit_supp.py`,
`shadow_fill.py` + `shadow_fill_results.jsonl`).
