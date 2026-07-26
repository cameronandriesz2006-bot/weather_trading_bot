"""Shared edge and position-sizing math.

These two helpers are strategy-agnostic and are used by the weather signal
generator. They were extracted from the (now removed) BTC signal module so the
weather path no longer depends on crypto code.

Direction convention: "up" = first outcome (Yes), "down" = second outcome (No).
"""
from backend.config import settings


def calculate_edge(
    model_prob: float,
    market_price: float
) -> tuple[float, str]:
    """
    Calculate edge and determine direction.

    - "up" is equivalent to "yes" (outcomePrices[0])
    - "down" is equivalent to "no" (outcomePrices[1])

    Returns:
        (edge, direction) where direction is "up" or "down"
    """
    # Edge for UP bet
    up_edge = model_prob - market_price

    # Edge for DOWN bet
    down_edge = (1 - model_prob) - (1 - market_price)

    if up_edge >= down_edge:
        return up_edge, "up"
    else:
        return down_edge, "down"


def calculate_kelly_size(
    edge: float,
    probability: float,
    market_price: float,
    direction: str,
    bankroll: float
) -> float:
    """
    Calculate position size using fractional Kelly criterion.

    Kelly formula: f = (p * b - q) / b
    where:
        f = fraction of bankroll to bet
        p = probability of winning
        q = probability of losing (1 - p)
        b = odds (payout ratio)
    """
    if direction == "up":
        win_prob = probability
        price = market_price
    else:
        win_prob = 1 - probability
        price = 1 - market_price

    if price <= 0 or price >= 1:
        return 0

    odds = (1 - price) / price

    lose_prob = 1 - win_prob
    kelly = (win_prob * odds - lose_prob) / odds

    # Apply fractional Kelly
    kelly *= settings.KELLY_FRACTION

    # Per-trade ceiling as a fraction of bankroll (relative, so it scales at any
    # bankroll). This is the ONLY per-trade cap now: it lets a bigger-edge / more-
    # confident bet take a genuinely bigger stake and clips only the strongest at the
    # ceiling — instead of the old fixed-dollar cap that flattened every bet to one size.
    kelly = min(kelly, settings.KELLY_MAX_TRADE_FRACTION)
    kelly = max(kelly, 0)

    return kelly * bankroll


def taker_fee_per_share(price: float, rate: float | None = None) -> float:
    """Polymarket taker fee for ONE share bought at ``price``, in price units.

    Polymarket's weather markets charge ``fee = shares * rate * p * (1-p)`` to the TAKER only
    (``feeType: "weather_fees"``, rate 0.05, ``takerOnly: true``; makers pay nothing and receive
    a 25% rebate). Because the result is per-share and in the same units as a price, it can be
    subtracted directly from a per-share edge — which is exactly how the signal path uses it.

    The p*(1-p) shape is the whole point and is why the old flat ``WEATHER_FEE_RATE`` could not
    represent this: the fee peaks at p=0.50 (1.25c/share = 2.5% of notional) and vanishes at both
    tails (0.05c/share = 0.05% of notional at p=0.99). It is therefore largest precisely where
    the book is loose and an apparent edge is most likely to be a mirage.
    """
    if rate is None:
        rate = settings.WEATHER_TAKER_FEE_RATE
    if rate <= 0:
        return 0.0
    p = min(max(price, 0.0), 1.0)
    return rate * p * (1.0 - p)


def taker_fee_on_cash(cash: float, price: float, rate: float | None = None) -> float:
    """Polymarket taker fee in DOLLARS for spending ``cash`` USDC at ``price`` per share.

    ``shares = cash / price``, so the per-share fee ``rate * p * (1-p)`` collapses to
    ``cash * rate * (1-p)`` — the fee as a fraction of notional is ``rate * (1-p)``, falling
    linearly as the price rises. Use this for booking the fee on a trade; use
    ``taker_fee_per_share`` for gating and sizing on net edge.
    """
    if rate is None:
        rate = settings.WEATHER_TAKER_FEE_RATE
    if rate <= 0 or cash <= 0:
        return 0.0
    p = min(max(price, 1e-9), 1.0)
    return cash * rate * (1.0 - p)
