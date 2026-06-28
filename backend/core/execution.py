"""Limit-order execution layer — simulation now, real Polymarket later (drop-in swap).

WHY THIS EXISTS
The bot has always modelled itself as a liquidity TAKER: it walks the ask book and pays the
VWAP (backend/data/orderbook.py). Stream-1 liquidity work (2026-06-28) showed the day-ahead
edge is real but the resting depth at any instant is tiny (~$2/bucket within 1c) while ~$1k
flows over the whole window — i.e. it is only harvestable as a patient MAKER: rest a limit
order near fair value and let order flow come to you (you earn the spread instead of paying
it). This module is that capability.

DESIGN — one interface, two backends, so going live is a swap not a rewrite:
  * ``Executor``                — the abstract interface the bot codes against.
  * ``SimulatedExecutor``       — faithful matching against live book + real trade flow. Used
                                  now (SIMULATION_MODE). Models crossing, price-time priority,
                                  partial fills, and trade-flow-driven resting fills.
  * ``LivePolymarketExecutor``  — wraps the real CLOB client (py-clob-client). Constructs and
                                  signs the SAME OrderArgs, posts GTC/GTD, cancels by id. Hard-
                                  gated behind SIMULATION_MODE=False + credentials; raises
                                  loudly otherwise. Going live = install py-clob-client, supply
                                  creds, flip the flag — no change to the bot's calling code.

FIDELITY TO REAL POLYMARKET (verified against docs.polymarket.com, 2026-06-28):
  * Order is SHARES at a PRICE in [0,1] (Polymarket-native); cash = price * shares. ``shares_for_cash``
    converts a Kelly cash budget into a share size at a chosen limit price.
  * Time-in-force matches the CLOB's four types: GTC (rest till cancelled), GTD (rest till a
    timestamp, auto-expire), FOK (all-or-nothing immediate), FAK (immediate, kill remainder).
  * The "cancel if unfilled after N seconds" feature == GTD. Polymarket enforces a 1-MINUTE
    security threshold on GTD, so an effective lifetime of N seconds is expiration = now+60+N
    (baked into ``_gtd_expiration`` and used by BOTH backends, so the sim and the live book
    expire at the identical instant). A local monitor (OrderManager) also expires/cancels as a
    backstop and to drive the simulated clock.
  * Crossing: a marketable portion (asks <= my buy limit, cheapest first) fills IMMEDIATELY as
    a taker at the BOOK price (price improvement); the remainder RESTS at my limit as a maker.

WHAT THE SIMULATION CAN AND CANNOT KNOW (honest):
  * Exact: order lifecycle/semantics, the crossing fill (real book), expiry, cancel — these are
    identical to live.
  * Modelled: a resting order's fills come from REAL subsequent trade prints at/through my price
    (Data API /trades), consumed AFTER a queue-ahead snapshot taken at placement (price-time
    priority). Queue position is approximated from that snapshot — the one thing a sim cannot
    observe perfectly without being in the real book. Documented, and conservative (it assumes
    everything resting at/ahead of my price fills before me).

This module is NOT wired into the live scan loop yet (that is a separate, reviewable step) and
imports nothing at module load that the running bot depends on, so it cannot affect the live bot.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Protocol, Tuple

logger = logging.getLogger("trading_bot")

# Polymarket's GTD security threshold: an order must be valid for at least ~1 min, so to get an
# effective lifetime of N seconds you set expiration = now + 60 + N. (docs.polymarket.com)
GTD_SECURITY_THRESHOLD_S = 60


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class TimeInForce(str, Enum):
    GTC = "GTC"   # good-till-cancelled: rest indefinitely
    GTD = "GTD"   # good-till-date: rest until expiration_ts, then auto-cancel
    FOK = "FOK"   # fill-or-kill: fill the WHOLE size immediately or reject
    FAK = "FAK"   # fill-and-kill (IOC): fill what's immediately available, kill the rest


class OrderStatus(str, Enum):
    OPEN = "OPEN"                       # live on the book (maker), unfilled or partially filled
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"                   # terminal: fully filled
    CANCELLED = "CANCELLED"             # terminal: cancelled (by us, or FOK/FAK kill)
    EXPIRED = "EXPIRED"                 # terminal: GTD lifetime elapsed before full fill
    REJECTED = "REJECTED"              # terminal: FOK could not fully fill, nothing done


TERMINAL = {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.EXPIRED, OrderStatus.REJECTED}


@dataclass
class Fill:
    """A single execution against an order."""
    price: float     # price actually paid/received (0-1)
    size: float      # shares
    ts: float        # unix seconds
    taker: bool      # True = we crossed (taker), False = we were rested and got hit (maker)


@dataclass
class LimitOrder:
    """A limit order and its lifecycle — the same fields a real Polymarket order carries, so the
    live executor maps onto it 1:1."""
    token_id: str
    side: OrderSide
    price: float                       # limit price (0-1)
    size: float                        # requested size in SHARES
    tif: TimeInForce = TimeInForce.GTC
    expiration_ts: Optional[float] = None   # set for GTD (already includes the +60 threshold)
    order_id: str = field(default_factory=lambda: f"sim-{uuid.uuid4().hex[:16]}")
    status: OrderStatus = OrderStatus.OPEN
    fills: List[Fill] = field(default_factory=list)
    created_ts: float = 0.0
    # --- simulator bookkeeping (ignored by the live executor) ---
    _queue_ahead: float = 0.0          # shares resting at/ahead of our price at placement (FIFO)
    _last_poll_ts: float = 0.0

    @property
    def filled_size(self) -> float:
        return sum(f.size for f in self.fills)

    @property
    def remaining_size(self) -> float:
        return max(0.0, self.size - self.filled_size)

    @property
    def notional_filled(self) -> float:
        return sum(f.price * f.size for f in self.fills)

    @property
    def avg_fill_price(self) -> Optional[float]:
        fs = self.filled_size
        return (self.notional_filled / fs) if fs > 0 else None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    def _refresh_status(self):
        if self.status in TERMINAL:
            return
        if self.remaining_size <= 1e-9:
            self.status = OrderStatus.FILLED
        elif self.fills:
            self.status = OrderStatus.PARTIALLY_FILLED


def shares_for_cash(cash: float, price: float) -> float:
    """Convert a cash budget (USDC) into a share size at ``price`` — Polymarket sizes orders in
    shares, the bot sizes in cash, so this bridges them. A BUY of N shares at p costs N*p."""
    if price <= 0:
        return 0.0
    return cash / price


def _gtd_expiration(now: float, ttl_seconds: float) -> float:
    """Absolute GTD expiration for an intended lifetime of ``ttl_seconds``, including Polymarket's
    1-minute security threshold. Both backends use this so sim and live expire identically."""
    return now + GTD_SECURITY_THRESHOLD_S + ttl_seconds


# ---------------------------------------------------------------------------------------------
# Market-data source (injectable so the simulator is fully testable offline)
# ---------------------------------------------------------------------------------------------
@dataclass
class BookSnapshot:
    """Full two-sided book at an instant: bids (highest first) and asks (cheapest first)."""
    bids: List[Tuple[float, float]]   # (price, size) desc by price
    asks: List[Tuple[float, float]]   # (price, size) asc by price


@dataclass
class TradePrint:
    price: float
    size: float
    ts: float


class MarketData(Protocol):
    """What the SimulatedExecutor needs from the world. ``LiveMarketData`` implements it against
    the real CLOB book + Data API trades; ``FakeMarketData`` (tests) scripts it deterministically."""
    async def book(self, token_id: str) -> Optional[BookSnapshot]: ...
    async def trades_since(self, token_id: str, since_ts: float) -> List[TradePrint]: ...


# ---------------------------------------------------------------------------------------------
# Executor interface
# ---------------------------------------------------------------------------------------------
class Executor:
    """Abstract execution interface. The bot calls THIS; the backend (sim vs live) is swapped in
    one place. All methods are async (the bot is async; the live client does network I/O)."""

    async def place_order(self, token_id: str, side: OrderSide, price: float, size: float,
                          tif: TimeInForce = TimeInForce.GTC,
                          ttl_seconds: Optional[float] = None) -> LimitOrder:
        raise NotImplementedError

    async def cancel_order(self, order: LimitOrder) -> bool:
        raise NotImplementedError

    async def poll_order(self, order: LimitOrder) -> LimitOrder:
        """Refresh fills/status (sim: advance from trade flow + expiry; live: query the CLOB)."""
        raise NotImplementedError

    # convenience: size a BUY/SELL from a cash budget at the limit price
    async def place_limit_for_cash(self, token_id: str, side: OrderSide, price: float, cash: float,
                                   ttl_seconds: Optional[float] = None) -> LimitOrder:
        tif = TimeInForce.GTD if ttl_seconds is not None else TimeInForce.GTC
        return await self.place_order(token_id, side, price, shares_for_cash(cash, price),
                                      tif=tif, ttl_seconds=ttl_seconds)


# ---------------------------------------------------------------------------------------------
# Simulated executor — faithful matching against live book + real trade flow
# ---------------------------------------------------------------------------------------------
class SimulatedExecutor(Executor):
    """Faithful limit-order simulation. Uses a ``MarketData`` source for the live book (entry
    crossing + queue snapshot) and real subsequent trade prints (resting fills). See the module
    docstring for exactly what is exact vs modelled."""

    def __init__(self, market_data: MarketData, clock=time.time):
        self.md = market_data
        self.clock = clock
        self.orders: Dict[str, LimitOrder] = {}

    async def place_order(self, token_id, side, price, size, tif=TimeInForce.GTC,
                          ttl_seconds=None) -> LimitOrder:
        now = self.clock()
        if tif == TimeInForce.GTD and ttl_seconds is None:
            raise ValueError("GTD order requires ttl_seconds")
        order = LimitOrder(
            token_id=token_id, side=OrderSide(side), price=float(price), size=float(size),
            tif=tif, created_ts=now, _last_poll_ts=now,
            expiration_ts=_gtd_expiration(now, ttl_seconds) if ttl_seconds is not None else None,
        )
        book = await self.md.book(token_id)
        if book is None:
            # no book -> nothing to cross; rests (or for FOK/FAK, rejected/cancelled)
            if tif in (TimeInForce.FOK, TimeInForce.FAK):
                order.status = OrderStatus.REJECTED if tif == TimeInForce.FOK else OrderStatus.CANCELLED
            self.orders[order.order_id] = order
            return order

        self._cross(order, book, now)                 # immediate marketable fill

        if order.remaining_size > 1e-9:
            if tif == TimeInForce.FOK:                 # all-or-nothing: undo, reject
                order.fills.clear()
                order.status = OrderStatus.REJECTED
            elif tif == TimeInForce.FAK:               # IOC: keep any partial fill, kill remainder
                order.status = OrderStatus.CANCELLED   # (filled_size is retained on the order)
            else:                                       # GTC/GTD: rest the remainder
                order._queue_ahead = self._queue_ahead(order, book)
                order._refresh_status()
        else:
            order.status = OrderStatus.FILLED

        self.orders[order.order_id] = order
        return order

    def _cross(self, order: LimitOrder, book: BookSnapshot, now: float):
        """Fill the marketable portion immediately against the opposite side at the BOOK price
        (price improvement), cheapest-asks-first for a buy / highest-bids-first for a sell, only
        through levels that satisfy the limit."""
        remaining = order.remaining_size
        if order.side == OrderSide.BUY:
            levels = [(p, s) for p, s in book.asks if p <= order.price + 1e-12]
        else:
            levels = [(p, s) for p, s in book.bids if p >= order.price - 1e-12]
        for p, s in levels:
            if remaining <= 1e-9:
                break
            take = min(remaining, s)
            order.fills.append(Fill(price=p, size=take, ts=now, taker=True))
            remaining -= take

    @staticmethod
    def _queue_ahead(order: LimitOrder, book: BookSnapshot) -> float:
        """Shares resting at-or-ahead of our price (price-time priority). For a resting BUY at L,
        incoming sells hit bids with price >= L before ours; symmetric for a SELL."""
        if order.side == OrderSide.BUY:
            return sum(s for p, s in book.bids if p >= order.price - 1e-12)
        return sum(s for p, s in book.asks if p <= order.price + 1e-12)

    async def poll_order(self, order: LimitOrder) -> LimitOrder:
        if order.is_terminal:
            return order
        now = self.clock()
        # expiry first (GTD): the remainder is auto-cancelled at expiration; partial fills stay.
        if order.expiration_ts is not None and now >= order.expiration_ts:
            order.status = OrderStatus.FILLED if order.remaining_size <= 1e-9 else OrderStatus.EXPIRED
            order._last_poll_ts = now
            return order
        # resting fills: real trade prints at/through our price since the last poll, consumed
        # AFTER the queue ahead of us (FIFO). A maker fill executes at OUR limit price.
        trades = await self.md.trades_since(order.token_id, order._last_poll_ts)
        for t in sorted(trades, key=lambda x: x.ts):
            if order.remaining_size <= 1e-9:
                break
            hits = (t.price <= order.price + 1e-12) if order.side == OrderSide.BUY \
                else (t.price >= order.price - 1e-12)
            if not hits:
                continue
            vol = t.size
            if order._queue_ahead > 0:                  # flow consumes the queue ahead of us first
                consumed = min(order._queue_ahead, vol)
                order._queue_ahead -= consumed
                vol -= consumed
            if vol > 0:
                fill = min(order.remaining_size, vol)
                order.fills.append(Fill(price=order.price, size=fill, ts=t.ts, taker=False))
        order._last_poll_ts = now
        order._refresh_status()
        return order

    async def cancel_order(self, order: LimitOrder) -> bool:
        if order.is_terminal:
            return False
        order.status = OrderStatus.CANCELLED
        return True


# ---------------------------------------------------------------------------------------------
# Live market data (real CLOB book + Data API trades) — used by SimulatedExecutor in production
# ---------------------------------------------------------------------------------------------
class LiveMarketData:
    """Real market data: full CLOB book + recent executed trades. Same source the rest of the bot
    uses, so the forward simulation runs against the true live tape."""
    CLOB_BOOK = "https://clob.polymarket.com/book"
    DATA_TRADES = "https://data-api.polymarket.com/trades"

    def __init__(self, client, condition_id_for: Optional[Dict[str, str]] = None):
        # ``client`` is an httpx.AsyncClient. The Data API /trades filters by market = conditionId,
        # so callers pass a token_id -> conditionId map (the WeatherMarket carries both).
        self.client = client
        self.condition_id_for = condition_id_for or {}

    async def book(self, token_id: str) -> Optional[BookSnapshot]:
        from backend.data.orderbook import _parse_levels
        try:
            r = await self.client.get(self.CLOB_BOOK, params={"token_id": token_id})
            if r.status_code != 200:
                return None
            d = r.json()
        except Exception as e:
            logger.debug(f"LiveMarketData.book failed for {token_id}: {e}")
            return None
        bids = _parse_levels(d.get("bids")); bids.sort(key=lambda x: -x[0])
        asks = _parse_levels(d.get("asks")); asks.sort(key=lambda x: x[0])
        if not bids and not asks:
            return None
        return BookSnapshot(bids=bids, asks=asks)

    async def trades_since(self, token_id: str, since_ts: float) -> List[TradePrint]:
        cond = self.condition_id_for.get(token_id)
        if not cond:
            return []
        try:
            r = await self.client.get(self.DATA_TRADES, params={"market": cond, "limit": 500},
                                      headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code >= 400:
                return []
            rows = r.json() or []
        except Exception:
            return []
        out = []
        for t in rows:
            try:
                ts = float(t["timestamp"])
                if ts <= since_ts:
                    continue
                # only this token's trades (an event's conditionId can span the binary pair)
                if str(t.get("asset")) != str(token_id):
                    continue
                out.append(TradePrint(price=float(t["price"]), size=float(t["size"]), ts=ts))
            except (KeyError, ValueError, TypeError):
                continue
        return out


# ---------------------------------------------------------------------------------------------
# Live Polymarket executor — the real thing, gated. Going live fills this in.
# ---------------------------------------------------------------------------------------------
class LivePolymarketExecutor(Executor):
    """Places REAL orders on Polymarket via py-clob-client. Same OrderArgs/TIF the simulator
    models, so swapping this for SimulatedExecutor changes nothing in the bot's calling code.

    HARD-GATED: refuses to act while SIMULATION_MODE is True, and lazy-imports py-clob-client so
    the module loads without it. To go live:
        1) pip install py-clob-client
        2) provide POLYMARKET creds (private key / API creds / funder address)
        3) set SIMULATION_MODE=False
    Until all three hold, every method raises a clear error — there is intentionally no way to
    fire a real order by accident.
    """

    def __init__(self, host: str = "https://clob.polymarket.com", private_key: Optional[str] = None,
                 funder: Optional[str] = None, chain_id: int = 137):
        self.host, self.private_key, self.funder, self.chain_id = host, private_key, funder, chain_id
        self._client = None

    def _guard(self):
        from backend.config import settings
        if settings.SIMULATION_MODE:
            raise RuntimeError("LivePolymarketExecutor blocked: SIMULATION_MODE is True. "
                               "This is the safety gate — flip it only when ready to trade real money.")
        if not self.private_key:
            raise RuntimeError("LivePolymarketExecutor needs a wallet private key (no creds supplied).")

    def _clob(self):
        self._guard()
        if self._client is None:
            try:
                from py_clob_client.client import ClobClient            # lazy: not a load-time dep
            except ImportError as e:
                raise RuntimeError("py-clob-client not installed — `pip install py-clob-client` to go live.") from e
            self._client = ClobClient(self.host, key=self.private_key, chain_id=self.chain_id,
                                      funder=self.funder, signature_type=2)
            self._client.set_api_creds(self._client.create_or_derive_api_creds())
        return self._client

    async def place_order(self, token_id, side, price, size, tif=TimeInForce.GTC,
                          ttl_seconds=None) -> LimitOrder:
        client = self._clob()
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL
        now = time.time()
        expiration = int(_gtd_expiration(now, ttl_seconds)) if ttl_seconds is not None else 0
        args = OrderArgs(price=round(float(price), 3), size=float(size),
                         side=(BUY if OrderSide(side) == OrderSide.BUY else SELL),
                         token_id=token_id, expiration=expiration)
        signed = client.create_order(args)
        otype = OrderType.GTD if ttl_seconds is not None else (
            OrderType.GTC if tif == TimeInForce.GTC else
            OrderType.FOK if tif == TimeInForce.FOK else OrderType.FAK)
        resp = client.post_order(signed, otype)
        order = LimitOrder(token_id=token_id, side=OrderSide(side), price=float(price),
                           size=float(size), tif=tif, created_ts=now,
                           expiration_ts=(expiration or None),
                           order_id=resp.get("orderID") or resp.get("orderId") or f"live-{uuid.uuid4().hex[:12]}")
        if not resp.get("success", True):
            order.status = OrderStatus.REJECTED
        return order

    async def poll_order(self, order: LimitOrder) -> LimitOrder:
        client = self._clob()
        data = client.get_order(order.order_id)        # real status + fills from the CLOB
        # map the live size_matched/status onto our model (fields per py-clob-client get_order)
        matched = float(data.get("size_matched", 0) or 0)
        if matched > order.filled_size:
            order.fills.append(Fill(price=order.price, size=matched - order.filled_size,
                                    ts=time.time(), taker=False))
        st = (data.get("status") or "").upper()
        if st in ("MATCHED", "FILLED") or order.remaining_size <= 1e-9:
            order.status = OrderStatus.FILLED
        elif st in ("CANCELED", "CANCELLED"):
            order.status = OrderStatus.CANCELLED
        else:
            order._refresh_status()
        return order

    async def cancel_order(self, order: LimitOrder) -> bool:
        client = self._clob()
        resp = client.cancel(order.order_id)
        ok = order.order_id in (resp.get("canceled") or [])
        if ok:
            order.status = OrderStatus.CANCELLED
        return ok


# ---------------------------------------------------------------------------------------------
# Order manager — tracks open orders, drives polling + the expiry/auto-cancel backstop
# ---------------------------------------------------------------------------------------------
class OrderManager:
    """Owns the lifecycle of working orders: submit, poll-all (advancing fills + expiring GTD),
    and an explicit auto-cancel backstop for any TIF (so 'cancel if unfilled after N seconds'
    holds even on GTC). A scan/settlement job calls ``poll_all`` on a fast cadence while orders
    are open. Backend-agnostic — works with the sim now and the live executor later."""

    def __init__(self, executor: Executor, clock=time.time):
        self.executor = executor
        self.clock = clock
        self.open: Dict[str, LimitOrder] = {}
        self.done: List[LimitOrder] = []

    async def submit_limit(self, token_id, side, price, cash=None, size=None,
                           ttl_seconds: Optional[float] = None) -> LimitOrder:
        """Place a limit order sized either by ``cash`` (converted to shares at ``price``) or an
        explicit ``size`` in shares. ``ttl_seconds`` -> GTD auto-expiry (the cancel-after-time
        feature); omit for GTC."""
        if size is None:
            if cash is None:
                raise ValueError("submit_limit needs cash or size")
            size = shares_for_cash(cash, price)
        tif = TimeInForce.GTD if ttl_seconds is not None else TimeInForce.GTC
        order = await self.executor.place_order(token_id, OrderSide(side), price, size,
                                                tif=tif, ttl_seconds=ttl_seconds)
        if order.is_terminal:
            self.done.append(order)
        else:
            self.open[order.order_id] = order
        return order

    async def poll_all(self, hard_ttl_seconds: Optional[float] = None) -> None:
        """Advance every open order. Auto-cancels any order older than ``hard_ttl_seconds`` (a
        TIF-independent backstop, e.g. to cancel GTC orders too), in addition to GTD expiry."""
        now = self.clock()
        for oid, order in list(self.open.items()):
            if hard_ttl_seconds is not None and (now - order.created_ts) >= hard_ttl_seconds \
                    and not order.is_terminal:
                await self.executor.cancel_order(order)
            else:
                await self.executor.poll_order(order)
            if order.is_terminal:
                self.done.append(order)
                del self.open[oid]

    async def cancel_all(self) -> None:
        for oid, order in list(self.open.items()):
            await self.executor.cancel_order(order)
            self.done.append(order)
            del self.open[oid]
