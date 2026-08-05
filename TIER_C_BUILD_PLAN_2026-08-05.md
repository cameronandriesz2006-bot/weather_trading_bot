# Tier C build plan — order path, no money at risk (2026-08-05)

Authorized by user 2026-08-05 after the 08-03/08-04 arrival read (158 / 211 episodes — soft,
partly structural, but trailing ~$11.3/day credited ≈ ~$6/day executable, above the $5 bar).
Build order: C7 (signing + submission), C8 (real latency from AMS), C9 (partial-fill policy).
Four agents: three builders (Opus 5), one adversarial auditor (Fable 5). Main session integrates.

## Non-negotiables (inherited + new)

- `SIMULATION_MODE` stays `True`. `negrisk-arb.service` keeps running untouched — the sample is
  the asset. Nothing in `backend/exec/` may be imported by the scanner path.
- **No fillable order, ever, in this tier.** Dry-run orders are BUY-side only, priced at
  ≤ $0.02/share AND < half the current best bid (book-checked at post time), ≤ 5 shares
  (the minimum), ≤ $1.00 total notional per invocation, auto-cancelled on exit. Worst case if
  every rail fails simultaneously: a few cents of lottery tickets. Rails live in the client
  (`DryRunGuard`), not in the CLIs, so no caller can skip them.
- No Polygon private key exists in `.env` yet. Everything is built and tested offline; the two
  steps that need the key (authenticated dry-run POST, authenticated latency leg) detect its
  absence and say so plainly. `.env` is never edited by agents; `.env.example` documents the
  three new vars: `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_FUNDER_ADDRESS`,
  `POLYMARKET_SIGNATURE_TYPE` (0=EOA, 1=email/magic proxy, 2=browser-wallet proxy).
- This box has 1 vCPU / 950MB RAM. No heavy parallel test runs; never replay the full 55MB log.

## Module map

```
backend/exec/
  __init__.py
  clob_auth.py       # env loading, ClobClient construction, L2 API-cred derivation + on-disk cache
  order_client.py    # OrderClient + DryRunGuard (the only place orders are built/signed/posted)
  fill_policy.py     # C9: pure-logic partial-fill state machine (no network, no clob imports)
  dry_run.py         # C7 CLI: python -m backend.exec.dry_run --live-dry-run
  latency_probe.py   # C8 CLI: python -m backend.exec.latency_probe
tests/test_exec_client.py
tests/test_fill_policy.py
reports/tier_c/C8_latency.md   # written by the probe run
```

## Interface contract (builders code against this; A implements, C consumes)

```python
@dataclass
class OrderSpec:  token_id: str; side: Literal["BUY","SELL"]; price: float; size: float
@dataclass
class PostResult: ok: bool; order_id: str | None; status: str; error: str | None; latency_ms: float

class OrderClient:
    @classmethod
    def from_env(cls, guard: DryRunGuard | None = STRICT_DEFAULT) -> "OrderClient"
    def build_signed(self, spec: OrderSpec, order_type: str = "FOK") -> tuple[Any, float]  # (signed, sign_ms)
    def post_single(self, signed, order_type: str = "FOK") -> PostResult
    def post_batch(self, signed_list, order_type: str = "FOK") -> list[PostResult]  # ONE POST /orders
    def cancel_all(self) -> int
```

Batch semantics are fixed by the Tier A/B verdict: **11 per-order-FOK orders in one
`POST /orders`, never sequential, never abort mid-set.** Agent A verifies the installed
py-clob-client actually exposes batch posting + FOK and pins the version; if the released
client lacks the batch endpoint, keep the client for signing and post the batch with httpx
against `POST /orders` directly, matching the client's own serialization.

## Agent assignments

**A (Opus, build): deps + auth + order client.** Install and pin `py-clob-client` (new
"Tier C execution" section in requirements.txt). Read the installed source to verify FOK,
batch endpoint, `create_or_derive_api_creds`, signature types. Implement `clob_auth.py`,
`order_client.py`, `dry_run.py` per the contract. Offline tests: deterministic signing golden
test with a hardcoded throwaway key, batch payload shape vs the client's own serialization,
every guard rail violated → raises. No network in tests; no live posts at all.

**B (Opus, build): C9 fill policy.** Ground truth: `reports/tier_ab/A2_legging.md`,
`TIER_AB_RESULTS_2026-08-02.md`, test-plan C9, and the canonical fee math in
`backend/core/sizing.py` (`taker_fee_per_share`). Implement `fill_policy.py`: given the
intended set and per-order FOK results, emit ordered actions (repost missing leg at a max
price bounded by set breakeven; unwind filled legs; hold) with the EV arithmetic in the
docstring. Short side is benign by B5 (convertPositions) — encode that asymmetry. Exhaustive
unit tests. Pure logic; must not import py-clob-client.

**C (Opus, build, after A): C8 latency probe.** Measure components separately and honestly:
local build+sign time for 11 orders; unauthenticated `POST /order` time-to-reject (N=50,
≥250ms spacing — polite to prod); single-board book fetch. If a key is present, add the
authenticated leg (far-from-market post → accepted → cancel). Synthesize: see→submit estimate
vs the 0.7s shadow-decay curve → reachable fraction of the edge. Write
`reports/tier_c/C8_latency.md`, stating plainly that time-to-401 is a lower bound, not
time-to-accepted.

**D (Fable, audit, after A+B+C).** Adversarial: try to refute. (1) Any path to a fillable
order — side confusion, guard bypass, price/size edge cases. (2) EIP-712/signing use vs the
library's source. (3) Fill-policy EV math vs `sizing.py` — sign errors, fee shape, breakeven
bound. (4) Latency methodology — what each number actually measures. (5) Test honesty —
what the mocks assume. Report-only (no code edits): verdict per component + MUST-FIX list.
Main session applies fixes, re-runs tests, commits.

## Acceptance

- C7: signed order reproducible offline; dry-run CLI refuses to run without `--live-dry-run`;
  with no key it exits with the exact missing-var list. (Live 401→auth→accepted proof deferred
  until the user supplies a key.)
- C8: report exists with component p50/p95 and the reachable-edge synthesis, caveats stated.
- C9: every policy branch unit-tested; EV rule derived in-file from `taker_fee_per_share`.
- Audit MUST-FIX list is empty after fixes; full test suite green; docs + CLAUDE.md updated.
