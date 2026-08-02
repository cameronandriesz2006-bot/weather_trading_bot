# negRisk arb — go-live test plan (2026-08-02)

This file exists so a cold session can pick the work up without re-deriving anything. It contains
(1) the go/no-go read that was due 2026-07-28 and never happened, and (2) the ordered list of
tests that must pass before real money.

Supersedes the *numbers* in `AUDIT_2026-07-27_arb_pnl_VERDICT.md` (3× the sample); its *method*
and its rejections still stand. `AUDIT_2026-07-26_negrisk_arb_VERDICT.md` still holds for what
must not be re-litigated (token mapping, partition structure, snapshot coherence, the fee).

---

## 1. The go/no-go read (finally done, 2026-08-02)

The scanner ran unattended 07-27 → 08-02. Sample is now **158.3h, 950 executions, 29,522 shadow
probes, 1,132 episodes** — vs ~48h at the audit.

Command that produced this:

```
venv/bin/python -m backend.data.negrisk_arb_pnl --gas 0.02
venv/bin/python -m backend.data.negrisk_shadow_report
```

**Headline: $18.34/day** survival-credited + gas over the full window.
Ex-top-1 **$16.25/day**, ex-top-5 **$11.97/day** (the recurring grind).

**The crediting was honest.** Two independent measurements agree:

| method | $/day |
|---|---|
| replay, credited at row 2 (survived one sweep) | $18.34 |
| shadow probe, arrival value that actually survived 0.7s ($87.16 of $310.43 over 5.82d) | $14.97 |

Agreement within ~20% is the audit's central open question answered — row-2 crediting is not
optimistic. The $47/day estimate was high because the sample was short, not because the method
was wrong.

**But it is decaying.** UTC-day net:

| day | net | trades |
|---|---|---|
| 07-28 | $14.07 | 161 |
| 07-29 | $20.41 | 157 |
| 07-30 | $8.64 | 123 |
| 07-31 | $11.94 | 128 |
| 08-01 | **$3.77** | 83 |

5-day mean $11.77/day clears the user's ≥$5/day bar; the most recent full day does not. Board
count is healthy (127→145 over the same period), so this is **not** a coverage artifact — the
opportunity itself is thinning. Recent sweeps in `logs/negrisk_arb_run.log` are mostly `hits=0`.

**Capital.** $2.81/day on $100 held to resolution; ~$14/day on $100 only if sets can be merged
back to USDC within ~10 min. That merge path does not exist and its gas is unmeasured, so the
entire difference between those two numbers is currently an assumption.

**Still unpriced, both cutting the same way:** legging risk (nothing measures atomic fill yet)
and real merge/gas cost.

### Verdict

Real, measured, small, and trending down. Not a no — but not worth building the execution stack
against until Tier A below is done, because Tier A is free and can kill it.

---

## 2. What exists (checked 2026-08-02)

**No execution plumbing whatsoever.** No `py-clob-client`, no `web3`, no `eth-account` in
`requirements.txt` or the venv. `SIMULATION_MODE: bool = True` at `backend/config.py:32`.
Order signing + submission + reconciliation is a from-scratch build.

This is why the plan is ordered the way it is: **every free test that could kill the idea comes
before any of that build.**

---

## 3. The tests, in order

### Tier A — free, runs against data already on disk. Do these first.

**A1. Is there still an edge to chase?**
Decompose the decay: competition (spreads tightening before we see them) vs board mix (seasonal —
fewer wide-open cities). Split the last 6 days by city and by hour.
*Kill:* if it's competition, it continues, and nothing below matters.

**A2. The legging test — the most important test in this document.**
Everything measured so far is whether the *opportunity* survives. Nothing measures whether *we can
get all 11 legs*. Replay the log buying legs one at a time with realistic per-leg latency; count
how often we end up holding an incomplete set.
Why it matters: a partial fill doesn't merely lose the profit, it converts a locked arb into an
unhedged directional weather bet — the exact strategy that already lost $477.85. Worst case (the
missed leg is the winner) loses the full stake.
*Kill:* if a meaningful share of sets go partial, the ~$12/day grind is gone.

**A3. Did winning boards actually pay $1?**
Take boards from the log that have since resolved; confirm exactly one bucket settled to $1. Tests
exhaustiveness against reality rather than against `board_sanity()`'s own logic.
*Kill:* any board resolving to none-of-the-above breaks buy-the-board outright.

### Tier B — cheap research, no money

**B4. True cost of a round trip.** Real Polygon gas for split/merge/redeem, from actual on-chain
transactions — not the assumed $0.02/position. This one number decides whether $100 earns
$2.81/day or $14/day.

**B5. Is there an atomic path?** Whether Polymarket's negRisk adapter can convert/mint a full
position set in one transaction. If yes, A2's legging risk largely disappears and the economics
change materially.

**B6. The boring API limits.** Minimum order size, rate limits, auth requirements, regional
availability from this machine. Any one can make an 11-leg burst impossible.

### Tier C — build the path, still no money at risk

**C7. Signing dry run.** Build order signing; submit orders priced far enough from the market that
they cannot fill. Proves plumbing without exposure.

**C8. Real end-to-end latency.** Measured time from scanner-sees-it to order-accepted. The shadow
probe shows value decaying hard inside 0.7s; if the real round trip is ~2s the edge was never
reachable.

**C9. Partial-fill handling.** Decide and test what happens when leg 7 of 11 fails — abort and
unwind, or chase. Must work before real money, not after.

### Tier D — real money, deliberately tiny

**D10. One leg, minimum size.** Confirm fill, fee charged, and on-chain position all match
prediction.

**D11. One full set at $25–30.** First genuine test of the strategy. (User set this stake 07-27:
measurement, not income. $500–1k explicitly rejected.)

**D12. Merge and redeem.** Get capital back out. Until this works the economics are the
$2.81/day column.

**D13. Sustained micro-live vs the bar.** Minimum size, ≥$5/day bar, pre-committed loss limit and
shutdown rule.

---

## 4. Standing decisions (do not re-litigate)

- **Bar is ≥$5/day.** User-set 07-27. Do **not** raise it — small consistent profit *is* the goal.
- **Stake for micro-live is $25–30**, min-size 5-share orders.
- `SIMULATION_MODE` stays `True` until Tier C is built and Tier A/B passed.
- The scanner keeps running. Do not stop `negrisk-arb.service` — the sample is the asset.

## 5. Next action

**Start with A2 (legging simulation).** It is free, needs no new data, and is the test most likely
to change the answer.
