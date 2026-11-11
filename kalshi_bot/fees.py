"""Kalshi taker fees: quadratic in price, about 1.75c per contract at 0.50.

    fee = ceil_to_cent(0.07 * contracts * price * (1 - price))

Rounded up per order, not per contract. Some series carry their own
multiplier; the standard schedule (1) is assumed here.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

RATE = Decimal("0.07")
CENT = Decimal("0.01")


def fee_per_contract(price: Decimal) -> Decimal:
    """Unrounded, for comparing a saving at the margin."""
    return RATE * price * (1 - price)


def order_fee(price: Decimal, contracts: int) -> Decimal:
    if contracts <= 0:
        return Decimal("0.00")
    return (RATE * contracts * price * (1 - price)).quantize(CENT, rounding=ROUND_CEILING)


def order_cost(price: Decimal, contracts: int) -> Decimal:
    """What a buy costs all in: contracts at `price` plus the fee."""
    return price * contracts + order_fee(price, contracts)
