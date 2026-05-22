#!/usr/bin/env python3
from __future__ import annotations

from decimal import Decimal

MIN_TICK = -887272
MAX_TICK = 887272

_TICK_SPACING_BY_FEE: dict[int, int] = {
    100: 1,
    500: 10,
    3000: 60,
    10000: 200,
}


def fee_tier_to_tick_spacing(fee: int) -> int:
    if fee not in _TICK_SPACING_BY_FEE:
        raise ValueError(f"unknown fee tier {fee}; valid: {sorted(_TICK_SPACING_BY_FEE)}")
    return _TICK_SPACING_BY_FEE[fee]


def nearest_usable_tick(tick: int, tick_spacing: int) -> int:
    if tick_spacing <= 0:
        raise ValueError(f"tick_spacing must be positive, got {tick_spacing}")
    rounded = round(tick / tick_spacing) * tick_spacing
    return max(MIN_TICK, min(MAX_TICK, rounded))


def price_to_tick(price: float, decimals0: int, decimals1: int) -> int:
    if price <= 0:
        raise ValueError("price must be positive")
    adjusted = Decimal(str(price)) * (Decimal(10) ** (decimals0 - decimals1))
    tick = int(adjusted.ln() / Decimal("1.0001").ln())
    return max(MIN_TICK, min(MAX_TICK, tick))


def tick_to_price(tick: int, decimals0: int, decimals1: int) -> float:
    adjusted = Decimal("1.0001") ** tick
    price = adjusted * (Decimal(10) ** (decimals1 - decimals0))
    return float(price)


def tick_to_sqrt_price_x96(tick: int) -> int:
    adjusted = Decimal("1.0001") ** tick
    sqrt_price = adjusted.sqrt()
    return int(sqrt_price * (Decimal(2) ** 96))


def suggest_ticks_for_range(
    current_tick: int,
    tick_spacing: int,
    price_lower: float | None = None,
    price_upper: float | None = None,
    decimals0: int = 18,
    decimals1: int = 18,
) -> tuple[int, int]:
    if price_lower is not None and price_upper is not None:
        lower = price_to_tick(price_lower, decimals0, decimals1)
        upper = price_to_tick(price_upper, decimals0, decimals1)
    elif price_lower is not None:
        lower = price_to_tick(price_lower, decimals0, decimals1)
        upper = MAX_TICK
    elif price_upper is not None:
        lower = MIN_TICK
        upper = price_to_tick(price_upper, decimals0, decimals1)
    else:
        return (
            nearest_usable_tick(MIN_TICK, tick_spacing),
            nearest_usable_tick(MAX_TICK, tick_spacing),
        )
    return (
        nearest_usable_tick(lower, tick_spacing),
        nearest_usable_tick(upper, tick_spacing),
    )
