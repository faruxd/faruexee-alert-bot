"""
The pessimistic fill model.

This is the most important part of the backtester. An optimistic fill model does
not produce a slightly-too-good result, it produces a result that is entirely
fictional -- because the trades it claims to have taken were never available.

Three rules, each of which exists to kill a specific way of lying to yourself:

1. A post-only limit at price P fills ONLY if a LATER bar trades THROUGH P.
   Not the signal bar -- the signal bar's close IS P, so counting it would fill
   every single order for free. And "through", not "touched": a bar whose low
   exactly equals your bid does not prove your bid was reached, because you were
   at the back of the queue.

2. Stops fill at the trigger price plus 3 bps of ADVERSE slippage. A stop is a
   market order fired into the move that triggered it; it does not fill at the
   trigger.

3. When a bar could have hit both the stop and the target, the STOP is taken.
   Bar data cannot tell you which came first, and assuming the good one is how
   backtests manufacture edge.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from cf_bot.strategy import Bar

# Verified against Bitget's published VIP 0 USDT-perpetual schedule.
# Do not change these without re-checking the live fee page.
MAKER_FEE = Decimal("0.0002")  # 0.020%
TAKER_FEE = Decimal("0.0006")  # 0.060%

# Adverse slippage applied to every stop fill.
STOP_SLIPPAGE_BPS = Decimal("3")
BPS = Decimal("10000")


@dataclass(frozen=True)
class EntryFill:
    bar_index: int
    timestamp_ms: int
    price: Decimal


def entry_fills_on_bar(bar: Bar, side: str, limit_price: Decimal) -> bool:
    """
    Did this bar trade THROUGH our resting post-only limit?

    Long  -> we are bidding at limit_price; requires low  <  limit_price.
    Short -> we are offering at limit_price; requires high >  limit_price.

    Strict inequality on purpose. A touch is not a fill.
    """
    if side == "long":
        return bar.low < limit_price
    if side == "short":
        return bar.high > limit_price
    raise ValueError(f"unknown side {side!r}")


def find_entry_fill(
    bars: list[Bar],
    signal_index: int,
    side: str,
    limit_price: Decimal,
    valid_bars: int,
) -> Optional[EntryFill]:
    """
    Scan bars signal_index+1 .. signal_index+valid_bars for a fill.

    The scan STARTS at signal_index+1. The signal bar itself can never fill the
    order, because its close is the order's price and it has already happened.
    """
    last_index = min(signal_index + valid_bars, len(bars) - 1)

    for index in range(signal_index + 1, last_index + 1):
        bar = bars[index]
        if entry_fills_on_bar(bar, side, limit_price):
            # We filled at our limit price, not better. A post-only order that
            # gets swept does not get price improvement.
            return EntryFill(bar_index=index, timestamp_ms=bar.timestamp_ms, price=limit_price)

    return None


def stop_fill_price(side: str, stop_price: Decimal) -> Decimal:
    """Trigger price plus 3 bps against us."""
    slip = stop_price * STOP_SLIPPAGE_BPS / BPS
    if side == "long":
        return stop_price - slip  # long stop sells lower than trigger
    if side == "short":
        return stop_price + slip  # short stop buys higher than trigger
    raise ValueError(f"unknown side {side!r}")


def stop_hit(bar: Bar, side: str, stop_price: Decimal) -> bool:
    if side == "long":
        return bar.low <= stop_price
    if side == "short":
        return bar.high >= stop_price
    raise ValueError(f"unknown side {side!r}")


def target_hit(bar: Bar, side: str, target_price: Decimal) -> bool:
    """
    Strict inequality: the target is a resting limit, so price must trade
    through it, same standard as the entry.
    """
    if side == "long":
        return bar.high > target_price
    if side == "short":
        return bar.low < target_price
    raise ValueError(f"unknown side {side!r}")


def gross_pnl(side: str, entry_price: Decimal, exit_price: Decimal, qty: Decimal) -> Decimal:
    if side == "long":
        return (exit_price - entry_price) * qty
    if side == "short":
        return (entry_price - exit_price) * qty
    raise ValueError(f"unknown side {side!r}")


def fee_for(notional: Decimal, is_maker: bool) -> Decimal:
    return notional * (MAKER_FEE if is_maker else TAKER_FEE)
