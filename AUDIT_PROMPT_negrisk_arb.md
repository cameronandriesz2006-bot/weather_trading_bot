# Audit prompt — negRisk arb test (Plan B, phases B0/B1)

Paste this into a fresh session. It is written to be self-contained.

---

You are auditing a live measurement experiment in `/root/weather_trading_bot` (branch
`research/arb-and-edge-analysis`, HEAD `388e25a`). **Be adversarial.** Your job is not to
confirm the work — it is to find what is wrong with it, what it fails to measure, and where it
could produce a confident answer that is false. This project has a documented history of
"edges" that evaporated under scrutiny (see `AUDIT_2026-06-29.md`, and the memory files
`edge2-live-test-verdict-2026-07-26`, `edge1-freemoney-not-durable`). Assume the same thing is
happening here until you have verified otherwise. **Verify empirically — run the code, query
the live API, check the logs. Do not audit by reading source alone.**

## What the experiment claims

Polymarket daily-temperature events are `negRisk` boards of 11 mutually-exclusive, exhaustive
temperature buckets; exactly one settles YES. The claim is that buying NO on a subset `S`
(size `m`) of a board guarantees a payout of at least `m-1`, so:

    guaranteed profit = (m-1) - sum(NO_ask over S) = sum_{i in S}(1 - NO_ask_i) - 1

and therefore a leg belongs in the trade exactly when its NO costs under $1. This needs no
weather forecast at all. A scanner sweeps all ~149 boards every 2 seconds, walks every leg to
full depth, and logs what was executable. A report then counts each opportunity **once** and
produces a profit-vs-latency curve. The go/no-go bar is **>$50/day executable**.

## The pieces

- `backend/data/negrisk_arb_scan.py` — the scanner. Runs as `negrisk-arb.service` (systemd,
  `Restart=always`), 2s sweeps, re-enumerates the event universe every 30 min.
- `backend/data/negrisk_arb_report.py` — window grouping + latency curve.
- `backend/data/orderbook.py` — `fetch_books` (recently parallelized, 9.34s → 0.96s).
- `logs/negrisk_arb.jsonl` — current log. **Two earlier phases were rotated out:**
  `negrisk_arb_60s_baseline.jsonl` (60s cadence) and `negrisk_arb_strict_phase.jsonl`
  (all-legs-required logic). Do not mix formats when analysing.
- `weatherbot.service` — the main bot, now in shadow mode (`WEATHER_TAKER_ENABLED=false`),
  unrelated to this test but must not be disturbed. **SIMULATION_MODE stays True. There is no
  live-execution path and you must not build one.**

## A. Is the arb identity actually correct?

1. **YES/NO token mapping.** If YES and NO token ids are swapped anywhere, every number
   inverts and the whole result is garbage. `fetch_event_tokens` picks the index from Gamma's
   `outcomes` field. Cross-check a sample of markets against the CLOB `/markets` endpoint's
   `tokens[].outcome` labels. Prove the mapping is right, don't assume it.
2. **Are we reading the side we think we are?** We buy at *asks*. Verify `_parse_levels` and
   the sort order in `orderbook.py`, and confirm against a raw `/book` response that the
   cheapest ask is what a buyer pays.
3. **Exhaustiveness and mutual exclusivity.** A one-off check found 153/153 boards exhaustive
   (no gaps, open tails both ends) by parsing `groupItemTitle` with `parse_bucket_label`.
   Re-run it. Then ask: is it checked *continuously*? New boards appear every 30 minutes and
   the scanner does **not** verify exhaustiveness before trading the identity. Is that a real
   hole? Can a board exist with a gap, a duplicate, or a non-partitioning bucket set?
4. **Does `negRisk: true` actually guarantee exactly one winner?** What happens on a voided,
   invalid, or 50/50-resolved market? What if a board is still open but a bucket is already
   settled? Does the guarantee survive that?
5. **Boundary/rounding.** Settlement rounds to integer °F. Confirm no temperature can fall
   between two buckets, and that °C boards (single-degree buckets) partition correctly too.

## B. Is the optimization correct?

6. For a fixed `k`, the code includes leg `i` iff `cost_i(k) < k`. Prove or disprove that this
   is optimal (profit is additive across legs — check that reasoning holds).
7. `best_subset_short` subsamples candidate `k` values to `max_candidates=160`. The claim is
   this can only *under*estimate profit (conservative). Verify. Construct a case where
   subsampling misses the optimum and quantify the error.
8. `cost_for_shares` — check off-by-one and float-tolerance behaviour at exact level
   boundaries and when depth is exactly `k`.
9. `short_strict` (all-legs) should equal `short` when all legs are used. Confirm on real data.

## C. Costs and realism — the most likely place this is wrong

10. **No costs are modelled at all.** Quantify: Polygon gas for `m` legs plus redemption and
    any negRisk conversion; Polymarket's fee schedule (is there a taker fee on these markets?
    the repo believes realized fees are 0 — verify against current docs//markets metadata).
    At $0.30 profit across 8 legs, do costs exceed the entire edge? Compute the break-even
    profit-per-board and report how many observed opportunities clear it.
11. **Capital.** The report gives peak capital for a *single* board. Compute the **total
    simultaneous** capital required across all concurrently-open windows and compare it to a
    realistic bankroll ($10k sim). If 22 boards each need $500, the achievable profit is
    capital-capped and the headline $/day is unreachable. Quantify the capped number.
12. **Minimum order size.** The code treats `MIN_ORDER_SHARES = 5.0`. Verify `orderMinSize` is
    5 *shares* and not $5, and that it applies per leg.
13. **Tick size.** Orders round to $0.01. Does rounding erase thin edges? Re-price the observed
    opportunities with tick-rounding applied.
14. **Execution risk.** The measurement assumes all `m` legs fill simultaneously at quoted
    prices. In reality you leg in over hundreds of milliseconds and get picked off on the rest.
    Is the reported number an upper bound? By how much? Note the box is in **Singapore with
    171ms RTT to the CLOB** — model the cost of that.
15. **Market impact.** Our own order consumes the depth being measured. Does the depth walk
    account for the fact that we are the marginal buyer?

## D. Measurement methodology

16. ⚠️ **Snapshot coherence — check this carefully.** `fetch_books` issues ~7 concurrent
    requests taking ~1s total. Books for different legs therefore arrive at slightly
    *different moments*. An "arb" computed across legs stitched from non-simultaneous
    snapshots may not have existed at any single instant. Quantify how much of the observed
    profit could be this artifact. Compare against the websocket
    (`wss://ws-subscriptions-clob.polymarket.com/ws/market`, needs a literal `PING` every 10s,
    shard ~500 assets per connection) which measured ~310 book changes/second across 500
    tokens. **This is a prime suspect for a fake edge.**
17. **Latency curve validity.** The curve credits each window with its **peak** profit if the
    window outlived the latency. But a trader reacting `L` seconds late captures what is
    available *at* `L`, not the peak. Does this overstate capture at high latency? Fix or
    quantify.
18. **Window identity.** Windows are keyed by slug only, so a board showing both a `buy` and a
    `short` opportunity merges into one window. Does that lose or double-count profit?
19. **Gaps vs. silence.** The scanner logs a `sweep` row every pass so replay can distinguish
    "no edge existed" from "we weren't watching". Verify the report actually uses this, and
    check the log for service restarts or cadence drift (timestamp deltas — is it really
    holding 2s?). Report any period where the scanner was down.
20. **Diurnal bias.** A sample dominated by one part of the day may not extrapolate. The board
    set spans Asia, Europe and the US. Break results down by hour-of-day and by city region
    before trusting any $/day figure.
21. **Sample sufficiency.** State the minimum observation span needed for the $/day figure to
    be meaningful, and whether the collected data meets it. Give a confidence interval, not a
    point estimate.

## E. Coverage

22. Only `tag_slug=daily-temperature` is scanned. Are there weather markets outside that tag?
    More importantly: **the same subset identity applies to every negRisk multi-outcome event
    on the platform** (elections, sports, mentions). Is the test needlessly narrow? Estimate
    the opportunity outside weather.
23. Confirm `fetch_books` never silently drops tokens (an earlier run returned all 3,278).
    Check for partial responses under load.
24. Does the 30-minute re-enumeration correctly add new boards and drop closed ones? Is the
    token cache (`logs/negrisk_tokens.json`) ever stale or wrong?

## F. The bar itself

25. Is **>$50/day** the right go/no-go? It should account for capital tied up, gas, execution
    risk, and the fact that capturing *any* of this requires building a live-execution path
    (order signing/submission/reconciliation) that does not exist and is a 2–3 week build.
    Propose a defensible bar and justify it.

## Deliverable

Report, in this order:

1. **Verdict on correctness** — is the arb identity, as implemented, sound? Any defect that
   would produce a fake edge, with the specific failing input.
2. **Verdict on the measurement** — is it measuring what we need? What is it blind to?
3. **A corrected $/day estimate** with costs, capital cap and execution realism applied, as a
   range with a confidence interval.
4. **Top 5 concrete fixes**, ranked by how much they change the answer.
5. **Your own go/no-go recommendation** against a bar you defend.

Be blunt. If the honest answer is "this is another $15/month scrap like Edge-1, kill it," say
exactly that. If you find the measurement is inflated by the snapshot-coherence problem in
item 16, say that plainly and quantify it. A confident wrong "yes" here costs weeks of build
time; a wrong "no" costs almost nothing.

Do not modify the running services or the live bot. Read-only analysis plus new throwaway
scripts under the scratchpad is fine; if you need to change scanner logic, propose it rather
than deploying it mid-collection.
