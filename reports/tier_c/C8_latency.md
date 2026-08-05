# C8 — real end-to-end latency (Amsterdam), measured 2026-08-05

`python -m backend.exec.latency_probe --n 50` — run 2026-08-05T04:42:39+00:00 UTC against `https://clob.polymarket.com` from the Vultr AMS box, with `negrisk-arb.service` running throughout (so these are contended numbers, which is the production condition). Per-request series: `reports/tier_c/C8_latency_raw.json`; `--from-raw <path>` re-renders this report without touching the network.

Board: `highest-temperature-in-seoul-on-august-6-2026` (11 legs, each priced at its own tick). Probe leg: `…33374907` at $0.001 × 5 shares = $0.005/probe (best bid 0.133, best ask 0.136 — 133× the price).

**No authenticated request was made and no order could have been created.** The wire probe posts a well-formed signed body with NO auth headers, `owner` = nil UUID, signed by the public throwaway key `0x0123…0123` (unfunded, no allowances), at a price the full `DryRunGuard` cleared against a freshly-read book at post time. The loop aborts on any status other than 401/403. Nominal notional across the whole run: $0.250, inside the $1.00 cap; actual exposure $0.00.

## Components

All times in **milliseconds**. Warm = one reused HTTP/2 connection (HTTP/2); cold = a brand-new client per request (DNS + TCP + TLS). Headline figures are the warm series.

| component | n | p50 | p95 | p99 | min | max | mean | note |
|---|---|---|---|---|---|---|---|---|
| SIGN one order (warm cache) | 550 | 4.5 | 10.4 | 14.6 | 4.0 | 17.3 | 6.3 |  |
| SIGN full 11-leg set | 50 | 52.6 | 111.5 | 125.4 | 46.7 | 125.4 | 69.6 |  |
| L2 auth-header HMAC (local) | 200 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | ≈3µs — below this table's resolution |
| WIRE POST /order → 401 (warm) | 47 | 27.7 | 32.6 | 36.4 | 22.7 | 36.4 | 27.9 |  |
| WIRE POST /order → 401 (cold) | 3 | 26.2 | 28.6 | 28.6 | 25.5 | 28.6 | 26.7 | incl. DNS+TCP+TLS |
| BOOK 11-leg fetch_books (warm) | 47 | 29.9 | 48.5 | 77.3 | 23.0 | 77.3 | 32.0 |  |
| BOOK 11-leg fetch_books (cold) | 3 | 36.7 | 51.0 | 51.0 | 29.0 | 51.0 | 38.9 | incl. DNS+TCP+TLS |
| REF GET /time (warm) | 20 | 20.0 | 24.0 | 49.5 | 15.6 | 49.5 | 21.3 | front-door floor, no order body |
| REF TCP connect | 10 | 1.8 | 2.7 | 2.7 | 1.7 | 2.7 | 2.0 | to the CF edge, not the engine |

- **(a) SIGN** — local EIP-712 over the negRisk exchange domain, metadata cache warm. **4.5ms per order, 52.6ms for the full 11-leg set** (p95 111.5ms, max 125.4ms — the spread is CPU contention with the running scanner, not the crypto). Pure CPU: no network in the timed region. On a 1-vCPU box this is not a rounding error — see §Design.
- **(a′) metadata warm-up** — the first `create_order` on a token also does `GET /tick-size`, `/neg-risk`, `/fee-rate`. Filling all 33 for this board cost **588ms** (~53ms/leg). `build_signed`'s returned `sign_ms` includes any of these that miss, so a cold `sign_ms` is a network number wearing a CPU number's name.
- **(a″) L2 header HMAC** — the local half of the authenticated path, timed with dummy creds over a real 633-byte body: **3µs p50**. Authentication adds no meaningful local cost, so (a) is not an underestimate of the authenticated build path.
- **(b) WIRE** — **27.7ms p50 time-to-401** (p95 32.6, max 36.4), 47 warm requests excluding stalls (see §Stalls). Cold p50 26.2ms over 3 requests — not distinguishable from warm at n=3. With the TLS endpoint ~2ms away the handshake is small against the ~28ms round trip, so connection reuse is worth having but is not where the time goes. Server said: '{"error":"missing address header"}'; 50 × HTTP 401 — every single request rejected, as designed.
- **(c) BOOK** — `fetch_books` over 11 tokens = one `POST /books`, **29.9ms p50** warm (11/11 books returned every time).
- **(d) AUTH** — **SKIPPED: no key — missing POLYMARKET_PRIVATE_KEY, POLYMARKET_FUNDER_ADDRESS, POLYMARKET_SIGNATURE_TYPE. The authenticated leg needs all of: POLYMARKET_PRIVATE_KEY, POLYMARKET_FUNDER_ADDRESS, POLYMARKET_SIGNATURE_TYPE in .env (see .env.example; signature type 0=EOA, 1=email/magic proxy, 2=browser-wallet proxy). Until then, time-to-accepted, matching latency and cancel latency are unmeasured.**
- **(ref)** `GET /time` p50 20.0ms vs raw TCP connect 1.8ms. Read this pair carefully — it corrects a premise. **The ~2ms in CLAUDE.md is the distance to the Cloudflare edge in Amsterdam, not to the CLOB.** The cheapest real endpoint on the platform still costs 20.0ms and `POST /order` costs 27.7ms — +8ms. Two consequences: (i) an API round trip from here is ~10× the edge RTT, and the residual is consistent with the edge→origin hop (B6: origin is AWS eu-west-2, London) rather than anything we control; (ii) because the order front door costs essentially the same as a timestamp lookup, the 401 is returned before any order work happens — which is precisely why (b) is a floor and not an estimate.

## What (b) does and does not measure

**Time-to-401 is a LOWER BOUND on time-to-accepted.** A 401 is returned by the front door before the parts of order handling that cost real time: signature recovery, balance/allowance checks, tick/price validation, the book lock, and matching. An accepted FOK additionally has to be *matched or killed* before it answers. Nothing here measures any of that, and no amount of unauthenticated probing can. Treat 28ms as the floor of the wire term and the true accept latency as unknown-but-larger until a key exists (component d).

## Stalls

None in this run: none of the 130 network operations exceeded 1000ms.

**An earlier run of this same probe, same day (~04:31 UTC), same board and leg, n=50, did stall twice**: one warm `POST /order` at **5016.9ms** (1 of 47) and one cold `POST /books` at **5038.3ms** (1 of 3) — 2 of ~103 network operations, ~2%. Both are ~5.0s, exactly the glibc resolver's default per-nameserver timeout (`/etc/resolv.conf` on this box sets no `options timeout`), and both landed on operations that had to establish a *new* connection — one deliberately-cold client, and one warm-loop request whose pooled HTTP/2 connection must have been re-dialled. Measured DNS is otherwise ~0.6ms p50 (30 lookups). Leading explanation: a single lost UDP query costing a full resolver timeout, not CLOB slowness — but the cause is unproven, and the run above reproduced zero stalls in 130 operations. Call it O(1%) of submissions, not a headline number.

That earlier run is also the honest width of a single-run p50: it measured WIRE warm p50 **22.3ms** against **27.7ms** here, eleven minutes apart, same leg — a 24% difference between two n=47 samples. Treat the wire p50 as ~20–30ms, not as 27.7.

Small, but not ignorable: **a 5s stall on an order submission is a dead set**, arriving ~7× past the 0.7s decay window, and it is a property of this box's connection setup rather than of the CLOB. Two cheap mitigations, both outside this probe's scope: pin a local caching resolver (or `options timeout:1 attempts:2` in `/etc/resolv.conf`), and give the production order client an explicit request timeout well under the window so a stalled submit *fails* instead of arriving late into a book that has moved.

## See → submit

Two clocks, and conflating them is the classic error here. The shadow probe's **0.7s** is measured from `ts` — the moment a sweep *scored* a board, stamped after its fetch — not from when the price appeared.

| clock | terms | estimate |
|---|---|---|
| **scored → our bytes at the front door** | sign 53ms + wire 28ms | **~0.08s** |
| price on book → submit, best case | fetch 0.33s + sign + wire | ~0.41s |
| price on book → submit, mean | ½ × 2s sweep + 0.41s | ~1.41s |
| price on book → submit, worst | 2s sweep + 0.41s | ~2.41s |

The fetch term is the documented 0.33s full-universe sweep fetch for this box (CLAUDE.md, 2026-08-04); the single-board 30ms measured above is the cost of a *re-check* before submit, not of the sweep itself. p95 of the reaction leg is ~0.47s. None of these include time-to-accepted, which is unmeasured.

## Against the 0.7s shadow-decay read

Documented (GOLIVE_TESTPLAN_2026-08-02.md): **$87.16 of $310.43 of arrival value survived 0.7s** over 5.82 days = **28.1%**, $14.97/day — the figure that agreed within ~20% with the $18.34/day row-2 replay crediting and is what made that crediting credible.

Our order path reaches the front door **~0.08s after scoring** — about 11% of the shadow probe's 0.7s window, and that is with the wire term being a lower bound. Even if time-to-accepted were 5× the measured time-to-401, the scored→submit leg would be ~0.19s, still inside 0.7s.

**Reachable fraction.** On the reaction leg the order path is not the binding constraint: at ~0.08s scored→submit we land well inside the 0.7s at which 28.1% of arrival value was still there, so **the 28.1% / $14.97-per-day survival read is a lower bound on what this box's order path can reach, not a ceiling it misses** — the ~$9.78/day Tier-A/B executable figure needs no further latency haircut for Amsterdam, and a latency-shaped kill is not supported by this measurement. What we cannot claim is *more* than 28.1%: survival was never measured between 0s and 0.7s, so the ~0.55s of headroom we appear to have buys an unmeasured — probably positive, definitely unquantified — amount. **The binding term is detection, not submission**: the 2s sweep cadence contributes ~1.0s mean and up to 2s, i.e. 71–83% of the whole chain, against 6% for sign+wire combined. Latency effort belongs on the WebSocket market feed (B6: exists, ~117 msg/s for one board), not on the order path.

## Design: does this change the batch-FOK case?

**It strengthens it, and it adds two hazards that were not on the list.**

1. **Sequential posting is now measurably worse, not just modelled worse.** Eleven sequential `POST /order` calls cost 11 × 28ms ≈ 0.30s of wire *at the 401 floor* — on its own 43% of the 0.7s window, before any matching time, with leg 11 landing ~0.28s after leg 1. The batch pays 28ms once. A2's 13.6% broken-set rate was derived at modelled latencies; this is the same verdict measured on the wire.
2. **Signing, not the network, is the biggest term we control.** 53ms to sign 11 legs vs 28ms to post them — signing is ~66% of the scored→submit path on this 1-vCPU box, and its p95 (112ms) is more than double its p50 because it competes with the scanner for the single core. It is sequential and single-threaded today. Pre-signing the current set between sweeps, or signing the legs off the hot path, is worth more than any network tuning available here. Note the interaction with FOK: the order **type is not signed over** (it rides in the POST body), so a pre-signed set can still choose FOK at submit — but price and size *are* signed, so pre-signing only helps where the intended prices are stable across a sweep.
3. **NEW HAZARD — the tick-size cache expires.** `ClobClient.get_tick_size` caches per token with a **300s TTL** (`tick_size_ttl`, a constructor argument); neg-risk and fee-rate are cached for the life of the process. Cold, the three metadata GETs cost ~53ms/leg (**588ms for a whole board**). So every 5 minutes the first set signed on a given board silently pays roughly that again, *inside* `build_signed` and counted as `sign_ms` — which alone would blow the 0.7s window. Mitigations in order of preference: construct the ClobClient with a large `tick_size_ttl` and refresh deliberately; or keep a warm-up loop touching `get_tick_size` for the live token set every <300s. A fresh `OrderClient.from_env()` per opportunity is the worst possible shape and must not be the production one.
4. **NEW HAZARD — connection-setup stalls, and why they argue for FOK.** The ~5s stalls in §Stalls hit connection establishment, not the CLOB. A GTC leg that arrives 5s late **rests on the book at a price the market has moved past**; an FOK leg that arrives 5s late is killed and costs nothing but the missed set. That is a second, independent argument for per-order FOK on top of A2's — and an argument for an explicit sub-second client timeout, so a stalled submit fails instead of arriving into a dead book.
5. **Batch size is not a latency problem.** One `POST /orders` carries all 11 (633B per order, ~6KB for the set) — trivial on a path whose cost is the edge→origin hop, not the payload.

## Caveats — every one of them

- **Time-to-401 is a lower bound.** The wire number excludes signature recovery, balance/allowance checks, book lock and matching. Time-to-**accepted** is unmeasured.
- **No fills were measured.** Nothing here says an order would fill, only when its bytes arrive. Fill probability is A2/D11 territory.
- **Matching latency is unmeasured**, and for FOK the kill/fill decision happens inside the request. The accept path could be several times the reject path.
- **Component (d) is skipped.** Until a Polygon key exists, accepted-latency, matching-latency and cancel-latency cannot be measured at all.
- **Single-day, single-board, n=50 sample**, one time of day (04:42 UTC), one board (`highest-temperature-in-seoul-on-august-6-2026`), one probe leg. At n=47 the reported p99 IS the max (nearest-rank, no interpolation) — it is not a tail estimate. Nothing here says the p50 is the same at 14:00 UTC.
- **Server-side queueing at real order rates is unknown.** These probes are one at a time, 250ms apart, from an account with no orders and no book presence. A live 11-order batch against a busy matching engine, competing with other bots on the same board, is a different load and may be a different latency.
- **The scanner was running throughout** — realistic contention, and the reason the SIGN p95 is double its p50, but it also means these numbers carry whatever CPU the sweep happened to be using.
- **No rate limiting and no Cloudflare interference was observed** at this rate (50 POSTs at 250ms spacing; 50 × HTTP 401; no 429, no challenge page). That is not evidence there is none at 40 orders/s (B6's documented per-signer limit).
- The 0.7s survival figure is itself a 5.82-day measurement from the **Singapore** vantage, and the shadow probe's own delay distribution was shaped by 171ms RTT. From Amsterdam the probe would re-read sooner, so re-running the shadow report from this box would measure decay on a slightly different clock than the one being compared against here.
- The probe prices each leg at the market's **minimum tick**, far below any real order. Body size and therefore wire time for a production-priced order are effectively identical, but no claim is made about server-side handling of a marketable price, which we have never sent.

Reproduce: `python -m backend.exec.latency_probe --n 50` (re-probes), or `--from-raw reports/tier_c/C8_latency_raw.json` (re-renders this file offline).
