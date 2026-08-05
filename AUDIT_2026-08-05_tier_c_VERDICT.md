# Adversarial audit — Tier C order path (C7/C8/C9), 2026-08-05

Auditor: Agent D (Fable), report-only. Scope per `TIER_C_BUILD_PLAN_2026-08-05.md` §D.
Ground truth for all signing/serialization claims: the installed `py-clob-client==0.34.6`
/ `py-order-utils==0.3.2` source in `venv/lib/python3.12/site-packages/`. All 79 tests
re-run individually (37 + 37 + 5, all green). `negrisk-arb.service` verified `active`
before, during and after the audit. No code was edited; this file is the only file created.

## Verdicts

| component | verdict |
|---|---|
| 1. Fillable-order paths (DryRunGuard, dry_run, probe) | **PASS-WITH-NOTES** |
| 2. Signing correctness vs installed library | **PASS** |
| 3. Fill-policy EV math vs `sizing.py` | **PASS-WITH-NOTES** (one MUST-FIX in the risk *bound*, not the EV math) |
| 4. Latency methodology (C8 report vs raw) | **PASS-WITH-NOTES** |
| 5. Test honesty | **PASS-WITH-NOTES** |
| 6. Cross-cutting (imports, deps, service) | **PASS-WITH-NOTES** |

---

## 1. Fillable-order paths — PASS-WITH-NOTES

The cardinal question: **I found no path to an unintended or unbounded fill.** What exists
is the plan-sanctioned residual: a resting GTC dry-run order (≤ $0.02 × ≤ 5 sh × ≤ $1.00
total, non-marketable at arrival) can still be **filled later by an aggressive seller
sweeping down through it** — "provably non-marketable" (order_client.py:27-43) is true at
post time only. That is exactly the "few cents of lottery tickets" worst case the plan
accepts, and FOK (`--order-type FOK`) removes even that.

Attacks tried and defeated (all verified by execution, not by reading):

- **Forged signed orders** handed straight to `post_single`: rails run on
  `decode_signed(signed)` — the wire bytes — not the OrderSpec (order_client.py:247-256).
  taker=0 (price=inf), maker=0 (price=0), negative amounts, NaN amounts, lowercase
  `"buy"`, integer side `0`, oversize taker: **every one raises** `GuardRailViolation`
  (NaN/inf fail the `price > 0` / `> cap` comparisons; wire side is the string
  `"BUY"`/`"SELL"` per `py_order_utils/model/order.py` `SignedOrder.dict()`, so any other
  form fails `buy_only`). Fail-closed throughout.
- **Float/tick edge cases**: decoded price = makerAmount/takerAmount of 6-dp integers; for
  size ≤ 2 dp and price ≤ 4 dp the product never exceeds the library's `amount` rounding
  budget (`ROUNDING_CONFIG`, builder.py), so the round-up branch only cleans float dust and
  the decoded price cannot exceed the rounded limit price. The 1e-12 epsilon at
  order_client.py:100 relaxes the $0.02 cap by ~9 orders of magnitude more than the worst
  possible float error (≤ half-ulp ≈ 2e-18 at this scale) — harmless. The 1e-9 at :131
  relaxes the $1.00 cap by a nano-dollar — harmless. check_book's `- 1e-12` (:120,:125)
  points the *strict* way.
- **Book rail**: `_guard_signed` refetches with `refresh=True` at post time
  (order_client.py:252) — the selection cache is never what the rail reads (tested,
  test_exec_client.py:315). Empty/no-bid book refuses (:115-118). The book is fetched for
  the token decoded from the signed order itself, so it cannot be "the wrong token" by
  caller error. Boundary price == ½·bid raises (tested :262).
- **guard=None**: `from_env(guard=None)` → `STRICT_DEFAULT` (:196-197, tested).
  `OrderClient(client, guard=None)` directly → AttributeError at first use, before signing
  — a crash, not a bypass.
- **Batch**: all rails run over the whole set before one byte is sent
  (order_client.py:287-291); notional accumulates across MIXED singles and batches through
  the single `notional_spent` counter, committed at *attempt* (:263-264, :294) so a
  timed-out post still consumes budget (tested :327).
- **GTC auto-cancel**: `with oc:` cancels on normal exit, exception, and
  KeyboardInterrupt (context-manager `__exit__` runs for BaseException; RuntimeError case
  tested :374). The library's transport is `httpx.Client(http2=True)` with httpx's
  **default 5.0s timeout** (verified), so a hung cancel errors loudly rather than hanging
  forever. **Not airtight**: SIGKILL/OOM/power loss leaves resting GTC orders until the
  next run's `cancel_all` (which does sweep them, being account-wide); a failed
  `cancel_all` in `__exit__` only warns (:343-345). Residual bounded at ≤ $1.00. See NOTED.
- **Latency probe's 50 real POSTs**: no auth headers (asserted per-request,
  latency_probe.py:285), owner = nil UUID, throwaway unfunded signer, **FOK** body (never
  GTC, :277), full guard incl. fresh book per probe (:270-275), notional cap enforced with
  probe-count reduction (:874-878), abort on any non-401/403 (:289-293). Even a
  hypothetically accepted probe is a far-below-market FOK from an unfunded key with no
  allowances — killed on arrival, unfillable at settlement. Measured run: 50 × HTTP 401,
  `{"error":"missing address header"}`. The abort rail necessarily fires *after* the first
  anomalous request; nothing can make it fire before.
- **`.client` attribute**: the raw ClobClient is reachable as `oc.client`, and
  `client.post_order(...)` on it would bypass every rail. That is deliberate misuse, not a
  rail failure (the rails-live-in-the-client guarantee covers every OrderClient path), but
  it is the one honest answer to "any path": a caller who reaches around the wrapper. No
  code in the repo does (grepped). NOTED.

## 2. Signing correctness — PASS

Verified against library source, then **re-derived independently**:

- **negRisk domain**: `create_order` resolves `neg_risk` per token from the server
  (client.py:517-520) and `get_contract_config(137, neg_risk=True)` selects
  `0xC5d563A36AE78145C45a50134d48A1215220f80a` (config.py). The wrapper passes no options,
  so it cannot defeat this. **Proof the goldens pin the right domain**: I rebuilt the
  golden order with the pinned salt against *both* exchange addresses. negRisk 0xC5d5…
  reproduces `GOLDEN_STRUCT_HASH` and `GOLDEN_SIGNATURE` exactly; binary 0x4bFb… produces a
  different hash. `Account._recover_hash(GOLDEN_STRUCT_HASH, GOLDEN_SIGNATURE)` recovers
  the throwaway address. The golden test is therefore not self-referential — it would catch
  a wrong-domain regression.
- **Salt pinning**: the builder's `salt_generator=generate_seed` default is bound at def
  time (order_builder.py:19-21), *and* `py_clob_client` constructs a fresh
  `UtilsOrderBuilder` per `create_order` call (builder.py:150-157), so neither patching the
  module global nor patching an instance would take. The test's wrapped-`__init__`
  (test_exec_client.py:76-85) patches the class `__init__` to force
  `salt_generator=lambda: GOLDEN_SALT` — this is the correct (and essentially only) pinning
  point, and it demonstrably works: two builds are byte-identical (:158).
- **Batch = library serialization by construction**: `post_batch` wraps
  `ClobClient.post_orders` (one `POST /orders`, body `[order_to_json(...)]`, compact
  separators, exact bytes under the L2 HMAC — client.py:592-621). The test asserts the sent
  body equals the library's own `order_to_json` output byte-for-byte (:203-209).
- **Amount rounding**: BUY maker/taker per `get_order_amounts` verified; decode is the
  exact inverse. Order type is not signed over (only in the POST body) — correctly
  documented and exploited (:5-9).
- **feeRateBps**: tests stub `base_fee: 0`, but production `create_order` always resolves
  the market's real base_fee server-side and signs with it (`__resolve_fee_rate`,
  client.py:476-490); `OrderArgs.fee_rate_bps` defaults to 0 which never trips the
  mismatch exception (only user-supplied *non-zero* mismatches raise). A non-zero base_fee
  board therefore signs correctly with no code change; the guard's $1.00 cap excludes the
  (≤ ~$0.005 at these prices) taker fee. NOTED, not a defect.
- `builder_config` is None → `can_builder_auth()` False → the builder-header branch is
  dead for us (client.py:863-864).

## 3. Fill-policy EV math — PASS-WITH-NOTES (one MUST-FIX on the risk bound)

Re-derived everything independently:

- **c(p) = p + r·p·(1−p)**, r read back out of the canonical function as `4·f(0.5)`
  (fill_policy.py:260-267) — cannot drift from `sizing.taker_fee_per_share`
  (sizing.py:82-100, r = 0.05 from settings; `.env` does **not** override
  `WEATHER_TAKER_FEE_RATE` — checked).
- **Quadratic**: c(x) = R ⇒ r·x² − (1+r)·x + R = 0; smaller root
  x* = [(1+r) − √((1+r)² − 4rR)]/(2r) — sign and root selection correct (other root > 1
  for R ≤ 1); degenerate r=0 → x=R; clip to [0,1]; runtime self-check against the
  canonical fee at :284-289. My grid over 999 budgets: max |c(x*) − R| = 1.8e-15.
- **Worked example** (docstring :58-68): every number reproduces exactly —
  paid 19.184675, B = 0.804325, R = 0.04021625, x* = 0.0383713,
  19.184675 + 20·c(0.0383713) + 0.011 = 20.000000000. The unwind figure −$3.47
  (12.85¢/set spread + 4.43¢/set round-trip fee) and hold figures (−$0.48 expected,
  −$19.18 worst) also reproduce from A2 §3 assumption 7's spread curve.
- **Budget split / no double-spend**: B_j sum to exactly B; at the ceilings the reposts
  spend exactly B (tested :153-155). I additionally proved the split is *conservative
  only*: gates-pass ⇒ total affordable holds algebraically (c(a)/a = 1 + r(1−a) is
  decreasing, so Σq·a·(c(a)/a) ≤ (B/Σw)·Σq·a = B), and 100k random draws found zero
  anti-conservative cases. The converse (affordable but a cheap leg blocked) can occur —
  conservative direction, NOTED.
- **Gas asymmetry is correct**: redeem gas charged to completion and to
  hold-a-complete-set (:410, :486-487); *not* charged to unwind (taker fills cost no gas,
  B4) nor to hold-a-broken-set-that-pays-$0 (nothing to redeem). Not an error.
- **Unwind-vs-hold default**: the file quotes A2 §4.3 *against itself* accurately and the
  three defenses check out against the report: §7.3 really does call the expected case a
  ceiling (adverse selection), §7.2 really does call break rates floors, and §4.3's "abort
  is not a fix" is a verdict on the *sequential* mode — this module runs only on §5.5's
  per-order-rejection-inside-a-batch case, which A2 explicitly could not measure. The
  policy is also not A2's "abort": it reposts at breakeven first and unwinds only when
  completion is provably loss-making. For a $250 bankroll where one held bag is 8–39% of
  bankroll (A2 §4.4 max $97.24), certain-small-loss over fat-tail is a defensible risk
  policy, and `unwind_on_break=False` is a real, honest escape hatch (:99-101, tested
  :251-264). Sound.
- **Property test** (test_fill_policy.py:433-479): the bound holds **by construction**,
  not merely on the 600 draws — repost plans are only emitted when `unwind_now_loss ≥ 0`
  (the crossed-book gate, fill_policy.py:555), which makes
  `worst_case = max(0, unwind_now) ≤ unwind_now` structural; unwind plans equal the bound;
  recovery ≥ 0 makes `unwind_now ≤ hold_worst` structural. The draws are a smoke test of
  that structure (and do exercise every branch, asserted at :479). **Under the default
  config only** — see MUST-FIX 1.
- Degenerate inputs: exhaustively validated and tested (NaN/0/negative
  prices-sizes, duplicate legs/outcomes, missing books, non-exhaustive boards, bad masks).
  `best_bid=0.0` and `best_ask` falsy are treated as absent — conservative.

**The one real defect**: `worst_case_loss` on a repost plan assumes the repost-killed
fallback is *unwind* (fill_policy.py:475-476), but under
`PolicyConfig(unwind_on_break=False)` the actual fallback is *Hold* with full-stake
downside (:499-503, via the chased branch :529-533). Demonstrated: same partial set,
`explain(..., cfg=PolicyConfig(unwind_on_break=False))` returns a RepostLeg plan with
`worst_case_loss` = $2.89 while the true worst under that config's own fallback is
$19.18 (= `hold_worst_loss`). Default config unaffected; but a risk bound that silently
understates 6.6× under a documented config is exactly what this project's audits exist to
catch. MUST-FIX 1.

## 4. Latency methodology — PASS-WITH-NOTES

- **Report ⇄ raw**: I recomputed every table row (nearest-rank, the module's own `pct`)
  from `C8_latency_raw.json` — all 9 rows match to 0.1ms. n's reconcile (47 warm + 3 cold
  = 50 = the 50 × HTTP 401; 130 network ops ✓; notional $0.250 ✓). `--from-raw` makes the
  report a pure function of the sidecar, as claimed.
- **Synthesis arithmetic**: 52.6 + 27.7 = 80.3ms → "~0.08s" ✓; 0.33+0.08 → 0.41 ✓; mean
  1.41 / worst 2.41 ✓; p95 0.47 ✓; 11×27.7 = 305ms = 43% of 0.7s ✓; signing 66% ✓;
  87.16/310.43 = 28.1% and /5.82d = $14.97/day ✓ against GOLIVE_TESTPLAN:33.
- **Is time-to-401 "a lower bound" in a useful sense?** The report's own evidence (the
  POST /order ≈ GET /time + 8ms comparison, §ref) supports its claim that the 401 returns
  before *any* order work, i.e. the wire number bounds only the network+front-door term
  and says nothing about matching. The report states this plainly and repeatedly (§b,
  §What (b) does, three Caveats bullets) and quantifies a 5× headroom scenario (0.19s,
  still inside 0.7s). The "28.1% is a lower bound for AMS" conclusion is *conditional* on
  accepted-latency staying under ~0.6s — unmeasured. The strongest sentence ("needs no
  further latency haircut for Amsterdam") slightly outruns the measurement, but it is
  hedged in the same paragraph and gated on component (d). Acceptable with notes.
- **The 5s stall**: presented as an *earlier-run* observation via the `--note` mechanism,
  with "cause is unproven" and "reproduced zero stalls in 130 operations" stated — honest
  framing. But the earlier run's raw sidecar was **overwritten** by the 04:42 run (both
  runs default to the same `raw_path`, latency_probe.py:804), so the 5016.9ms/5038.3ms
  values, the "~103 ops" denominator, the 22.3ms earlier p50, and the "DNS ~0.6ms p50 (30
  lookups)" figure are all **unverifiable free text**. Evidence destruction by default
  path — SHOULD-FIX 3.
- **n=50 / single board / single time-of-day**: the caveats section says exactly this,
  correctly notes p99 = max at this n, and the second `--note` honestly widens the wire
  p50 to 20–30ms from run-to-run variance. Not overstated.

## 5. Test honesty — PASS-WITH-NOTES

- The FakeHTTP stubs replace only the transport; signing, serialization, decoding and all
  rails are the real library and real code. The one mock-shaped test
  (`test_from_env_guard_none_falls_back_to_strict`, :353-363) still exercises the real
  `from_env` None-branch and would fail if it regressed. `test_api_creds_cached_once`
  tests real cache logic against a fake client. No test asserts only mock behavior.
- **What the mocks assume vs production**: tick 0.01 (weather boards can be 0.001 — safe:
  rounding analysis in §1; a 0.1-tick market would fail `price_valid` loudly);
  `neg_risk: True` always (production resolves per token; a False board would sign against
  the binary exchange — correct behavior, just untested); `base_fee: 0` (production signs
  the server's real fee — §2). None of these can create exposure; all can only fail closed
  or sign correctly.
- **Risky branches not covered**: (i) notional cap across MIXED single+batch calls in one
  client (singles-only :293 and batch-only :304 exist; the shared counter makes it correct
  by construction but no test crosses the cap mixed) — SHOULD-FIX 4; (ii)
  `__exit__` when `cancel_all` itself raises (:343-345) — untested; (iii) `post_batch`
  length-mismatch branch (:309-314) — untested; (iv) KeyboardInterrupt specifically
  (RuntimeError is tested; same mechanism). fill_policy degenerate inputs are covered
  exhaustively; uneven leg sizes (Q = min) are the one untested shape there.
- 79 = 37 (exec) + 37 (fill_policy) + 5 (orderbook), re-run individually, all green.

## 6. Cross-cutting — PASS-WITH-NOTES

- **`import backend.exec.fill_policy`** executes `backend/exec/__init__.py:7-15`, pulling
  `clob_auth` + `order_client`. Measured: this loads `pydantic`/`pydantic_settings` (and
  reads `.env` via `sizing`→`config`) but **not** `py_clob_client` and **not** `httpx` —
  both are function-local imports (clob_auth.py:147-148, order_client.py:235,285,350). So
  builder B's concern is real but bounded: the "pure" module drags settings machinery, not
  network-capable modules. The scanner path imports nothing from `backend.exec` (grepped
  `backend/data/`). SHOULD-FIX 5 (make the package init lazy) — cheap insurance.
- **httpx 0.26 → 0.28.1**: grepped every `httpx` use in `backend/` — only
  `AsyncClient(timeout=, headers=, limits=)`, `.get(params=)`, `.post(json=)`,
  `Client(http2=, timeout=)`, `content=` — none of the removed 0.28 APIs (`proxies=`,
  `app=`, `allow_redirects=`). **Import smoke test passed**: `import
  backend.data.negrisk_arb_scan` under httpx 0.28.1 succeeds. The running scanner
  (PID 2302, up since 08-04 11:03, predating the 08-05 04:10 install) holds 0.26 in
  memory; on crash, `Restart=always` relaunches into 0.28.1 — no import-time failure mode
  found. Requirements comment documents exactly this.
- **`.clob_creds.json`**: in `.gitignore` ✓; does not exist yet ✓; but
  `save_cached_creds` writes the L2 secret with `open(path, "w")` under the default umask
  and only *then* chmods 0600 (clob_auth.py:205-207) — a world-readable window for an
  order-placement credential. MUST-FIX 2 (trivial: `os.open(..., 0o600)` before write).
- **eth-abi 6.0.0b1**: pre-release pin forced by eth-account's `>=4.0.0-b.2` specifier;
  pinned to what was resolved and tested, with a revisit note (requirements.txt) — NOTED.
- **pytest as a runtime dep**: confirmed — `poly_eip712_structs-0.0.1` METADATA declares
  `Requires-Dist: pytest`, so pytest lands in the production venv unpinned. Documented in
  requirements.txt. NOTED (supply-chain).
- `requests==2.34.2` pin matches the installed dist. `.env` untouched;
  `SIMULATION_MODE=true` confirmed; no `WEATHER_TAKER_FEE_RATE` override.
- CLAUDE.md has **not** yet been updated for Tier C (acceptance item; main-session
  integration work, listed here so it is not lost).

---

## MUST-FIX

1. **`worst_case_loss` understates the repost plan's worst case under
   `unwind_on_break=False`** — `backend/exec/fill_policy.py:475-476` returns
   `max(0, unwind_now_loss)` for any RepostLeg plan, but that config's repost-killed
   fallback is Hold-the-residue (`:499-503` via `:529-533`), whose worst case is the full
   stake. *Repro*: `explain(spec, outcomes(killed={"t7"}), books,
   PolicyConfig(unwind_on_break=False))` on the docstring board →
   `worst_case_loss` $2.89 vs true $19.18 (`hold_worst_loss`). Fix: in the RepostLeg
   branch return `hold_worst_loss(view)` when `not view.cfg.unwind_on_break` (and cover it
   in the property test with both configs).

2. **L2 API-secret cache is briefly world-readable at creation** —
   `backend/exec/clob_auth.py:205-207` does `open(path, "w")` → `json.dump` → `chmod 0600`;
   under umask 022 the secret (an order-placement credential, cannot be re-fetched) sits
   0644 until the chmod, and permanently if the process dies mid-function. *Repro*: strace
   the write path, or simply read the three lines. Fix:
   `fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)` then
   `with os.fdopen(fd, "w") as fh: json.dump(...)`. Fix before any real key exists.

## SHOULD-FIX

3. **Latency-probe raw sidecar overwrites the previous run** —
   `backend/exec/latency_probe.py:804` derives the same default `raw_path` every run; the
   04:31 run's series (the only per-request evidence of the 5s stalls and the 22.3ms p50
   quoted in C8_latency.md §Stalls) was clobbered by the 04:42 run, leaving those figures
   unverifiable free text. Timestamp the default raw path (or refuse to overwrite), and
   persist any auxiliary series (the "30 DNS lookups") that the report quotes.
4. **No test crosses the notional cap with mixed single+batch posts on one client** —
   correct today by the shared `notional_spent` counter (order_client.py:264,294) but
   unpinned; add a test: post_single ×5 at $0.10 then `post_batch([...×6])` must raise
   with nothing sent.
5. **`backend/exec/__init__.py:7-15` eagerly imports auth/client modules** — makes
   `import backend.exec.fill_policy` read `.env` and load pydantic_settings (measured; no
   httpx/py_clob_client, so no network surface today). Lazy/`__getattr__` imports keep the
   pure module pure and shrink the scanner-contamination surface to zero.
6. **`_live_dry_run` does not `cancel_all` at startup** — orders left resting by a
   SIGKILLed previous run are only swept when a *later* run reaches its cancel step; one
   line at entry (`oc.cancel_all()`) closes the gap (dry_run.py:100-115).
7. Untested branches worth a line each: `__exit__` cancel-failure warning
   (order_client.py:343-345), `post_batch` result-count mismatch (:309-314), fill_policy
   with uneven leg sizes (Q = min binding on a fat leg).

## NOTED (accepted risks)

- **A resting GTC dry-run order can fill** if a seller sweeps through it — bounded at
  ≤ $1.00 + ≤ ~$0.005 taker fee (the fee is outside the notional cap), plan-sanctioned,
  eliminated by `--order-type FOK`. Auto-cancel is not airtight against SIGKILL/power
  loss; residual is the same bounded few cents.
- **`oc.client` exposes the raw, unguarded ClobClient** — deliberate misuse defeats the
  rails; no repo code does this. The rails-in-client guarantee covers every OrderClient
  path, which is what the plan requires.
- The book rail trusts the server to return the book for the requested token (no
  `asset_id` cross-check), and a dust bid (tiny size) is a valid ½-bid reference; both are
  second-order behind the $0.02/5-share/$1 static caps.
- Budget split proportional-to-cost can *block* a marginally-affordable completion when
  killed-leg asks are heterogeneous (never the reverse — proven §3). Conservative.
- `_short_worst_loss` floors payout at (m−1)·min-size — more conservative than the true
  Σq−max(q) floor for uneven sizes.
- Tests never exercise `neg_risk=False` or non-0.01 tick sizes; both paths are
  library-resolved server-side and fail closed or sign correctly (§5).
- C8's "no further latency haircut" conclusion is conditional on unmeasured
  accepted-latency < ~0.6s and on monotone decay inside 0.7s; both conditions are stated
  in the report itself. Component (d) remains the real test.
- eth-abi 6.0.0b1 pre-release pin; pytest as an unpinned runtime dep via
  poly-eip712-structs; pydantic `class Config` deprecation warnings. All documented.
- CLAUDE.md Tier C section pending (main-session integration step).

## Could not verify

- **The 04:31 earlier-run stall figures** (5016.9ms / 5038.3ms / 22.3ms p50 / "~103 ops" /
  "DNS ~0.6ms p50, 30 lookups") — the raw sidecar was overwritten (SHOULD-FIX 3). The
  *presentation* is honest about non-reproduction; the underlying data is gone.
- **Anything requiring a key**: authenticated POST acceptance, time-to-accepted, matching
  and cancel latency, real-board base_fee behavior end-to-end, live 401→auth→accepted
  proof. Absent by design; the code paths that would run them were audited statically.
- The requirements.txt claim that the scanner's httpx call shapes "were re-run live
  against 0.28.1 before this pin moved" — historical; I verified import + call-shape
  compatibility independently instead.
