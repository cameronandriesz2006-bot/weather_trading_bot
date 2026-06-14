"""Exact order-book fills for Polymarket CLOB markets.

The signal generator used to assume it could fill an entire order at the
top-of-book ask. Real markets don't work that way: a marketable buy order
*walks the book*, consuming each price level until it's filled, so the average
price you actually pay (the VWAP) is worse than the best ask — badly so on the
thin weather buckets (e.g. a $75 buy can drag a 6c best-ask up to a ~19c VWAP).

This module fetches the live CLOB order book for an outcome token and simulates
exactly that walk, returning the precise fill — no heuristic, no modelled
slippage curve. It is what would actually happen against the book as quoted.

Polymarket buy market orders are denominated in USDC (you specify how much cash
to spend), so the primary helper walks the asks for a target cash amount.
"""
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger("trading_bot")

CLOB_BOOK_URL = "https://clob.polymarket.com/book"
CLOB_BOOKS_URL = "https://clob.polymarket.com/books"   # batch: POST [{"token_id":...}]


@dataclass
class FillResult:
    """The exact outcome of walking the ask side of a book for a cash budget."""
    contracts: float        # shares acquired
    cash: float             # USDC actually spent
    vwap: float             # volume-weighted avg price paid (cash / contracts)
    requested_cash: float   # cash we wanted to spend
    fully_filled: bool      # did the book have enough depth for the full request?
    levels: int             # number of price levels consumed
    best_ask: float         # cheapest ask (top of book) for reference
    book_depth_cash: float  # total USDC to sweep the entire ask side (depth proxy)


@dataclass
class BookTop:
    """Top of book for a single outcome token, from the live CLOB."""
    best_bid: Optional[float]   # highest bid (what you could sell into now)
    best_ask: Optional[float]   # lowest ask (what you'd pay to buy now)
    mid: float                  # (bid+ask)/2, or the one side present


@dataclass
class LiveBook:
    """A token's full live book: top-of-book plus the ask ladder for walking."""
    top: BookTop
    asks: List[Tuple[float, float]]   # (price, size) sorted cheapest-first


def _top_from_levels(bids, asks) -> Optional[BookTop]:
    """Build a BookTop from raw bid/ask level lists, or None if both empty."""
    best_bid = max((p for p, _ in _parse_levels(bids)), default=None)
    best_ask = min((p for p, _ in _parse_levels(asks)), default=None)
    if best_bid is not None and best_ask is not None:
        mid = (best_bid + best_ask) / 2
    elif best_bid is not None:
        mid = best_bid
    elif best_ask is not None:
        mid = best_ask
    else:
        return None
    return BookTop(best_bid=best_bid, best_ask=best_ask, mid=mid)


async def fetch_book_top(
    token_id: str, client: httpx.AsyncClient
) -> Optional[BookTop]:
    """
    Fetch the live top of book (best bid/ask + mid) for a single CLOB token.

    Use this for mark-to-market display: Gamma's cached ``outcomePrices`` /
    ``bestBid`` / ``bestAsk`` fields can be badly stale on thin daily-temperature
    markets (observed ~20c off), so the live CLOB book is the only trustworthy
    "current price". Returns None if the book can't be read or is empty on both
    sides, so callers can fall back. For many tokens at once use
    ``fetch_books`` (one batched request) instead of looping this.
    """
    if not token_id:
        return None
    try:
        r = await client.get(CLOB_BOOK_URL, params={"token_id": token_id})
        if r.status_code != 200:
            return None
        data = r.json()
        return _top_from_levels(data.get("bids"), data.get("asks"))
    except Exception as e:
        logger.debug(f"Order-book top fetch failed for token {token_id}: {e}")
        return None


async def fetch_books(
    token_ids: List[str], client: httpx.AsyncClient, chunk_size: int = 200
) -> Dict[str, LiveBook]:
    """
    Fetch FULL live books for MANY tokens via the CLOB batch endpoint.

    Looping the single-token endpoint over hundreds of tokens is ~one HTTP
    round-trip each and gets rate-limited (a full weather scan measured ~50s).
    ``POST /books`` returns all requested books in one response (keyed by
    ``asset_id``), each with the complete bid/ask ladders — so a whole scan's
    worth of tokens is a handful of requests. The returned ``LiveBook`` carries
    both the top-of-book (for the edge screen / mark-to-market) AND the sorted ask
    ladder (for the exact fill walk), so a scan needs NO per-candidate fetches.
    Tokens whose book is missing/empty are simply absent, so callers fall back to
    their existing (Gamma) values.
    """
    out: Dict[str, LiveBook] = {}
    ids = [t for t in token_ids if t]
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i:i + chunk_size]
        try:
            r = await client.post(CLOB_BOOKS_URL, json=[{"token_id": t} for t in chunk])
            if r.status_code != 200:
                logger.debug(f"Batch /books returned {r.status_code} for {len(chunk)} tokens")
                continue
            for b in r.json() or []:
                tid = b.get("asset_id")
                if not tid:
                    continue
                top = _top_from_levels(b.get("bids"), b.get("asks"))
                if top is None:
                    continue
                asks = _parse_levels(b.get("asks"))
                asks.sort(key=lambda x: x[0])   # cheapest first — fill order
                out[tid] = LiveBook(top=top, asks=asks)
        except Exception as e:
            logger.debug(f"Batch book fetch failed for a chunk of {len(chunk)}: {e}")
            continue
    return out


def _parse_levels(raw) -> List[Tuple[float, float]]:
    """Parse [{'price','size'}, ...] into [(price, size)], dropping junk."""
    out: List[Tuple[float, float]] = []
    for lvl in raw or []:
        try:
            price = float(lvl["price"])
            size = float(lvl["size"])
        except (KeyError, ValueError, TypeError):
            continue
        if price > 0 and size > 0:
            out.append((price, size))
    return out


async def fetch_ask_levels(
    token_id: str, client: httpx.AsyncClient
) -> Optional[List[Tuple[float, float]]]:
    """
    Fetch the ASK side of a CLOB token's book, sorted cheapest-first.

    Returns None on any failure (missing token, network error, empty book) so
    callers can fall back to the top-of-book estimate without breaking the scan.
    The CLOB returns asks in descending price order, so we sort ascending here.
    """
    if not token_id:
        return None
    try:
        r = await client.get(CLOB_BOOK_URL, params={"token_id": token_id})
        if r.status_code != 200:
            return None
        asks = _parse_levels(r.json().get("asks"))
        if not asks:
            return None
        asks.sort(key=lambda x: x[0])  # cheapest ask first — the order a buy fills in
        return asks
    except Exception as e:
        logger.debug(f"Order-book fetch failed for token {token_id}: {e}")
        return None


def walk_asks_for_cash(
    asks: List[Tuple[float, float]], requested_cash: float
) -> Optional[FillResult]:
    """
    Simulate a marketable buy of ``requested_cash`` USDC against ``asks``.

    Consumes levels cheapest-first (a real market buy), spending cash until the
    budget is exhausted or the book runs dry. Returns the exact VWAP and the
    number of contracts acquired. None if there's nothing to fill.
    """
    if not asks or requested_cash <= 0:
        return None

    depth_cash = sum(price * size for price, size in asks)
    best_ask = asks[0][0]

    spent = 0.0
    contracts = 0.0
    levels = 0
    for price, size in asks:
        if spent >= requested_cash:
            break
        level_cash = price * size
        remaining = requested_cash - spent
        if level_cash <= remaining:
            spent += level_cash
            contracts += size
        else:
            # Partial consumption of this level: buy only what the budget allows.
            contracts += remaining / price
            spent += remaining
        levels += 1

    if contracts <= 0:
        return None

    return FillResult(
        contracts=contracts,
        cash=spent,
        vwap=spent / contracts,
        requested_cash=requested_cash,
        fully_filled=spent >= requested_cash - 1e-9,
        levels=levels,
        best_ask=best_ask,
        book_depth_cash=depth_cash,
    )


async def simulate_market_buy(
    token_id: str,
    requested_cash: float,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[FillResult]:
    """
    Fetch the live book for ``token_id`` and walk it for ``requested_cash``.

    Convenience wrapper combining fetch + walk. Opens its own short-lived client
    if one isn't supplied. Returns None if the book can't be fetched/filled.
    """
    if not token_id or requested_cash <= 0:
        return None

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=8.0)
    try:
        asks = await fetch_ask_levels(token_id, client)
        if not asks:
            return None
        return walk_asks_for_cash(asks, requested_cash)
    finally:
        if own_client:
            await client.aclose()
