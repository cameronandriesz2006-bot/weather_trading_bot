# AUDIT 2026-08-05 — negrisk_ws_feed (WS shadow detector) — adversarial verdict

Auditor: adversarial pass, instructed to refute. Method: full read of the deliverable, the reused
scanner functions, tests, fixtures, unit file and validation log; independent re-derivation of the
gate and cap math; 119-test suite re-run (passed, 1.08s, offline); ~75s read-only live WS
subscription (4 boards, 88 tokens) applying every delta under BOTH size interpretations and
diffing against one `POST /books`; sweep-log contention re-measured from `logs/negrisk_arb.jsonl`.
`negrisk-arb.service` verified **active before, during and after** every network step.

Headline: **no fabricated numbers found; the two central protocol claims reproduce on the wire;
the math claims survive independent re-derivation.** The build fails nowhere fatal, but three
small items must land before the 24h log is trusted — two of them are visible in the builder's
own 6-minute validation log.

---

## Per-target verdicts

### 1. `price_change.size` is the ABSOLUTE level size — **PASS**
- Code applies it as absolute: `TokenBook.set_level` replaces/removes the level
  (`backend/data/negrisk_ws_feed.py:264-281`), zero removes (`:269`).
- Tests pin it at both layers: `tests/test_ws_feed.py:84-90` (set_level replace) and
  `:121-129` (end-to-end via a `price_change` frame, removal-on-zero — a delta reading would
  leave the level and fail the assert).
- **Reproduced live (this audit)**: 13,642 entries applied both ways over 75s, then diffed
  against REST on every touched level: **absolute 1490 match / 4 mismatch; cumulative 1056 /
  438**. The 4 absolute mismatches are two mirrored YES/NO level pairs that moved between the
  last frame and the REST fetch (race, not semantics). Claim confirmed.

### 2. Sentinel decode + in-band gap detection — **PASS-WITH-NOTES**
- Decode is correct (`_disagrees`, `negrisk_ws_feed.py:215-233`; tests `:195-217`).
  **Reproduced live**: 943 `best_bid:"0"` + 943 `best_ask:"1"` sentinels in 75s; post-change
  top agreement **27,210 / 27,284 = 99.73%** — the residual 0.27% matches the builder's ~0.3%
  admission, and the persistent disagreements I sampled were one mirrored token pair repeating
  (a genuinely missed update), which is exactly what the 2-strike rule then resyncs.
- Failure-mode assessment: **fail-safe at the top of book** (2 consecutive strikes → REST
  resync, `:751-767`; single strike forgiven as a same-instant race, and agreement resets
  strikes). The fail-open windows that remain:
  - between a miss and the 2nd strike the wrong book is scored; on a token that then goes
    quiet, indefinitely (next reconnect / random spotcheck ~hours away at 4 boards/300s).
  - the detector and the spotcheck compare **top-of-book only**; a missed mid-ladder delta is
    invisible while tops agree, and the optimiser walks depth — so "0 drift" claims cover tops
    only. Mitigated by absolute level semantics (later updates self-correct level by level).
  - **the resyncer itself has a fail-open hole — see MUST-FIX 1.**

### 3. The cheap pre-gate is a true contrapositive — **PASS-WITH-NOTES**
Re-derived independently against the actual optimisers, not the docstring:
- `_cost_at`/prefix walk gives `cost_i(k) ≥ k·best_ask_i` (ladder ascending); fee
  `k·0.05·p(1−p) ≥ 0` for `p∈[0,1]`.
- SHORT: `net = k(Σ_{i∈S} g_i − 1)` with per-share margin `g_i ≤ 1 − best_ask_i`; `net>0` ⟹
  `Σ max(0,1−best_ask) > 1`, and inclusion needs `g_i>0` ⟹ `best_ask_i<1` ⟹ gate's `n≥2`
  matches the optimiser's `m≥2` (`negrisk_arb_scan.py:225-232`). Legs with `best_ask ≥ 1`
  can never enter the optimiser, so the gate's exclusion of them is sound.
- BUY: `net ≤ k(1 − Σ best_ask)`; empty/unseeded YES ladder ⟹ gate False AND optimiser None —
  consistent, no false negative. MIN_ORDER_SHARES and depth do not enter the necessity direction.
- The gate's want-flag suppression cannot corrupt episodes: gate-false ⟹ provably unprofitable
  ⟹ an emitted `episode_end` is correct.
- Notes: soundness assumes level prices < $1 (fee goes negative above; at avg p ≥ ~21 a
  constructed board profits while the gate blocks). Unreachable on the 0.001–0.999 tick grid,
  only via corrupt frames — `_levels` (`negrisk_ws_feed.py:193-203`) has no upper clamp
  (SHOULD-FIX 4). The 3,000-random-board property test (`test_ws_feed.py:371-404`) covers the
  reachable space and asserts non-vacuity (`hits > 20`).

### 4. `score_board` parity + cap/uncap path — **PASS-WITH-NOTES**
- The parity test compares against the **live** `S.evaluate_event` (`test_ws_feed.py:347`), not
  a copy of the copy — editing the scanner's scoring breaks the test. A monkeypatch test
  (`:353-368`) proves score_board calls the scanner's own optimisers.
- Cap window re-derived: a capped-profitable optimum `k*` is a prefix boundary, so it remains a
  candidate uncapped, and extra levels/legs only add non-negative contributions ⟹
  capped-profitable ⟹ uncapped-profitable ⟹ the logged row is always the uncapped (=sweep)
  numbers; there is **no window where a hit found capped disappears uncapped**. The reverse
  (capped-missed, uncapped-profitable) requires >32 levels summing <5 shares in one ladder —
  sub-min-order dust 32 deep, practically unreachable (NOTED, not a live risk).
- Notes: parity coverage is 4 synthetic + 1 real-book case; a short-only detect row omits
  `top_yes_ask_sum` that the sweep row carries (cosmetic field diff); the "153 live boards, 0
  gate misses" figure was not re-run here (method accepted; analytically proven above).

### 5. Coalescing and the queue — **PASS-WITH-NOTES**
- Worst-case added latency is **not 20ms**: with a globally quiet stream the processor sleeps
  up to 250ms (`q.get` timeout, `negrisk_ws_feed.py:788`) before scoring a pending dirty board.
  At the observed 1,100–1,370 frames/s the practical bound is ~20ms + rescore (mean 1.1ms,
  max 42ms observed) — the 250ms ceiling binds only when nothing at all is arriving. NOTED.
- Drop-oldest is bounded, counted (`dropped`), reported per-heartbeat, and arms a **full REST
  resync** (`:621-638`, test `:253-261`). A drop cannot leave a phantom open episode: post-resync
  every affected board is re-touched and re-scored (ends fire then), and `expire()` on the
  heartbeat closes any quiet-while-profitable episode at the 300s gap as `stale` — a late end,
  never a lost one. q_max observed 703 of 20,000.

### 6. Episode semantics vs the sweep analytics — **PASS-WITH-NOTES (diff must compensate — see MUST-FIX 2)**
- Gap rule matches exactly: `> gap_s`, 300s default on both sides
  (`negrisk_ws_feed.py:402` vs `negrisk_arb_pnl.py:105,163`); same `(slug, side)` keying, same
  sides tuple, same net-of-fee profit fields from the same optimisers with the same rounding.
  Multiple `episode_end` per `ep` is documented and observed (log rows 14–15, resumes 0→1).
- Two fragmentation asymmetries inflate ws-side episode counts in a naive group-by-`ep` diff:
  1. **Restart resets the tracker and the `ep` counter.** Proven by the builder's own log:
     `logs/negrisk_ws_detect.jsonl` rows 6 and 11 — same slug/side, **103s apart** (inside the
     300s rule = ONE episode by the sweep convention), logged as two `detect` rows, both `ep:1`.
  2. A profitable board with **zero book updates for >300s** fragments (stale end + new detect)
     while the sweep's 2s re-observation keeps one episode — and 30-minute standing mispricings
     are documented on these boards.
- The pnl replay credits row-2 survival; the ws log has detect-at-arrival + end rows only. The
  diff must compare arrival vs arrival (sweep episode's first row) — available on both sides.

### 7. Timestamp honesty — **PASS**
- `ts` = rescore wall time, `ws_recv_ts` = earliest socket arrival that dirtied the board
  (min-keeping `touch`, `negrisk_ws_feed.py:773-780`, test `:132-137`), `ex_ts` logged and never
  used as a clock (grep: only ever assigned/logged). The 22ms claim is exactly log row 6
  (`lag_ms: 22.2`, ts − ws_recv_ts = 22.2ms); second detect 7.2ms. Minor: rows in one rescore
  pass share the pass-start `now`, so lag_ms understates detect-to-write by ≤ rescore_ms.

### 8. Resource claims — **PASS-WITH-NOTES**
- **RSS units are mixed**: `rss_mb` is decimal MB (`statm·page/1e6`, `:184-190`) while
  `rss_peak_mb` is MiB (`ru_maxrss/1024`, `:1061`) — the stop row prints rss 117.9 over "peak"
  112.3, which is impossible until you convert. In the cap's units the observed peak is
  **~112 MiB against MemoryMax=150 MiB ≈ 25% headroom**, thinner than "104-110 vs 150" reads
  (SHOULD-FIX 1). Six minutes of RSS history is not a leak test; the cap is the backstop.
- At the cap: MemoryMax **kills**; with `Restart=always`, `RestartSec=10` and systemd's default
  `StartLimitIntervalSec=10s` (shorter than RestartSec, so the limiter can never trip) it is an
  **indefinite flap loop**, each lap re-enumerating Gamma, re-seeding 3,366 tokens over REST,
  rewriting the 2.3MB cache, and resetting the episode tracker (compounds target 6). MUST-FIX 3.
- Shard margin: a silent 1.5MiB cross is **survivable by design** (connect-time REST resync
  seeds books; deltas keep flowing) — refuting "resync forever" — but the margin itself is not
  monitored (no dump-byte gauge; only `book_s`≈0 / reconnects as passive tells). Acceptable for
  a shadow.
- Contention, re-measured here from the sweep log (not the builder's number): fetch_s p50
  **0.357 before, 0.426 / 0.452 during the two feed runs (+19-27%), 0.366 after** — worse than
  the claimed +13% (startup burst included; n=129/55). That shifts sweep detection stamps
  ~70-95ms later, inflating the apparent ws gain by ~0.1s. Partially offsetting: sweep `board`
  rows are stamped at fetch-end **before** the ~0.3s evaluate loop (`negrisk_arb_scan.py:554-564`),
  flattering the sweep. Net: against a multi-hundred-ms claimed gain these are ~10-20% biases in
  opposite directions — the 24h read must state both; any measured gain under ~200ms is inside
  this noise.

### 9. Service unit — **PASS-WITH-NOTES (small edits before enabling)**
`deploy/negrisk-ws.service`: WorkingDirectory/PYTHONPATH/quiet/append-logs correct; not yet
installed (`is-enabled: not-found`). Missing: `MemoryHigh` (throttle-reclaim before the kill —
e.g. `MemoryHigh=130M` under `MemoryMax=150M`), `StartLimitIntervalSec=/StartLimitBurst=` (see
MUST-FIX 3), any rotation for `logs/negrisk_ws_detect.jsonl` (estimated ~2MB/day: heartbeats
1,440/day ≈ 1.6MB + episodes/spots ~0.4MB — ~3x slower than the sweep log's 5.5MB/day, but
unbounded), runs as root implicitly, no CPUWeight/Nice on a 1-vCPU box sharing with the scanner.

### 10. Purity / independence — **PASS**
- No `backend.exec` import anywhere in `backend/data/` or the ws test (grep clean).
- Scanner's token cache read-only; feed writes only its own `logs/negrisk_ws_tokens.json`
  atomically via tmp+rename (`negrisk_ws_feed.py:511-515`); log paths disjoint; no `.env` read.
- `websockets==16.0` pinned (`requirements.txt:74`) and installed; `pip check`: "No broken
  requirements found"; httpx 0.28.1 as the scanner expects.

### 11. Test honesty — **PASS-WITH-NOTES**
- The fixtures are real captured frames (28 book + 309 price_change, 7 array frames) and the
  tests exercise the array-dump shape, sentinel decode, gap→resync transition, pending
  buffer/overflow, drop-oldest and malformed frames without mocking past them. An
  absolute-vs-delta regression is caught at both the `set_level` layer and the `apply_event`
  layer (removal-on-zero end-to-end would fail under a delta reading).
- Gaps: the 1MB frame is fixture-trimmed to 11KB, so the **size** path is untested and nothing
  asserts `max_size=None` on connect — that one-kwarg regression would 1009-close every
  connection on its own dump, live only (SHOULD-FIX 3); the resyncer replay logic is duplicated
  inline in the test (`test_ws_feed.py:229-237`) instead of invoking `resyncer()` (drift
  hazard); the REST-response-missing-token path (MUST-FIX 1) has no test; reader/processor/
  spotcheck loops untested (acknowledged async surface).

---

## MUST-FIX (before trusting the 24h diff)

1. **Resyncer resumes a stale book when REST omits the token** —
   `backend/data/negrisk_ws_feed.py:926-939`: `lb = rest.get(t); if lb is not None: snapshot`
   has no else — the token leaves `pending` and scores again on its pre-gap book, with no
   counter and no row. Reachable two ways: `fetch_books` swallows per-chunk failures silently
   (`backend/data/orderbook.py:132-139`, non-200/exception → chunk absent → up to 500 tokens),
   and empty/settled books are omitted by design. This is the only path that can put a phantom
   `detect` in the log with zero trace. Repro: mark a token for resync and let its chunk 429 —
   pending replays onto the stale ladder and the board is immediately scoreable. Fix: on
   `lb is None`, re-queue (or mark unseeded) and count it.
2. **Episode identity does not survive a restart, and `ep` ids collide** — proven in the
   builder's own validation log: `logs/negrisk_ws_detect.jsonl` rows 6 and 11 are the same
   (slug, side) 103s apart — one episode by the sweep's 300s rule — logged as two detects, both
   `ep:1`. Fix: stamp a run id (start-ts or pid) into every row
   (`negrisk_ws_feed.py:874-896`), and have the 24h diff merge ws episodes across
   restarts/stale-gaps with the same 300s rule before counting.
3. **OOM flap loop in the unit** — `deploy/negrisk-ws.service:12-17`: `Restart=always` +
   `RestartSec=10` + default `StartLimitIntervalSec=10s` (< RestartSec ⟹ limiter can never
   trip) turns a MemoryMax kill into an unbounded restart storm — Gamma + full REST re-seed +
   episode reset every lap, contaminating both logs. Fix: `MemoryHigh=130M`,
   `StartLimitIntervalSec=600`, `StartLimitBurst=5` (or `Restart=on-failure` with a burst cap).

## SHOULD-FIX

1. `rss_mb` (decimal MB) vs `rss_peak_mb` (MiB) unit mismatch — `negrisk_ws_feed.py:184-190`
   vs `:1061`; the stop row already prints rss > peak. Report both in MiB (the cap's units).
2. Rotation/cap for `logs/negrisk_ws_detect.jsonl` and `negrisk_ws_run.log` (~2MB/day, unbounded).
3. Test asserting `websockets.connect` is called with `max_size=None` (mock connect, inspect
   kwargs) — the only guard for the 1.05MB own-dump frame.
4. Clamp `p < 1.0` in `_levels` (`:193-203`) so the gate's soundness precondition is enforced
   rather than assumed of the wire.
5. Quiet-board fragmentation (target 6b): periodically re-observe boards with open episodes even
   when not dirty (e.g. each heartbeat), so a standing mispricing stays ONE episode as the sweep
   sees it — or handle it in the diff.
6. Have the diff net out the measured contention (+70-95ms on sweep fetch p50) and the sweep's
   pre-evaluation timestamping (~0.1-0.3s the other way) explicitly.
7. Exercise the real `resyncer()` in the replay test instead of an inline copy of its loop.

## NOTED
- Worst-case coalesce latency is 250ms (q.get timeout), not 20ms — binds only on a globally
  silent stream; practical bound at observed rates is ~20ms + ≤42ms rescore.
- Gap detector and spotcheck see top-of-book only; depth staleness is invisible until it
  surfaces at the touch or a reconnect (absolute level semantics self-heal level-by-level).
- A board with all YES quoted but zero NO quoted returns None (buy unscored) — inherited from
  `evaluate_event` (`negrisk_arb_scan.py:430-431`); parity preserved, not a divergence.
- Short-only detect rows omit `top_yes_ask_sum` (want-flag suppression); cosmetic.
- `best_subset_net`/`best_set_size_net` candidate filtering means `executable` is always true
  in practice (k ≥ 5 enforced in candidates) — same on both sides of the diff.
- The 0.16s Warsaw episode is one observation, as the builder says; nothing in it is falsified
  by this audit (lag, sweep blindness at 2s cadence, and fee arithmetic all check out).

---

## Verdict

**Safe to start `negrisk-ws.service` in shadow on this box tonight: YES**, conditional on the
three MUST-FIXes first (all small: ~3 lines in `resyncer`, a run-id field, 3 unit-file lines).
Without them it will still run and mostly measure — but a single OOM flap or one silent REST
chunk failure can contaminate the 24h sample invisibly, and the fix costs minutes. Purity is
clean, the scanner survived every check during this audit (`negrisk-arb` active throughout),
and steady-state cost (15% CPU, ~112 MiB, +0.07-0.10s on sweep fetch) is real but affordable.

**Single biggest risk to the 24h comparison's validity: episode-count asymmetry — restarts
reset episode identity and quiet standing mispricings fragment on the ws side (both already
visible in the 6-minute validation log), so a naive group-by-`ep` count overstates ws
detections; the diff must re-cluster ws rows under the same 300s rule across restarts before
any number is quoted.**
