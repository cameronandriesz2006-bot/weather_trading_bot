"""The only place orders are built, signed and posted. Rails live here, not in the CLIs.

Verified against the installed py-clob-client 0.34.6 source (read 2026-08-05):

  - **FOK is supported.** ``clob_types.OrderType`` = GTC / FOK / GTD / FAK, and the type is
    carried in the request body (``utilities.order_to_json`` → ``{"order":…, "owner":api_key,
    "orderType":…, "postOnly":…}``), not in the signature. So order type is a post-time
    choice over an already-signed order — which is why ``build_signed`` and ``post_*`` can
    take it separately without re-signing.
  - **Batch posting exists in the released client**: ``ClobClient.post_orders(list[
    PostOrdersArgs])`` → ONE ``POST /orders`` (``endpoints.POST_ORDERS = "/orders"``), body =
    ``[order_to_json(...) for each]``, serialized once with ``separators=(",",":")`` and sent
    as exact bytes so the L2 HMAC covers the literal payload. No hand-rolled httpx needed;
    the plan's fallback branch is not taken. ``post_batch`` here is a thin wrapper, so the
    payload is the library's own serialization by construction.
  - Signing: ``order_builder.builder.OrderBuilder.create_order`` → ``py_order_utils``
    ``build_signed_order`` → EIP-712 over the ``Order`` struct with domain
    ("Polymarket CTF Exchange", "1", chainId, exchange). ``salt`` comes from
    ``py_order_utils.utils.generate_seed`` (time × random) so two signings of the same spec
    differ by design; ``eth_account.Account._sign_hash`` is RFC-6979 deterministic given the
    hash, so pinning the salt pins the signature (see tests/test_exec_client.py).
  - negRisk boards sign against a DIFFERENT exchange contract
    (``config.get_contract_config(chain_id, neg_risk=True)`` → 0xC5d5…f80a) than binary
    markets. ``neg_risk`` is resolved per-token by the library (GET /neg-risk, cached) — we
    do not hard-code it.

Rails (Tier C non-negotiable: no fillable order, ever). Enforced on the *decoded signed
order*, i.e. on the exact bytes about to go on the wire, so a caller that hand-forges a
SignedOrder and calls ``post_single`` directly is still stopped:

  1. BUY only. A SELL of a token we do not hold is a short, and the short leg is the one
     with real downside here.
  2. price ≤ $0.02.
  3. price < ½ · best bid, book-checked at post time. Since best_bid ≤ best_ask always, a
     price under the bid is under the ask, so the order is provably non-marketable — the
     bid reference is strictly stronger than an ask reference, which is why the plan picks
     it. Additionally rejected if price ≥ best_ask (redundant, kept as a second wall).
     No bids on the book → refuse: non-marketability cannot be proven without a reference.
  4. size ≤ 5 shares (the platform minimum — we never dry-run above the smallest real order).
  5. ≤ $1.00 cumulative notional per OrderClient instance, across singles and batches.

Worst case if every rail somehow fails at once: ≤ $1.00 of far-out-of-the-money lottery
tickets, auto-cancelled by the context manager on exit.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

BUY = "BUY"
SELL = "SELL"


class GuardRailViolation(RuntimeError):
    """A dry-run rail rejected an order. Never caught inside this module."""


@dataclass(frozen=True)
class OrderSpec:
    token_id: str
    side: Literal["BUY", "SELL"]
    price: float
    size: float


@dataclass
class PostResult:
    ok: bool
    order_id: Optional[str]
    status: str
    error: Optional[str]
    latency_ms: float


@dataclass(frozen=True)
class DryRunGuard:
    """Hard limits for the no-money-at-risk tier. Frozen: nothing may relax them in flight.

    ``max_bid_fraction`` 0.5 means the order price must be under half the current best bid.
    ``require_book`` False is for offline unit tests of the static rails ONLY; the live
    paths keep it True, and a guard with it False can never be the default.
    """

    max_price: float = 0.02
    max_bid_fraction: float = 0.5
    max_size: float = 5.0
    max_notional: float = 1.00
    buy_only: bool = True
    require_book: bool = True

    def check_static(self, side: str, price: float, size: float) -> None:
        """Rails that need no book. Cheap, so they also run at build time."""
        if self.buy_only and side != BUY:
            raise GuardRailViolation(
                f"dry-run is BUY-side only, got side={side!r}"
            )
        if not (price > 0):
            raise GuardRailViolation(f"price must be positive, got {price}")
        if price > self.max_price + 1e-12:
            raise GuardRailViolation(
                f"price {price:.6f} exceeds dry-run cap {self.max_price:.4f}"
            )
        if not (size > 0):
            raise GuardRailViolation(f"size must be positive, got {size}")
        if size > self.max_size + 1e-12:
            raise GuardRailViolation(
                f"size {size} exceeds dry-run cap {self.max_size} shares"
            )

    def check_book(self, price: float, best_bid: Optional[float], best_ask: Optional[float]) -> None:
        """The non-marketability proof. Fails closed on a missing/empty book."""
        if not self.require_book:
            return
        if best_bid is None:
            raise GuardRailViolation(
                "no bids on the book — cannot prove the order is non-marketable, refusing"
            )
        limit = self.max_bid_fraction * best_bid
        if price >= limit - 1e-12:
            raise GuardRailViolation(
                f"price {price:.6f} is not below {self.max_bid_fraction:g} x best bid "
                f"{best_bid:.6f} (= {limit:.6f})"
            )
        if best_ask is not None and price >= best_ask - 1e-12:
            raise GuardRailViolation(
                f"price {price:.6f} is marketable against best ask {best_ask:.6f}"
            )

    def check_notional(self, spent: float, adding: float) -> None:
        if spent + adding > self.max_notional + 1e-9:
            raise GuardRailViolation(
                f"notional ${spent + adding:.4f} would exceed the ${self.max_notional:.2f} "
                f"per-invocation cap (already committed ${spent:.4f})"
            )


#: The only guard any caller gets unless it explicitly constructs another.
STRICT_DEFAULT = DryRunGuard()


def decode_signed(signed: Any) -> tuple[str, str, float, float]:
    """(token_id, side, price, size) read back out of a SignedOrder's wire form.

    Guarding the decoded order rather than the OrderSpec is deliberate: ``post_single``
    accepts an arbitrary signed object, so the rails must read the same bytes the server
    will. Amounts are 6-decimal fixed point (``order_builder.helpers.to_token_decimals``);
    BUY quotes makerAmount=USDC / takerAmount=shares, SELL is the mirror.
    """
    wire = signed.dict()
    side = wire["side"]
    maker = float(wire["makerAmount"])
    taker = float(wire["takerAmount"])
    if side == BUY:
        size = taker / 1e6
        price = (maker / taker) if taker else float("inf")
    else:
        size = maker / 1e6
        price = (taker / maker) if maker else float("inf")
    return str(wire["tokenId"]), side, price, size


def _extract(resp: Any) -> PostResult:
    """CLOB order responses are loosely shaped; take the union of the field names the
    server has used and never crash on an unexpected body."""
    if not isinstance(resp, dict):
        return PostResult(ok=False, order_id=None, status="unknown",
                          error=f"unparsable response: {resp!r}", latency_ms=0.0)
    ok = bool(resp.get("success", False))
    order_id = resp.get("orderID") or resp.get("orderId") or resp.get("order_id")
    status = str(resp.get("status") or ("accepted" if ok else "rejected"))
    error = resp.get("errorMsg") or resp.get("error") or None
    if error == "":
        error = None
    return PostResult(ok=ok, order_id=order_id, status=status, error=error, latency_ms=0.0)


class OrderClient:
    """Builds, signs and posts guarded dry-run orders. Use as a context manager: exit
    cancels everything this process opened, whether or not the body raised."""

    def __init__(self, client: Any, guard: DryRunGuard = STRICT_DEFAULT):
        self.client = client
        self.guard = guard
        self.notional_spent = 0.0
        self._book_cache: dict[str, tuple[Optional[float], Optional[float]]] = {}

    @classmethod
    def from_env(cls, guard: Optional[DryRunGuard] = STRICT_DEFAULT,
                 env_file: Optional[str] = ".env", creds_path: Optional[str] = None) -> "OrderClient":
        """L2 client from .env + the cached API creds. Raises MissingCredentials (listing
        every absent var) if the key is not configured — which is the expected state on
        this box until the user supplies one."""
        from . import clob_auth

        if guard is None:
            guard = STRICT_DEFAULT
        env = clob_auth.load_clob_env(env_file)
        client = clob_auth.build_client(env)
        clob_auth.ensure_api_creds(
            client, env.signature_type,
            creds_path or clob_auth.CREDS_CACHE_PATH,
        )
        return cls(client, guard=guard)

    # ---- book -------------------------------------------------------------

    def top_of_book(self, token_id: str, refresh: bool = False) -> tuple[Optional[float], Optional[float]]:
        """(best_bid, best_ask) from GET /book.

        The cache exists for *selection* (pick a liquid leg without refetching). The rails
        never read it — ``_guard_signed`` passes ``refresh=True``, because "book-checked at
        post time" means at post time, not whenever the leg was chosen.
        """
        if not refresh and token_id in self._book_cache:
            return self._book_cache[token_id]
        book = self.client.get_order_book(token_id)
        bids = getattr(book, "bids", None) or []
        asks = getattr(book, "asks", None) or []
        best_bid = max((float(l.price) for l in bids), default=None)
        best_ask = min((float(l.price) for l in asks), default=None)
        self._book_cache[token_id] = (best_bid, best_ask)
        return best_bid, best_ask

    # ---- build / post -----------------------------------------------------

    def build_signed(self, spec: OrderSpec, order_type: str = "FOK") -> tuple[Any, float]:
        """Sign one order; returns (SignedOrder, sign_ms).

        ``order_type`` is not signed over (it rides in the POST body) — it is accepted here
        only so callers read as one operation and so an invalid type fails before a network
        round trip. The static rails run here too; the authoritative check is in the post
        methods, which also see the book.
        """
        from py_clob_client.clob_types import OrderArgs

        _assert_order_type(order_type)
        self.guard.check_static(spec.side, spec.price, spec.size)
        args = OrderArgs(
            token_id=spec.token_id, price=spec.price, size=spec.size, side=spec.side,
        )
        t0 = time.perf_counter()
        signed = self.client.create_order(args)
        sign_ms = (time.perf_counter() - t0) * 1000.0
        return signed, sign_ms

    def _guard_signed(self, signed: Any, pending: float) -> float:
        """Run every rail against one decoded signed order. Returns its notional.
        ``pending`` is notional already committed by earlier orders in the same call."""
        token_id, side, price, size = decode_signed(signed)
        self.guard.check_static(side, price, size)
        best_bid, best_ask = self.top_of_book(token_id, refresh=True)
        self.guard.check_book(price, best_bid, best_ask)
        notional = price * size
        self.guard.check_notional(self.notional_spent + pending, notional)
        return notional

    def post_single(self, signed: Any, order_type: str = "FOK") -> PostResult:
        """One guarded order via POST /order."""
        _assert_order_type(order_type)
        notional = self._guard_signed(signed, pending=0.0)
        # Committed at ATTEMPT, not at success: a post that times out may still have been
        # accepted, so the budget must assume it was. The cap is a risk limit, not a counter.
        self.notional_spent += notional

        t0 = time.perf_counter()
        try:
            resp = self.client.post_order(signed, order_type)
        except Exception as exc:  # noqa: BLE001 — a rejection is data, not a crash
            return PostResult(ok=False, order_id=None, status="error", error=str(exc),
                              latency_ms=(time.perf_counter() - t0) * 1000.0)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        result = _extract(resp)
        result.latency_ms = latency_ms
        return result

    def post_batch(self, signed_list: list, order_type: str = "FOK") -> list[PostResult]:
        """Every order in ONE POST /orders — never sequential, never aborted mid-set
        (Tier A/B verdict: sequential legging broke 13.6% of sets and was −EV).

        All rails run over the whole set BEFORE anything is sent, so a bad leg kills the
        submission rather than leaving a partial set on the book.
        """
        _assert_order_type(order_type)
        from py_clob_client.clob_types import PostOrdersArgs

        if not signed_list:
            return []
        pending = 0.0
        for signed in signed_list:
            pending += self._guard_signed(signed, pending)

        args = [PostOrdersArgs(order=s, orderType=order_type, postOnly=False) for s in signed_list]
        self.notional_spent += pending          # committed at attempt (see post_single)
        t0 = time.perf_counter()
        try:
            resp = self.client.post_orders(args)
        except Exception as exc:  # noqa: BLE001
            latency_ms = (time.perf_counter() - t0) * 1000.0
            return [PostResult(ok=False, order_id=None, status="error", error=str(exc),
                               latency_ms=latency_ms) for _ in signed_list]
        latency_ms = (time.perf_counter() - t0) * 1000.0

        items = resp if isinstance(resp, list) else [resp]
        results = [_extract(item) for item in items]
        # One round trip: the whole set shares the measured latency.
        for r in results:
            r.latency_ms = latency_ms
        if len(results) != len(signed_list):
            results.append(PostResult(
                ok=False, order_id=None, status="mismatch",
                error=f"server returned {len(items)} results for {len(signed_list)} orders",
                latency_ms=latency_ms,
            ))
        return results

    # ---- teardown ---------------------------------------------------------

    def open_order_count(self) -> int:
        return len(self.client.get_orders() or [])

    def cancel_all(self) -> int:
        """Cancel every open order for this API key. Returns the count the server says it
        cancelled (0 if it reports nothing — never raises on a well-formed 'nothing to do')."""
        resp = self.client.cancel_all()
        if isinstance(resp, dict):
            cancelled = resp.get("canceled") or resp.get("cancelled") or []
            return len(cancelled) if isinstance(cancelled, list) else 0
        if isinstance(resp, list):
            return len(resp)
        return 0

    def __enter__(self) -> "OrderClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> Literal[False]:
        # Best-effort: an exception on the way out must not mask the original one, but a
        # failed cancel is loud, because an uncancelled resting order is the whole risk.
        try:
            n = self.cancel_all()
            if n:
                print(f"[OrderClient] cancel_all on exit: {n} order(s) cancelled")
        except Exception as exc2:  # noqa: BLE001
            print(f"[OrderClient] WARNING cancel_all on exit FAILED: {exc2} — "
                  "check for resting orders manually")
        return False


def _assert_order_type(order_type: str) -> None:
    from py_clob_client.clob_types import OrderType

    valid = {OrderType.GTC, OrderType.FOK, OrderType.GTD, OrderType.FAK}
    if order_type not in valid:
        raise ValueError(f"order_type must be one of {sorted(valid)}, got {order_type!r}")
