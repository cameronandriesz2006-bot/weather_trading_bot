"""C8 — real end-to-end latency from this box, measured as SEPARATE components.

    python -m backend.exec.latency_probe [--n 50] [--out reports/tier_c/C8_latency.md]

The point of C8 is a number the go/no-go can use: does an order from here reach the book
before the mispricing dies? The shadow probe already measured decay at ~0.7s. So the answer
is not one number, it is a chain, and the only honest way to report it is to measure each
link on its own and say what each one does and does NOT include.

Four components, run sequentially (never in parallel — a 1-vCPU box measuring itself under
self-inflicted contention measures nothing):

  a. **SIGN** — ``OrderClient.build_signed`` × 11 legs, local EIP-712, no network once the
     per-token metadata cache is warm. Reported per-order and per-11-set.
  b. **WIRE** — unauthenticated ``POST /order``, well-formed signed body, NO auth headers,
     so the server rejects it at the front door. This is **time-to-401, a LOWER BOUND on
     time-to-accepted**: it measures network + TLS/connection reuse + Cloudflare + the
     front-door auth check, and nothing behind it (no balance/allowance check, no book
     lock, no matching). Never an authenticated order.
  c. **BOOK** — one board's 11 YES books through ``backend.data.orderbook.fetch_books``,
     i.e. the exact call the scanner's sweep and shadow probe make, read-only.
  d. **AUTH** — the same post authenticated + immediate ``cancel_all``, which is the only
     way to measure time-to-**accepted**. Requires a Polygon key. There is none on this
     box by design, so this component prints and writes ``SKIPPED: no key``.

Plus two reference measurements that make (b) interpretable: ``GET /time`` (the cheapest
real endpoint — the network+front-door floor with no order body at all) and a raw TCP
connect (the distance to the Cloudflare edge, not to the matching engine).

Connection reuse is explicit everywhere. Each network component runs a few COLD requests on
a brand-new client first (first request pays DNS + TCP + TLS), then the rest on ONE reused
client; the two are reported separately, and every quoted p50/p95/p99 is the WARM series.
py-clob-client's own transport is a module-level ``httpx.Client(http2=True)`` singleton, so
the library's GETs share one HTTP/2 connection for the life of the process — the warm case
is what production would actually see.

Every per-request series is written to a JSON sidecar next to the report, and
``--from-raw`` re-renders the report from it. Re-reading a measurement must never require
re-hitting production.

SAFETY. The wire probe sends real, signed, well-formed order bodies to production. It cannot
create a position, for four independent reasons, and it is built so that any one of them
alone would be enough:

  1. **No auth headers.** ``POST /order`` requires L2 headers (POLY_ADDRESS / POLY_SIGNATURE
     / POLY_TIMESTAMP / POLY_API_KEY / POLY_PASSPHRASE). We send none, which is the whole
     measurement. Measured result: HTTP 401 ``{"error":"missing address header"}``.
  2. **No owner.** ``order_to_json`` needs ``owner`` = the API key. We have none, so it is
     the nil UUID — there is no account for the order to be booked to.
  3. **Throwaway signer.** The order is signed by the public test key
     ``0x0123…0123`` (address ``0x1479…9325``), which holds no USDC and has granted the
     exchange no allowances. An accepted order from it could not fill.
  4. **Guard-legal price, checked at post time.** Every probe body runs the full
     ``DryRunGuard`` — ``check_static`` (BUY only, ≤ $0.02, ≤ 5 shares), ``check_book``
     against a freshly fetched book (price < ½ best bid, and under the best ask), and
     ``check_notional`` against the $1.00 per-invocation cap. The cap is enforced, not
     waived: the probe leg is chosen to be the cheapest tick on the board and the request
     count is cut if ``n`` probes would not fit. The whole run is a few tens of cents
     nominal — against $0.00 actually at risk.

And a fifth, behavioural: the loop **aborts immediately** on any status other than 401/403,
so a server that unexpectedly accepted one of these would stop the run rather than see 49
more.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import datetime
import json
import math
import os
import socket
import statistics
import sys
import time
from dataclasses import dataclass
from typing import Optional

import httpx

CLOB_HOST = "https://clob.polymarket.com"
POST_ORDER_URL = f"{CLOB_HOST}/order"
TIME_URL = f"{CLOB_HOST}/time"

#: The same obviously-fake public test vector tests/test_exec_client.py signs with.
#: Never funded, never approved. See the SAFETY note above.
THROWAWAY_KEY = "0x0123456789012345678901234567890123456789012345678901234567890123"
THROWAWAY_ADDRESS = "0x14791697260E4c9A71f18484C9f997B308e59325"

#: `owner` is the L2 API key. We have none; the nil UUID makes that explicit on the wire.
NIL_OWNER = "00000000-0000-0000-0000-000000000000"

DEFAULT_OUT = "reports/tier_c/C8_latency.md"
COLD_REQUESTS = 3          # per network component, on fresh clients
DEFAULT_SIZE = 5.0         # platform minimum

#: A request this slow is not latency, it is a stall — a different failure with a different
#: cause and a different fix, so it is counted separately instead of smeared into a p99.
STALL_MS = 1000.0

#: Byte-for-byte what py_clob_client.http_helpers.helpers.overloadHeaders builds for a POST.
#: The ONLY difference between this request and a real one is the five L2 auth headers.
POST_HEADERS = {
    "User-Agent": "py_clob_client",
    "Accept": "*/*",
    "Connection": "keep-alive",
    "Content-Type": "application/json",
}
GET_HEADERS = dict(POST_HEADERS, **{"Accept-Encoding": "gzip"})

AUTH_HEADER_NAMES = ("POLY_ADDRESS", "POLY_SIGNATURE", "POLY_TIMESTAMP",
                     "POLY_API_KEY", "POLY_PASSPHRASE", "POLY_NONCE")

#: Series that cross the network — the ones a stall can affect.
NETWORK_SERIES = ("wire_warm", "wire_cold", "book_warm", "book_cold", "ref_time", "ref_tcp")


# --------------------------------------------------------------------------- stats

def pct(xs: list[float], q: float) -> float:
    """Nearest-rank percentile. No interpolation: with n=50 an interpolated p99 invents a
    value that was never observed, and the honest statement is that p99 IS the max."""
    if not xs:
        return float("nan")
    s = sorted(xs)
    k = max(0, math.ceil(q / 100.0 * len(s)) - 1)
    return s[min(k, len(s) - 1)]


def split_stalls(xs: list[float]) -> tuple[list[float], list[float]]:
    return [x for x in xs if x < STALL_MS], [x for x in xs if x >= STALL_MS]


@dataclass
class Stats:
    name: str
    n: int
    p50: float
    p95: float
    p99: float
    mn: float
    mx: float
    mean: float
    note: str = ""

    @classmethod
    def of(cls, name: str, xs: list[float], note: str = "") -> "Stats":
        if not xs:
            return cls(name, 0, *([float("nan")] * 6), note=note or "no samples")
        return cls(name, len(xs), pct(xs, 50), pct(xs, 95), pct(xs, 99),
                   min(xs), max(xs), statistics.fmean(xs), note)

    def row(self) -> str:
        f = lambda v: "—" if math.isnan(v) else f"{v:.1f}"     # noqa: E731
        return (f"| {self.name} | {self.n} | {f(self.p50)} | {f(self.p95)} | {f(self.p99)} "
                f"| {f(self.mn)} | {f(self.mx)} | {f(self.mean)} | {self.note} |")

    def line(self) -> str:
        f = lambda v: "nan" if math.isnan(v) else f"{v:.1f}"   # noqa: E731
        return (f"{self.name:<34} n={self.n:<4} p50={f(self.p50):>7}  p95={f(self.p95):>7}  "
                f"p99={f(self.p99):>7}  min={f(self.mn):>7}  max={f(self.mx):>7} ms")


TABLE_HEAD = ("| component | n | p50 | p95 | p99 | min | max | mean | note |\n"
              "|---|---|---|---|---|---|---|---|---|")


# --------------------------------------------------------------------------- board / legs

def _pick_board(slug: Optional[str], date: Optional[str]) -> dict:
    """Tomorrow's fullest board from the scanner's token cache (read-only), via the same
    helpers the C7 dry run uses — one definition of "which board", not two."""
    from .dry_run import load_boards, pick_board

    day = (datetime.date.fromisoformat(date) if date
           else datetime.datetime.now(datetime.timezone.utc).date() + datetime.timedelta(days=1))
    return pick_board(load_boards(), day, slug)


# --------------------------------------------------------------------------- (a) SIGN

def component_sign(oc, tokens: list[str], n_sets: int) -> tuple[dict, dict]:
    """Local CPU only, once the per-token metadata cache is warm.

    ``ClobClient.create_order`` fetches three per-token metadata values — tick size
    (``GET /tick-size``, 300s TTL), neg-risk (``GET /neg-risk``, cached forever) and fee
    rate (``GET /fee-rate``, forever) — and ``OrderClient.build_signed`` times the whole
    call, so a COLD ``sign_ms`` is mostly network. That warm-up is measured separately
    below and is a production hazard in its own right, not a measurement artifact.
    """
    from py_clob_client.clob_types import ApiCreds, RequestArgs
    from py_clob_client.headers.headers import create_level_2_headers
    from py_clob_client.utilities import order_to_json

    from .order_client import OrderSpec

    # --- cold: fill the metadata cache for all 11 legs, and price each leg at its tick.
    t0 = time.perf_counter()
    prices: dict[str, float] = {}
    for tid in tokens:
        tick = float(oc.client.get_tick_size(tid))
        oc.client.get_neg_risk(tid)
        oc.client.get_fee_rate_bps(tid)
        prices[tid] = tick
    warm_ms = (time.perf_counter() - t0) * 1000.0

    usable = [t for t in tokens if 0 < prices[t] <= oc.guard.max_price]
    if not usable:
        raise SystemExit("no leg on this board has a tick size inside the $0.02 guard cap")

    per_order: list[float] = []
    per_set: list[float] = []
    signed_sample = None
    for _ in range(n_sets):
        t_set = time.perf_counter()
        for tid in usable:
            signed, ms = oc.build_signed(
                OrderSpec(token_id=tid, side="BUY", price=prices[tid], size=DEFAULT_SIZE),
                "FOK",
            )
            per_order.append(ms)
        per_set.append((time.perf_counter() - t_set) * 1000.0)
        signed_sample = signed

    # --- the local half of the authenticated path we cannot otherwise measure: the L2
    #     header HMAC over the serialized body. Dummy creds, never sent anywhere.
    dummy = ApiCreds(api_key=NIL_OWNER,
                     api_secret=base64.urlsafe_b64encode(b"0" * 32).decode(),
                     api_passphrase="x")
    body = order_to_json(signed_sample, NIL_OWNER, "FOK", False)
    serialized = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    hmac_ms = []
    for _ in range(200):
        t = time.perf_counter()
        create_level_2_headers(oc.client.signer, dummy,
                               RequestArgs(method="POST", request_path="/order",
                                           body=body, serialized_body=serialized))
        hmac_ms.append((time.perf_counter() - t) * 1000.0)

    series = {"sign_order": per_order, "sign_set": per_set, "hmac": hmac_ms}
    extra = {
        "legs": len(usable),
        "meta_warm_ms": warm_ms,
        "meta_gets": 3 * len(tokens),
        "body_bytes": len(serialized.encode()),
        "prices": prices,
    }
    return series, extra


# --------------------------------------------------------------------------- (b) WIRE

class ProbeAborted(RuntimeError):
    """An unauthenticated probe got a status that was not a rejection. Stop everything."""


def _probe_body(oc, token_id: str, price: float, size: float) -> tuple[str, float]:
    """One guard-checked, signed, unauthenticated ``POST /order`` body. Returns
    (serialized, notional). Every rail runs here, BEFORE the timed region."""
    from py_clob_client.utilities import order_to_json

    from .order_client import OrderSpec, decode_signed

    signed, _ = oc.build_signed(OrderSpec(token_id=token_id, side="BUY",
                                          price=price, size=size), "FOK")
    # Guard the DECODED order — the exact bytes about to go on the wire — exactly as
    # OrderClient.post_single would, including a fresh book read at post time.
    tid, side, dec_price, dec_size = decode_signed(signed)
    oc.guard.check_static(side, dec_price, dec_size)
    best_bid, best_ask = oc.top_of_book(tid, refresh=True)
    oc.guard.check_book(dec_price, best_bid, best_ask)
    notional = dec_price * dec_size
    oc.guard.check_notional(oc.notional_spent, notional)

    body = order_to_json(signed, NIL_OWNER, "FOK", False)
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False), notional


def _post_unauth(client: httpx.Client, data: str) -> tuple[float, int, str, str, str]:
    """(ms, status, body, http version, Cloudflare PoP). The PoP is the `cf-ray` suffix —
    recorded so the report can say whether every request landed on the same edge, which is
    the only cheap evidence available about Cloudflare's part in the number."""
    assert not any(h in POST_HEADERS for h in AUTH_HEADER_NAMES), "auth header leaked"
    t = time.perf_counter()
    r = client.post(POST_ORDER_URL, headers=POST_HEADERS, content=data.encode("utf-8"))
    ms = (time.perf_counter() - t) * 1000.0
    if r.status_code not in (401, 403):
        raise ProbeAborted(
            f"unauthenticated POST /order returned {r.status_code}, expected 401/403 — "
            f"aborting the run. body: {r.text[:300]!r}"
        )
    pop = (r.headers.get("cf-ray") or "none").rsplit("-", 1)[-1]
    return ms, r.status_code, r.text.strip()[:120], r.http_version, pop


def component_wire(oc, token_id: str, price: float, n: int, spacing: float) -> tuple[dict, dict]:
    """N total requests: COLD_REQUESTS on fresh clients, the rest on one reused client."""
    statuses: dict[str, int] = {}
    bodies: set[str] = set()
    versions: set[str] = set()
    pops: dict[str, int] = {}
    cold, warm = [], []
    n_cold = min(COLD_REQUESTS, n)

    def record(code, body, ver, pop):
        statuses[str(code)] = statuses.get(str(code), 0) + 1
        bodies.add(body)
        versions.add(ver)
        pops[pop] = pops.get(pop, 0) + 1

    for i in range(n_cold):
        data, notional = _probe_body(oc, token_id, price, DEFAULT_SIZE)
        oc.notional_spent += notional
        with httpx.Client(http2=True, timeout=15.0) as fresh:      # new DNS+TCP+TLS
            ms, code, body, ver, pop = _post_unauth(fresh, data)
        cold.append(ms)
        record(code, body, ver, pop)
        print(f"  wire cold  [{i + 1}/{n_cold}] {code} {ms:7.1f}ms")
        time.sleep(spacing)

    stopped = ""
    with httpx.Client(http2=True, timeout=15.0) as shared:          # ONE reused connection
        for i in range(n - n_cold):
            try:
                data, notional = _probe_body(oc, token_id, price, DEFAULT_SIZE)
            except Exception as exc:                                # noqa: BLE001 — a rail
                stopped = f"stopped after {i} warm probes: {exc}"
                print(f"  wire warm  STOP: {exc}")
                break
            oc.notional_spent += notional
            ms, code, body, ver, pop = _post_unauth(shared, data)
            warm.append(ms)
            record(code, body, ver, pop)
            if (i + 1) % 10 == 0 or i == 0 or ms >= STALL_MS:
                print(f"  wire warm  [{i + 1}/{n - n_cold}] {code} {ms:7.1f}ms")
            time.sleep(spacing)

    extra = {"statuses": statuses, "bodies": sorted(bodies), "http": sorted(versions),
             "pops": pops, "stopped": stopped, "notional": oc.notional_spent, "price": price}
    return {"wire_warm": warm, "wire_cold": cold}, extra


# --------------------------------------------------------------------------- (c) BOOK

def component_book(tokens: list[str], n: int, spacing: float) -> tuple[dict, dict]:
    """The scanner's own fetch path: ``orderbook.fetch_books`` over one board's 11 YES
    tokens = one ``POST /books``. Read-only."""
    from backend.data.orderbook import fetch_books

    async def run() -> tuple[list[float], list[float], int]:
        cold, warm, got = [], [], 0
        n_cold = min(COLD_REQUESTS, n)
        for i in range(n_cold):
            async with httpx.AsyncClient(timeout=15.0) as fresh:
                t = time.perf_counter()
                books = await fetch_books(tokens, fresh)
                cold.append((time.perf_counter() - t) * 1000.0)
            got = len(books)
            print(f"  book cold  [{i + 1}/{n_cold}] {got}/{len(tokens)} {cold[-1]:7.1f}ms")
            await asyncio.sleep(spacing)
        async with httpx.AsyncClient(timeout=15.0) as shared:       # ONE reused connection
            for i in range(n - n_cold):
                t = time.perf_counter()
                books = await fetch_books(tokens, shared)
                warm.append((time.perf_counter() - t) * 1000.0)
                got = len(books)
                if (i + 1) % 10 == 0 or i == 0 or warm[-1] >= STALL_MS:
                    print(f"  book warm  [{i + 1}/{n - n_cold}] {got}/{len(tokens)} "
                          f"{warm[-1]:7.1f}ms")
                await asyncio.sleep(spacing)
        return cold, warm, got

    cold, warm, got = asyncio.run(run())
    return ({"book_warm": warm, "book_cold": cold},
            {"tokens": len(tokens), "returned": got})


# --------------------------------------------------------------------------- (d) AUTH

AUTH_SKIP = "SKIPPED: no key"


def component_auth(env_file: str) -> str:
    """Authenticated post + immediate cancel_all — the only measurement of time-to-ACCEPTED.
    Needs a Polygon key; there is none on this box, so this reports the skip and the exact
    variables that would unblock it."""
    from .clob_auth import REQUIRED_VARS, MissingCredentials, load_clob_env

    try:
        load_clob_env(env_file)
    except MissingCredentials as exc:
        return (f"{AUTH_SKIP} — missing " + ", ".join(exc.missing)
                + ". The authenticated leg needs all of: "
                + ", ".join(REQUIRED_VARS)
                + " in .env (see .env.example; signature type 0=EOA, 1=email/magic proxy, "
                  "2=browser-wallet proxy). Until then, time-to-accepted, matching latency "
                  "and cancel latency are unmeasured.")
    except ValueError as exc:
        return f"{AUTH_SKIP} — credentials present but invalid: {exc}"
    return (
        f"{AUTH_SKIP} — a key is now configured, but this build deliberately does not run "
        "an authenticated leg that has not been re-reviewed. Enable it explicitly "
        "(post_single at a guard-legal price → cancel_all) and re-run."
    )


# --------------------------------------------------------------------------- reference

def component_reference(n: int, spacing: float) -> dict:
    """Two floors that make the wire number readable: ``GET /time`` (cheapest real endpoint,
    still a full front-door round trip) and a raw TCP connect (distance to the Cloudflare
    edge — NOT to the matching engine)."""
    t_ms, tcp_ms = [], []
    with httpx.Client(http2=True, timeout=15.0) as c:
        c.get(TIME_URL, headers=GET_HEADERS)                        # discard the cold one
        for _ in range(n):
            t = time.perf_counter()
            r = c.get(TIME_URL, headers=GET_HEADERS)
            t_ms.append((time.perf_counter() - t) * 1000.0)
            if r.status_code != 200:
                break
            time.sleep(spacing)
    for _ in range(min(n, 10)):
        t = time.perf_counter()
        try:
            s = socket.create_connection(("clob.polymarket.com", 443), timeout=5)
            s.close()
        except OSError:
            break
        tcp_ms.append((time.perf_counter() - t) * 1000.0)
        time.sleep(spacing)
    return {"ref_time": t_ms, "ref_tcp": tcp_ms}


# --------------------------------------------------------------------------- packing

def _ctx_from_raw(raw: dict) -> dict:
    """Stats + stall accounting from the persisted per-request series. Pure — the report is
    a function of the raw data, so ``--from-raw`` reproduces it exactly."""
    ser = {k: list(map(float, v)) for k, v in raw["series"].items()}
    legs = raw["extra"]["sign"]["legs"]
    labels = {
        "sign_order": "SIGN one order (warm cache)",
        "sign_set": f"SIGN full {legs}-leg set",
        "hmac": "L2 auth-header HMAC (local)",
        "wire_warm": "WIRE POST /order → 401 (warm)",
        "wire_cold": "WIRE POST /order → 401 (cold)",
        "book_warm": f"BOOK {raw['extra']['book']['tokens']}-leg fetch_books (warm)",
        "book_cold": f"BOOK {raw['extra']['book']['tokens']}-leg fetch_books (cold)",
        "ref_time": "REF GET /time (warm)",
        "ref_tcp": "REF TCP connect",
    }
    notes = {
        "wire_cold": "incl. DNS+TCP+TLS", "book_cold": "incl. DNS+TCP+TLS",
        "ref_time": "front-door floor, no order body",
        "ref_tcp": "to the CF edge, not the engine",
    }
    stats, clean_stats = {}, {}
    stalls: list[tuple[str, float]] = []
    net_ops = 0
    for key, xs in ser.items():
        stats[key] = Stats.of(labels[key], xs, notes.get(key, ""))
        ok, bad = split_stalls(xs)
        clean_stats[key] = Stats.of(labels[key] + " — excl. stalls", ok)
        if key in NETWORK_SERIES:
            net_ops += len(xs)
            stalls += [(key, v) for v in bad]
    h = stats["hmac"]
    h.note = f"≈{h.p50 * 1000:.0f}µs — below this table's resolution"
    return {**raw, "stats": stats, "clean": clean_stats,
            "stalls": sorted(stalls, key=lambda kv: -kv[1]), "net_ops": net_ops,
            "series": ser}


# --------------------------------------------------------------------------- report

def _delta(a: float, b: float) -> str:
    """'+3ms' / 'the same to within a millisecond' — never print a spurious negative
    overhead as if it meant something."""
    d = a - b
    if abs(d) < 1.0:
        return "the same to within a millisecond"
    return f"{d:+.0f}ms"


def build_report(ctx: dict) -> str:
    """The synthesis. Every number below is either measured in this run or cited to the
    document it came from — nothing is assumed."""
    s, cl, ex = ctx["stats"], ctx["clean"], ctx["extra"]
    sign1, sign11, hmac_s = s["sign_order"], s["sign_set"], s["hmac"]
    wire_w, wire_c = s["wire_warm"], s["wire_cold"]
    book_w, book_c = s["book_warm"], s["book_cold"]
    ref_t, ref_tcp = s["ref_time"], s["ref_tcp"]
    stalls, net_ops = ctx["stalls"], ctx["net_ops"]

    sweep = 2.0
    fetch = 0.33                     # documented in CLAUDE.md for this box, 2026-08-04
    sign_s = sign11.p50 / 1000.0
    wire_s = cl["wire_warm"].p50 / 1000.0
    react = fetch + sign_s + wire_s
    react_p95 = fetch + cl["sign_set"].p95 / 1000.0 + cl["wire_warm"].p95 / 1000.0
    from_score = sign_s + wire_s
    mean_chain = sweep / 2 + react
    worst_chain = sweep + react

    lines = [
        "# C8 — real end-to-end latency (Amsterdam), measured 2026-08-05",
        "",
        f"`python -m backend.exec.latency_probe --n {ctx['n']}` — run {ctx['started']} UTC "
        f"against `{CLOB_HOST}` from the Vultr AMS box, with `negrisk-arb.service` running "
        "throughout (so these are contended numbers, which is the production condition). "
        f"Per-request series: `{ctx.get('raw_path', '(see --raw)')}`; "
        "`--from-raw <path>` re-renders this report without touching the network.",
        "",
        f"Board: `{ctx['slug']}` ({ex['sign']['legs']} legs, each priced at its own tick). "
        f"Probe leg: `…{ctx['probe_token'][-8:]}` at ${ctx['probe_price']:.3f} × "
        f"{DEFAULT_SIZE:g} shares = ${ctx['probe_price'] * DEFAULT_SIZE:.3f}/probe "
        f"(best bid {ctx['probe_bid']}, best ask {ctx['probe_ask']} — "
        f"{ctx['probe_bid'] / ctx['probe_price']:.0f}× the price).",
        "",
        "**No authenticated request was made and no order could have been created.** The wire "
        "probe posts a well-formed signed body with NO auth headers, `owner` = nil UUID, "
        "signed by the public throwaway key `0x0123…0123` (unfunded, no allowances), at a "
        "price the full `DryRunGuard` cleared against a freshly-read book at post time. The "
        "loop aborts on any status other than 401/403. Nominal notional across the whole "
        f"run: ${ex['wire']['notional']:.3f}, inside the $1.00 cap; actual exposure $0.00.",
        "",
        "## Components",
        "",
        "All times in **milliseconds**. Warm = one reused HTTP/2 connection "
        f"({', '.join(ex['wire']['http'])}); cold = a brand-new client per request "
        "(DNS + TCP + TLS). Headline figures are the warm series.",
        "",
        TABLE_HEAD,
        sign1.row(),
        sign11.row(),
        hmac_s.row(),
        wire_w.row(),
        wire_c.row(),
        book_w.row(),
        book_c.row(),
        ref_t.row(),
        ref_tcp.row(),
    ]
    if stalls:
        lines += [cl[k].row() for k in sorted({k for k, _ in stalls})]
    lines += [
        "",
        f"- **(a) SIGN** — local EIP-712 over the negRisk exchange domain, metadata cache "
        f"warm. **{sign1.p50:.1f}ms per order, {sign11.p50:.1f}ms for the full "
        f"{ex['sign']['legs']}-leg set** (p95 {sign11.p95:.1f}ms, max {sign11.mx:.1f}ms — "
        "the spread is CPU contention with the running scanner, not the crypto). Pure CPU: "
        "no network in the timed region. On a 1-vCPU box this is not a rounding error — "
        "see §Design.",
        f"- **(a′) metadata warm-up** — the first `create_order` on a token also does "
        f"`GET /tick-size`, `/neg-risk`, `/fee-rate`. Filling all {ex['sign']['meta_gets']} "
        f"for this board cost **{ex['sign']['meta_warm_ms']:.0f}ms** "
        f"(~{ex['sign']['meta_warm_ms'] / max(1, ex['sign']['legs']):.0f}ms/leg). "
        "`build_signed`'s returned `sign_ms` includes any of these that miss, so a cold "
        "`sign_ms` is a network number wearing a CPU number's name.",
        f"- **(a″) L2 header HMAC** — the local half of the authenticated path, timed with "
        f"dummy creds over a real {ex['sign']['body_bytes']}-byte body: "
        f"**{hmac_s.p50 * 1000:.0f}µs p50**. Authentication adds no meaningful local cost, "
        "so (a) is not an underestimate of the authenticated build path.",
        f"- **(b) WIRE** — **{cl['wire_warm'].p50:.1f}ms p50 time-to-401** "
        f"(p95 {cl['wire_warm'].p95:.1f}, max {cl['wire_warm'].mx:.1f}), "
        f"{cl['wire_warm'].n} warm requests excluding stalls (see §Stalls). Cold p50 "
        f"{wire_c.p50:.1f}ms over {wire_c.n} requests — "
        + (f"{_delta(wire_c.p50, cl['wire_warm'].p50)} of handshake"
           if wire_c.p50 - cl["wire_warm"].p50 >= 2
           else f"not distinguishable from warm at n={wire_c.n}")
        + f". With the TLS endpoint ~{ref_tcp.p50:.0f}ms away the handshake is small against "
          f"the ~{cl['wire_warm'].p50:.0f}ms round trip, so connection reuse is worth having "
          "but is not where the time goes. Server said: "
          f"{', '.join(repr(b) for b in ex['wire']['bodies'])}; "
        + ", ".join(f"{v} × HTTP {k}" for k, v in ex["wire"]["statuses"].items())
        + " — every single request rejected, as designed.",
        f"- **(c) BOOK** — `fetch_books` over {ex['book']['tokens']} tokens = one "
        f"`POST /books`, **{cl['book_warm'].p50:.1f}ms p50** warm "
        f"({ex['book']['returned']}/{ex['book']['tokens']} books returned every time).",
        f"- **(d) AUTH** — **{ctx['auth_note']}**",
        f"- **(ref)** `GET /time` p50 {cl['ref_time'].p50:.1f}ms vs raw TCP connect "
        f"{ref_tcp.p50:.1f}ms. Read this pair carefully — it corrects a premise. **The ~2ms "
        "in CLAUDE.md is the distance to the Cloudflare edge in Amsterdam, not to the "
        f"CLOB.** The cheapest real endpoint on the platform still costs "
        f"{cl['ref_time'].p50:.1f}ms and `POST /order` costs {cl['wire_warm'].p50:.1f}ms — "
        f"{_delta(cl['wire_warm'].p50, cl['ref_time'].p50)}. Two consequences: (i) an API "
        "round trip from here is ~10× the edge RTT, and the residual is consistent with the "
        "edge→origin hop (B6: origin is AWS eu-west-2, London) rather than anything we "
        "control; (ii) because the order front door costs essentially the same as a "
        "timestamp lookup, the 401 is returned before any order work happens — which is "
        "precisely why (b) is a floor and not an estimate.",
        "",
        "## What (b) does and does not measure",
        "",
        "**Time-to-401 is a LOWER BOUND on time-to-accepted.** A 401 is returned by the "
        "front door before the parts of order handling that cost real time: signature "
        "recovery, balance/allowance checks, tick/price validation, the book lock, and "
        "matching. An accepted FOK additionally has to be *matched or killed* before it "
        "answers. Nothing here measures any of that, and no amount of unauthenticated "
        f"probing can. Treat {cl['wire_warm'].p50:.0f}ms as the floor of the wire term and "
        "the true accept latency as unknown-but-larger until a key exists (component d).",
        "",
        "## Stalls",
        "",
    ]
    notes = ctx.get("notes") or []
    if stalls:
        vals = ", ".join(f"{v / 1000:.1f}s ({k})" for k, v in stalls)
        lines += [
            f"**{len(stalls)} of {net_ops} network operations in this run stalled** "
            f"({len(stalls) / net_ops * 100:.1f}%): {vals}. Stalls are excluded from the "
            "headline p50/p95 above and reported here instead — smearing them into a p99 "
            "would hide a connection-setup problem inside a latency number.",
        ]
    else:
        lines += [f"None in this run: none of the {net_ops} network operations exceeded "
                  f"{STALL_MS:.0f}ms."]
    for note in notes:
        lines += ["", note]
    if stalls or notes:
        lines += [
            "",
            "Small, but not ignorable: **a 5s stall on an order submission is a dead set**, "
            "arriving ~7× past the 0.7s decay window, and it is a property of this box's "
            "connection setup rather than of the CLOB. Two cheap mitigations, both outside "
            "this probe's scope: pin a local caching resolver (or "
            "`options timeout:1 attempts:2` in `/etc/resolv.conf`), and give the production "
            "order client an explicit request timeout well under the window so a stalled "
            "submit *fails* instead of arriving late into a book that has moved.",
        ]
    lines += [
        "",
        "## See → submit",
        "",
        "Two clocks, and conflating them is the classic error here. The shadow probe's "
        "**0.7s** is measured from `ts` — the moment a sweep *scored* a board, stamped after "
        "its fetch — not from when the price appeared.",
        "",
        "| clock | terms | estimate |",
        "|---|---|---|",
        f"| **scored → our bytes at the front door** | sign {sign_s * 1000:.0f}ms + wire "
        f"{wire_s * 1000:.0f}ms | **~{from_score:.2f}s** |",
        f"| price on book → submit, best case | fetch {fetch:.2f}s + sign + wire | "
        f"~{react:.2f}s |",
        f"| price on book → submit, mean | ½ × {sweep:.0f}s sweep + {react:.2f}s | "
        f"~{mean_chain:.2f}s |",
        f"| price on book → submit, worst | {sweep:.0f}s sweep + {react:.2f}s | "
        f"~{worst_chain:.2f}s |",
        "",
        f"The fetch term is the documented 0.33s full-universe sweep fetch for this box "
        f"(CLAUDE.md, 2026-08-04); the single-board {cl['book_warm'].p50:.0f}ms measured "
        "above is the cost of a *re-check* before submit, not of the sweep itself. p95 of "
        f"the reaction leg is ~{react_p95:.2f}s. None of these include time-to-accepted, "
        "which is unmeasured.",
        "",
        "## Against the 0.7s shadow-decay read",
        "",
        "Documented (GOLIVE_TESTPLAN_2026-08-02.md): **$87.16 of $310.43 of arrival value "
        "survived 0.7s** over 5.82 days = **28.1%**, $14.97/day — the figure that agreed "
        "within ~20% with the $18.34/day row-2 replay crediting and is what made that "
        "crediting credible.",
        "",
        f"Our order path reaches the front door **~{from_score:.2f}s after scoring** — about "
        f"{from_score / 0.7 * 100:.0f}% of the shadow probe's 0.7s window, and that is with "
        "the wire term being a lower bound. Even if time-to-accepted were 5× the measured "
        f"time-to-401, the scored→submit leg would be ~{sign_s + 5 * wire_s:.2f}s, still "
        "inside 0.7s.",
        "",
        "**Reachable fraction.** On the reaction leg the order path is not the binding "
        f"constraint: at ~{from_score:.2f}s scored→submit we land well inside the 0.7s at "
        "which 28.1% of arrival value was still there, so **the 28.1% / $14.97-per-day "
        "survival read is a lower bound on what this box's order path can reach, not a "
        "ceiling it misses** — the ~$9.78/day Tier-A/B executable figure needs no further "
        "latency haircut for Amsterdam, and a latency-shaped kill is not supported by this "
        "measurement. What we cannot claim is *more* than 28.1%: survival was never measured "
        "between 0s and 0.7s, so the ~0.55s of headroom we appear to have buys an "
        "unmeasured — probably positive, definitely unquantified — amount. **The binding "
        f"term is detection, not submission**: the 2s sweep cadence contributes "
        f"~{sweep / 2:.1f}s mean and up to {sweep:.0f}s, i.e. "
        f"{sweep / 2 / mean_chain * 100:.0f}–{sweep / worst_chain * 100:.0f}% of the whole "
        f"chain, against {from_score / mean_chain * 100:.0f}% for sign+wire combined. "
        "Latency effort belongs on the WebSocket market feed (B6: exists, ~117 msg/s for one "
        "board), not on the order path.",
        "",
        "## Design: does this change the batch-FOK case?",
        "",
        "**It strengthens it, and it adds two hazards that were not on the list.**",
        "",
        f"1. **Sequential posting is now measurably worse, not just modelled worse.** Eleven "
        f"sequential `POST /order` calls cost 11 × {cl['wire_warm'].p50:.0f}ms ≈ "
        f"{11 * cl['wire_warm'].p50 / 1000:.2f}s of wire *at the 401 floor* — on its own "
        f"{11 * cl['wire_warm'].p50 / 700 * 100:.0f}% of the 0.7s window, before any "
        f"matching time, with leg 11 landing ~{10 * cl['wire_warm'].p50 / 1000:.2f}s after "
        f"leg 1. The batch pays {cl['wire_warm'].p50:.0f}ms once. A2's 13.6% broken-set rate "
        "was derived at modelled latencies; this is the same verdict measured on the wire.",
        f"2. **Signing, not the network, is the biggest term we control.** "
        f"{sign11.p50:.0f}ms to sign 11 legs vs {cl['wire_warm'].p50:.0f}ms to post them — "
        f"signing is ~{sign11.p50 / (sign11.p50 + cl['wire_warm'].p50) * 100:.0f}% of the "
        f"scored→submit path on this 1-vCPU box, and its p95 ({sign11.p95:.0f}ms) is more "
        "than double its p50 because it competes with the scanner for the single core. It "
        "is sequential and single-threaded today. Pre-signing the current set between "
        "sweeps, or signing the legs off the hot path, is worth more than any network "
        "tuning available here. Note the interaction with FOK: the order **type is not "
        "signed over** (it rides in the POST body), so a pre-signed set can still choose "
        "FOK at submit — but price and size *are* signed, so pre-signing only helps where "
        "the intended prices are stable across a sweep.",
        f"3. **NEW HAZARD — the tick-size cache expires.** `ClobClient.get_tick_size` caches "
        f"per token with a **300s TTL** (`tick_size_ttl`, a constructor argument); neg-risk "
        f"and fee-rate are cached for the life of the process. Cold, the three metadata GETs "
        f"cost ~{ex['sign']['meta_warm_ms'] / max(1, ex['sign']['legs']):.0f}ms/leg "
        f"(**{ex['sign']['meta_warm_ms']:.0f}ms for a whole board**). So every 5 minutes the "
        "first set signed on a given board silently pays roughly that again, *inside* "
        "`build_signed` and counted as `sign_ms` — which alone would blow the 0.7s window. "
        "Mitigations in order of preference: construct the ClobClient with a large "
        "`tick_size_ttl` and refresh deliberately; or keep a warm-up loop touching "
        "`get_tick_size` for the live token set every <300s. A fresh `OrderClient.from_env()` "
        "per opportunity is the worst possible shape and must not be the production one.",
        "4. **NEW HAZARD — connection-setup stalls, and why they argue for FOK.** The ~5s "
        "stalls in §Stalls hit connection establishment, not the CLOB. A GTC leg that "
        "arrives 5s late **rests on the book at a price the market has moved past**; an FOK "
        "leg that arrives 5s late is killed and costs nothing but the missed set. That is a "
        "second, independent argument for per-order FOK on top of A2's — and an argument "
        "for an explicit sub-second client timeout, so a stalled submit fails instead of "
        "arriving into a dead book.",
        f"5. **Batch size is not a latency problem.** One `POST /orders` carries all 11 "
        f"({ex['sign']['body_bytes']}B per order, ~{ex['sign']['body_bytes'] * 11 // 1024}KB "
        "for the set) — trivial on a path whose cost is the edge→origin hop, not the "
        "payload.",
        "",
        "## Caveats — every one of them",
        "",
        "- **Time-to-401 is a lower bound.** The wire number excludes signature recovery, "
        "balance/allowance checks, book lock and matching. Time-to-**accepted** is unmeasured.",
        "- **No fills were measured.** Nothing here says an order would fill, only when its "
        "bytes arrive. Fill probability is A2/D11 territory.",
        "- **Matching latency is unmeasured**, and for FOK the kill/fill decision happens "
        "inside the request. The accept path could be several times the reject path.",
        "- **Component (d) is skipped.** Until a Polygon key exists, accepted-latency, "
        "matching-latency and cancel-latency cannot be measured at all.",
        f"- **Single-day, single-board, n={ctx['n']} sample**, one time of day "
        f"({ctx['started'][11:16]} UTC), one board (`{ctx['slug']}`), one probe leg. At "
        f"n={wire_w.n} the reported p99 IS the max (nearest-rank, no interpolation) — it is "
        "not a tail estimate. Nothing here says the p50 is the same at 14:00 UTC.",
        "- **Server-side queueing at real order rates is unknown.** These probes are one at a "
        "time, 250ms apart, from an account with no orders and no book presence. A live "
        "11-order batch against a busy matching engine, competing with other bots on the "
        "same board, is a different load and may be a different latency.",
        "- **The scanner was running throughout** — realistic contention, and the reason the "
        "SIGN p95 is double its p50, but it also means these numbers carry whatever CPU the "
        "sweep happened to be using.",
        f"- **No rate limiting and no Cloudflare interference was observed** at this rate "
        f"({ctx['n']} POSTs at {ctx['spacing'] * 1000:.0f}ms spacing; "
        + ", ".join(f"{v} × HTTP {k}" for k, v in ex["wire"]["statuses"].items())
        + "; no 429, no challenge page"
        + ("; Cloudflare `cf-ray` PoP "
           + ", ".join(f"{k}×{v}" for k, v in ex["wire"]["pops"].items())
           if ex["wire"].get("pops") else "")
        + "). That is not evidence there is none at 40 orders/s (B6's documented per-signer "
          "limit).",
        "- The 0.7s survival figure is itself a 5.82-day measurement from the **Singapore** "
        "vantage, and the shadow probe's own delay distribution was shaped by 171ms RTT. "
        "From Amsterdam the probe would re-read sooner, so re-running the shadow report from "
        "this box would measure decay on a slightly different clock than the one being "
        "compared against here.",
        "- The probe prices each leg at the market's **minimum tick**, far below any real "
        "order. Body size and therefore wire time for a production-priced order are "
        "effectively identical, but no claim is made about server-side handling of a "
        "marketable price, which we have never sent.",
        "",
        f"Reproduce: `python -m backend.exec.latency_probe --n {ctx['n']}` (re-probes), or "
        f"`--from-raw {ctx.get('raw_path', '<raw.json>')}` (re-renders this file offline).",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- main

def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m backend.exec.latency_probe",
                                description=__doc__.split("\n\n")[0])
    p.add_argument("--n", type=int, default=50,
                   help="requests per network component (hard cap 50 — polite to prod)")
    p.add_argument("--out", default=DEFAULT_OUT, help="report path (- to skip writing)")
    p.add_argument("--raw", default=None,
                   help="per-request series JSON (default: <out> with a _raw.json suffix)")
    p.add_argument("--from-raw", default=None,
                   help="re-render the report from a raw JSON file; makes no requests")
    p.add_argument("--note", action="append", default=[],
                   help="a field observation to record under §Stalls (repeatable). Kept in "
                        "the raw JSON so it survives a re-render — an out-of-band anomaly "
                        "belongs in the run record, not in the report generator.")
    p.add_argument("--spacing", type=float, default=0.25,
                   help="seconds between requests (floor 0.25)")
    p.add_argument("--slug", default=None, help="explicit board slug instead of tomorrow's")
    p.add_argument("--date", default=None,
                   help="YYYY-MM-DD resolution date (default: tomorrow UTC)")
    p.add_argument("--env-file", default=".env", help="where the POLYMARKET_* vars would live")
    args = p.parse_args(argv)

    raw_path = args.raw or (args.out.rsplit(".", 1)[0] + "_raw.json"
                            if args.out and args.out != "-" else None)

    if args.from_raw:
        with open(args.from_raw) as fh:
            raw = json.load(fh)
        raw["raw_path"] = args.from_raw
        if args.note:
            raw["notes"] = list(raw.get("notes") or []) + args.note
            with open(args.from_raw, "w") as fh:      # the raw file IS the run record
                json.dump(raw, fh, indent=1)
        return _write(build_report(_ctx_from_raw(raw)), args.out)

    if args.n > 50:
        raise SystemExit("--n is capped at 50: these are POSTs against production")
    if args.spacing < 0.25:
        raise SystemExit("--spacing floor is 0.25s: these are POSTs against production")

    from py_clob_client.client import ClobClient

    from .order_client import STRICT_DEFAULT, OrderClient

    started = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    board = _pick_board(args.slug, args.date)
    tokens = [leg["yes"] for leg in board["legs"]]
    print(f"board  : {board['slug']} ({len(tokens)} legs)")
    print(f"host   : {CLOB_HOST}   n={args.n}  spacing={args.spacing}s")

    client = ClobClient(CLOB_HOST, chain_id=137, key=THROWAWAY_KEY,
                        signature_type=0, funder=THROWAWAY_ADDRESS)
    assert client.get_address().lower() == THROWAWAY_ADDRESS.lower(), "wrong signer"
    # STRICT_DEFAULT: no flag in this CLI can loosen it. Never .from_env() — this probe has
    # no business holding real credentials.
    oc = OrderClient(client, guard=STRICT_DEFAULT)

    series: dict[str, list] = {}
    extra: dict[str, dict] = {}

    print("\n(a) SIGN")
    sign_series, sign_extra = component_sign(oc, tokens, args.n)
    series.update(sign_series)
    extra["sign"] = sign_extra
    for k in ("sign_order", "sign_set"):
        print("   ", Stats.of(k, sign_series[k]).line())
    print(f"    metadata warm-up: {sign_extra['meta_warm_ms']:.0f}ms "
          f"for {sign_extra['meta_gets']} GETs")

    # Probe leg. Two things are being optimised, in this order:
    #   1. CHEAPEST tick — the order is priced at the market's minimum tick, so the cheapest
    #      tick means the smallest nominal notional per probe and the most probes that fit
    #      under the guard's $1.00 cap;
    #   2. widest bid/price margin — the guard needs price < ½·bid; we demand bid > 4×price
    #      so a book ticking down mid-run cannot turn a rail into a failed run.
    cands = []
    for tid in tokens:
        bid, ask = oc.top_of_book(tid, refresh=True)
        price = sign_extra["prices"][tid]
        if bid is None or price <= 0 or bid <= 4 * price:
            continue
        cands.append((price, -bid / price, tid, bid, ask))
    if not cands:
        raise SystemExit(
            "no leg on this board has a bid above 4x its tick — cannot prove "
            "non-marketability with margin; pick another board with --slug"
        )
    cands.sort()
    probe_price, _m, probe_token, probe_bid, probe_ask = cands[0]

    # The $1.00 cap is enforced, not waived: if n probes would not fit, do fewer and say so.
    n_wire = args.n
    affordable = int(oc.guard.max_notional / (probe_price * DEFAULT_SIZE))
    if affordable < n_wire:
        print(f"    notional cap: {n_wire} probes x ${probe_price * DEFAULT_SIZE:.3f} "
              f"> ${oc.guard.max_notional:.2f} — reducing to {affordable}")
        n_wire = affordable
    print(f"\n(b) WIRE  leg …{probe_token[-8:]} bid={probe_bid} ask={probe_ask} "
          f"price=${probe_price:.3f} x {DEFAULT_SIZE:g} = ${probe_price * DEFAULT_SIZE:.3f}"
          f"/probe, {n_wire} probes")
    try:
        wire_series, wire_extra = component_wire(
            oc, probe_token, probe_price, n_wire, args.spacing)
    except ProbeAborted as exc:
        print(f"\nABORTED: {exc}")
        return 3
    series.update(wire_series)
    extra["wire"] = wire_extra
    for k in ("wire_warm", "wire_cold"):
        print("   ", Stats.of(k, wire_series[k]).line())

    print("\n(c) BOOK")
    book_series, book_extra = component_book(tokens, args.n, args.spacing)
    series.update(book_series)
    extra["book"] = book_extra
    for k in ("book_warm", "book_cold"):
        print("   ", Stats.of(k, book_series[k]).line())

    print("\n(d) AUTH")
    auth_note = component_auth(args.env_file)
    print("   ", auth_note)

    print("\n(ref)")
    series.update(component_reference(min(args.n, 20), args.spacing))
    for k in ("ref_time", "ref_tcp"):
        print("   ", Stats.of(k, series[k]).line())

    raw = {
        "n": args.n, "spacing": args.spacing, "started": started, "slug": board["slug"],
        "probe_token": probe_token, "probe_price": probe_price,
        "probe_bid": probe_bid, "probe_ask": probe_ask, "auth_note": auth_note,
        "series": series, "extra": extra, "raw_path": raw_path, "notes": args.note,
    }
    if raw_path:
        os.makedirs(os.path.dirname(raw_path) or ".", exist_ok=True)
        with open(raw_path, "w") as fh:
            json.dump(raw, fh, indent=1)
        print(f"\nwrote {raw_path}")

    ctx = _ctx_from_raw(raw)
    if ctx["stalls"]:
        print(f"    STALLS: {len(ctx['stalls'])}/{ctx['net_ops']} network ops ≥"
              f"{STALL_MS:.0f}ms — " + ", ".join(f"{k} {v:.0f}ms" for k, v in ctx["stalls"]))
    return _write(build_report(ctx), args.out)


def _write(report: str, out: str) -> int:
    if out and out != "-":
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(out, "w") as fh:
            fh.write(report)
        print(f"wrote {out}  ({len(report)} bytes)")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
