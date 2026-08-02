"""
The pessimistic fill model.

These are the tests that stop the backtester from lying. Each one pins a rule
whose absence would manufacture edge that does not exist.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from backtest.fills import (
    MAKER_FEE,
    STOP_SLIPPAGE_BPS,
    TAKER_FEE,
    entry_fills_on_bar,
    fee_for,
    find_entry_fill,
    gross_pnl,
    stop_fill_price,
    stop_hit,
    target_hit,
)
from cf_bot.strategy import BAR_MS_5M, Bar


def bar(index, open_, high, low, close) -> Bar:
    return Bar(
        timestamp_ms=index * BAR_MS_5M,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("1"),
    )


# --- the signal bar can never fill its own order ---------------------------


def test_signal_bar_never_fills_the_entry():
    """
    The single most important rule.

    The order price IS the signal bar's close, so counting that bar would fill
    every order for free and produce a fantasy equity curve.
    """
    signal = bar(0, "100", "106", "100", "106")
    # Later bars never revisit 106.
    later = [bar(i, "105", "105.5", "104", "105") for i in range(1, 5)]
    bars = [signal] + later

    assert find_entry_fill(bars, 0, "short", Decimal("106"), valid_bars=3) is None


def test_a_later_bar_trading_through_fills():
    signal = bar(0, "100", "106", "100", "106")
    bars = [signal, bar(1, "105", "107", "104", "105")]  # high 107 > 106
    fill = find_entry_fill(bars, 0, "short", Decimal("106"), valid_bars=3)

    assert fill is not None
    assert fill.bar_index == 1
    assert fill.price == Decimal("106")  # no price improvement


def test_a_touch_is_not_a_fill():
    """high == limit means you were at the back of the queue."""
    signal = bar(0, "100", "106", "100", "106")
    bars = [signal, bar(1, "105", "106", "104", "105")]  # exactly touches
    assert find_entry_fill(bars, 0, "short", Decimal("106"), valid_bars=3) is None


def test_entry_expires_after_the_validity_window():
    signal = bar(0, "100", "106", "100", "106")
    # The through-trade happens on bar 5, outside the 3-bar window.
    bars = [signal] + [bar(i, "105", "105.5", "104", "105") for i in range(1, 5)]
    bars.append(bar(5, "105", "110", "104", "109"))

    assert find_entry_fill(bars, 0, "short", Decimal("106"), valid_bars=3) is None


def test_fill_is_found_on_the_last_valid_bar():
    signal = bar(0, "100", "106", "100", "106")
    bars = [signal] + [bar(i, "105", "105.5", "104", "105") for i in range(1, 3)]
    bars.append(bar(3, "105", "108", "104", "107"))  # bar 3 == signal+3
    fill = find_entry_fill(bars, 0, "short", Decimal("106"), valid_bars=3)
    assert fill is not None and fill.bar_index == 3


def test_long_entry_requires_trading_below_the_bid():
    assert entry_fills_on_bar(bar(1, "95", "96", "93", "95"), "long", Decimal("94")) is True
    assert entry_fills_on_bar(bar(1, "95", "96", "94", "95"), "long", Decimal("94")) is False


# --- stops -----------------------------------------------------------------


def test_long_stop_fills_below_the_trigger():
    """A stop is a market order fired into the move; it does not fill at trigger."""
    price = stop_fill_price("long", Decimal("100"))
    assert price < Decimal("100")
    assert price == Decimal("100") - Decimal("100") * STOP_SLIPPAGE_BPS / Decimal("10000")


def test_short_stop_fills_above_the_trigger():
    price = stop_fill_price("short", Decimal("100"))
    assert price > Decimal("100")


def test_slippage_is_three_basis_points():
    assert STOP_SLIPPAGE_BPS == Decimal("3")
    assert stop_fill_price("long", Decimal("10000")) == Decimal("9997")


def test_stop_triggers_on_a_touch():
    """Unlike a limit, a stop triggers when price reaches it."""
    assert stop_hit(bar(1, "100", "101", "95", "96"), "long", Decimal("95")) is True
    assert stop_hit(bar(1, "100", "101", "96", "97"), "long", Decimal("95")) is False


def test_short_stop_triggers_above():
    assert stop_hit(bar(1, "100", "105", "99", "104"), "short", Decimal("105")) is True


# --- targets ---------------------------------------------------------------


def test_target_requires_trading_through():
    assert target_hit(bar(1, "100", "106", "99", "105"), "long", Decimal("105")) is True
    assert target_hit(bar(1, "100", "105", "99", "104"), "long", Decimal("105")) is False


# --- fees ------------------------------------------------------------------


def test_fee_rates_match_bitgets_published_schedule():
    """Verified against Bitget's VIP 0 USDT-perpetual schedule."""
    assert MAKER_FEE == Decimal("0.0002")  # 0.020%
    assert TAKER_FEE == Decimal("0.0006")  # 0.060%


def test_taker_costs_three_times_maker():
    notional = Decimal("10000")
    assert fee_for(notional, is_maker=False) == fee_for(notional, is_maker=True) * 3


# --- pnl -------------------------------------------------------------------


def test_long_pnl():
    assert gross_pnl("long", Decimal("100"), Decimal("110"), Decimal("2")) == Decimal("20")


def test_short_pnl():
    assert gross_pnl("short", Decimal("100"), Decimal("90"), Decimal("2")) == Decimal("20")


def test_short_loses_when_price_rises():
    assert gross_pnl("short", Decimal("100"), Decimal("110"), Decimal("2")) == Decimal("-20")


@pytest.mark.parametrize("bad", ["", "buy", "up"])
def test_unknown_side_is_rejected_everywhere(bad):
    with pytest.raises(ValueError):
        gross_pnl(bad, Decimal("1"), Decimal("2"), Decimal("1"))
    with pytest.raises(ValueError):
        stop_fill_price(bad, Decimal("1"))
    with pytest.raises(ValueError):
        stop_hit(bar(1, "1", "1", "1", "1"), bad, Decimal("1"))
