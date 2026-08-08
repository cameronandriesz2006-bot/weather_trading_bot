# Live-feed watcher vs every-2-seconds checker — the comparison read (2026-08-08)

This is the read CLAUDE.md scheduled after the live-feed watcher went up on 2026-08-05:
compare what the two detection tools saw over the same period, and decide which one should
drive order-sending when that gets built.

**The two tools, in plain terms:**

- **The checker** (`negrisk-arb.service`) asks the exchange for all board prices every 2
  seconds (each round of asking takes ~0.33s). It has been the baseline since July.
- **The live-feed watcher** (`negrisk-ws.service`) keeps an open connection to the exchange
  and is told about every price change the moment it happens, then re-scores the affected
  board with the exact same profit math the checker uses. It has been watch-only since
  2026-08-05 11:55 UTC.

**Method** (`backend/data/negrisk_ws_sweep_diff.py`, run from the repo root as
`python3 -m backend.data.negrisk_ws_sweep_diff`): take both logs over the 70.1 hours both
tools were running (2026-08-05 11:55 → 2026-08-08 09:58 UTC, no restarts on either side);
group each tool's sightings into "opportunity windows" using the same 300-second gap rule
both tools already use (same board, same direction, gaps under 5 minutes = one window);
then line the two lists up, calling a window "shared" when the two tools' time spans for
the same board and direction overlap (with 2.5s of slack, about one checker interval).
A window's "span" is first sighting to last sighting — a flickering opportunity counts
its whole span, so span overstates time actually in profit.

## Results

| | live-feed watcher | checker |
|---|---|---|
| opportunity windows seen | **1,121** | **361** |
| seen only by this tool | 764 | 17 |
| $ visible at best moment, total | $428.59 | $137.10 |

- **The watcher saw essentially everything the checker saw**: 344 of the checker's 361
  windows (95.3%). On those shared windows the checker was typically **1.23 seconds late**
  (median; 10th percentile 0.28s, 90th percentile 16.6s), and lateness costs money: on the
  same windows, the best-moment dollars were **$225.83 in the watcher's view vs $131.94 in
  the checker's** — by the time the checker looked, the best of it was often gone.
- **The 17 windows only the checker reported look like mirages, not watcher blindness.**
  16 of 17 were seen exactly once and gone by the next look; total claimed value $5.16.
  The checker's own built-in re-check 0.3s later found **$0 remained on every one of the
  seven largest** (up to $0.97). The likely cause: the checker's round of asking takes
  ~0.33s across many requests, so its "snapshot" can stitch together prices from slightly
  different instants and show a combination that never existed at any single moment. The
  watcher, whose view is always of one instant, saw nothing — which is the point.
- **The checker misses two-thirds of everything.** 764 windows (68% of what the watcher
  saw) never appeared in the checker's log at all. 623 of those 764 lasted **less than one
  checker interval (2.4s)** — median lifespan **0.05 seconds**. This is exactly the blind
  spot the C8 latency work predicted when it found the 2s cadence is 71–83% of the
  see→send chain.
- **What the missed windows are worth, honestly.** At their best moment they totaled
  $202.76 over the period (≈ $69/day) — but most of that lived under 0.3 seconds, too fast
  to act on even with our ~0.08s sign-and-send. Restricting to windows that lived at least
  0.7s (comfortable margin to act): **190 windows, $63.44 ≈ $21.7/day at peak; $18.9/day
  excluding the single largest one** ($8.38, Tokyo 08-06). The value is concentrated: the
  top 5 flashes are 25% of the missed total, and the median missed window is worth just
  $0.05. And peak-visible is an upper bound, not expected capture — someone must still be
  on the other side when the order lands, so the survival discounts from the Tier A/B work
  still apply. Treat $18.9–21.7/day as *additional detectable surface*, not bookable profit.
- **The rate was steady**: watcher windows per full day 403 and 409; checker 131 and 112.
  No sign the difference is a one-day artifact.

## Verdict

**Detection should be driven by the live feed when order-sending is built.** The feed sees
everything the checker sees, sees it ~1.2s sooner, sees roughly 3× more of the total
surface, and does not produce the checker's stitched-snapshot mirages. The checker's
remaining jobs: independent cross-check, the 0.3s-re-check survival data, and continuity
of the long-running log — **keep it running unchanged; the sample is the asset.** Nothing
goes live from this read; the gates in CLAUDE.md (wallet key, live dry run) are unchanged.
