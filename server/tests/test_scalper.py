"""
The EMA scalper, driven by handcrafted bars.

Pure module: no network, no clock, no exchange.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from cf_bot.scalper import (
    BAR_MS_5M,
    MIN_SIGNAL_BARS,
    MIN_TREND_BARS,
    TIME_STOP_BARS,
    ScalperParams,
    cross_direction,
    ema_series,
    evaluate,
    trend_direction,
)
from cf_bot.strategy import Bar, StrategyError


def bar(index: int, close: Decimal, spread: Decimal = Decimal("1"), tf_ms=BAR_MS_5M) -> Bar:
    return Bar(
        timestamp_ms=index * tf_ms,
        open=close,
        high=close + spread,
        low=close - spread,
        close=close,
        volume=Decimal("10"),
    )


def ramp(count: int, start: Decimal, step: Decimal, tf_ms=BAR_MS_5M) -> list[Bar]:
    """A steadily trending series."""
    return [bar(i, start + step * Decimal(i), tf_ms=tf_ms) for i in range(count)]


def flat(count: int, price: Decimal = Decimal("100"), tf_ms=BAR_MS_5M) -> list[Bar]:
    return [bar(i, price, tf_ms=tf_ms) for i in range(count)]


@pytest.fixture
def params() -> ScalperParams:
    return ScalperParams()


# --- EMA -------------------------------------------------------------------


def test_ema_is_none_until_seeded():
    emas = ema_series([Decimal(n) for n in range(10)], period=5)
    assert emas[:4] == [None] * 4
    assert emas[4] is not None


def test_ema_seed_is_the_simple_average():
    values = [Decimal("10")] * 5
    assert ema_series(values, 5)[4] == Decimal("10")


def test_ema_of_a_constant_series_is_that_constant():
    emas = ema_series([Decimal("50")] * 40, period=9)
    assert emas[-1] == Decimal("50")


def test_ema_tracks_a_rising_series_from_below():
    values = [Decimal(n) for n in range(1, 60)]
    emas = ema_series(values, period=9)
    assert emas[-1] < values[-1]


def test_faster_ema_tracks_more_closely():
    values = [Decimal(n) for n in range(1, 60)]
    fast = ema_series(values, 5)[-1]
    slow = ema_series(values, 30)[-1]
    assert fast > slow


def test_ema_is_index_aligned():
    values = [Decimal(n) for n in range(50)]
    assert len(ema_series(values, 9)) == len(values)


def test_ema_rejects_a_bad_period():
    with pytest.raises(StrategyError):
        ema_series([Decimal("1")], 0)


def test_ema_of_a_too_short_series_is_all_none():
    assert ema_series([Decimal("1"), Decimal("2")], period=9) == [None, None]


# --- trend filter ----------------------------------------------------------


def test_price_above_trend_ema_is_long():
    assert trend_direction(ramp(MIN_TREND_BARS + 20, Decimal("100"), Decimal("1")), 50) == "long"


def test_price_below_trend_ema_is_short():
    bars = ramp(MIN_TREND_BARS + 20, Decimal("500"), Decimal("-1"))
    assert trend_direction(bars, 50) == "short"


def test_insufficient_trend_history_fails_closed():
    """
    Unknown trend means the system is OFF, not unconstrained.
    """
    assert trend_direction(flat(10), 50) is None


def test_flat_price_gives_no_trend_opinion():
    assert trend_direction(flat(MIN_TREND_BARS + 20), 50) is None


# --- cross detection -------------------------------------------------------


def _crossing_up_series() -> list[Bar]:
    """Long decline, then a sharp reversal that pulls the fast EMA up through the slow."""
    down = ramp(MIN_SIGNAL_BARS + 20, Decimal("200"), Decimal("-1"))
    last = down[-1].close
    up = [
        bar(len(down) + i, last + Decimal("8") * Decimal(i + 1))
        for i in range(12)
    ]
    return down + up


def test_upward_cross_is_detected():
    bars = _crossing_up_series()
    # Walk forward to the exact bar where the cross happens.
    found = None
    for end in range(MIN_SIGNAL_BARS + 20, len(bars) + 1):
        if cross_direction(bars[:end], 9, 21) == "long":
            found = end
            break
    assert found is not None, "no upward cross detected in a clearly reversing series"


def test_cross_fires_only_on_the_bar_it_happens():
    """
    A cross three bars ago is already priced in. Entering on it is chasing.
    """
    bars = _crossing_up_series()
    cross_at = None
    for end in range(MIN_SIGNAL_BARS + 20, len(bars) + 1):
        if cross_direction(bars[:end], 9, 21) == "long":
            cross_at = end
            break
    assert cross_at is not None

    # The very next bar must NOT re-report the same cross.
    assert cross_direction(bars[: cross_at + 1], 9, 21) is None


def test_no_cross_in_a_steady_trend():
    assert cross_direction(ramp(MIN_SIGNAL_BARS + 40, Decimal("100"), Decimal("1")), 9, 21) is None


def test_no_cross_with_insufficient_history():
    assert cross_direction(flat(10), 9, 21) is None


# --- evaluate --------------------------------------------------------------


def test_no_signal_when_trend_and_cross_disagree(params):
    """
    A long cross inside a downtrend is exactly the whipsaw the 15m filter exists
    to reject.
    """
    signal_bars = _crossing_up_series()  # produces a LONG cross
    trend_bars = ramp(MIN_TREND_BARS + 20, Decimal("500"), Decimal("-1"), tf_ms=BAR_MS_5M * 3)

    for end in range(MIN_SIGNAL_BARS + 20, len(signal_bars) + 1):
        assert (
            evaluate("BTC/USDT:USDT", signal_bars[:end], trend_bars, params, False) is None
        )


def test_signal_when_trend_and_cross_agree(params):
    signal_bars = _crossing_up_series()
    trend_bars = ramp(MIN_TREND_BARS + 20, Decimal("100"), Decimal("1"), tf_ms=BAR_MS_5M * 3)

    signal = None
    for end in range(MIN_SIGNAL_BARS + 20, len(signal_bars) + 1):
        signal = evaluate("BTC/USDT:USDT", signal_bars[:end], trend_bars, params, False)
        if signal is not None:
            break

    assert signal is not None
    assert signal.side == "long"


def test_signal_geometry_is_correct(params):
    signal_bars = _crossing_up_series()
    trend_bars = ramp(MIN_TREND_BARS + 20, Decimal("100"), Decimal("1"), tf_ms=BAR_MS_5M * 3)

    signal = None
    for end in range(MIN_SIGNAL_BARS + 20, len(signal_bars) + 1):
        signal = evaluate("BTC/USDT:USDT", signal_bars[:end], trend_bars, params, False)
        if signal is not None:
            break
    assert signal is not None

    # Long: stop below entry, target above, and target exactly target_r x risk.
    assert signal.stop_price < signal.entry_price
    assert signal.target_price > signal.entry_price

    risk = signal.entry_price - signal.stop_price
    reward = signal.target_price - signal.entry_price
    assert reward == risk * params.target_r
    assert signal.risk_per_unit == risk


def test_stop_distance_follows_atr_mult():
    signal_bars = _crossing_up_series()
    trend_bars = ramp(MIN_TREND_BARS + 20, Decimal("100"), Decimal("1"), tf_ms=BAR_MS_5M * 3)

    def first_signal(mult):
        p = ScalperParams(atr_mult=Decimal(mult))
        for end in range(MIN_SIGNAL_BARS + 20, len(signal_bars) + 1):
            s = evaluate("BTC/USDT:USDT", signal_bars[:end], trend_bars, p, False)
            if s is not None:
                return s
        return None

    tight = first_signal("1.0")
    wide = first_signal("2.0")
    assert tight is not None and wide is not None
    assert wide.risk_per_unit == tight.risk_per_unit * 2


def test_settlement_blackout_suppresses_the_signal(params):
    signal_bars = _crossing_up_series()
    trend_bars = ramp(MIN_TREND_BARS + 20, Decimal("100"), Decimal("1"), tf_ms=BAR_MS_5M * 3)

    for end in range(MIN_SIGNAL_BARS + 20, len(signal_bars) + 1):
        assert evaluate("BTC/USDT:USDT", signal_bars[:end], trend_bars, params, True) is None


def test_no_signal_without_enough_signal_history(params):
    trend_bars = ramp(MIN_TREND_BARS + 20, Decimal("100"), Decimal("1"))
    assert evaluate("BTC/USDT:USDT", flat(10), trend_bars, params, False) is None


def test_no_signal_without_enough_trend_history(params):
    assert evaluate("BTC/USDT:USDT", _crossing_up_series(), flat(5), params, False) is None


def test_signal_carries_its_time_stop(params):
    signal_bars = _crossing_up_series()
    trend_bars = ramp(MIN_TREND_BARS + 20, Decimal("100"), Decimal("1"), tf_ms=BAR_MS_5M * 3)

    for end in range(MIN_SIGNAL_BARS + 20, len(signal_bars) + 1):
        signal = evaluate("BTC/USDT:USDT", signal_bars[:end], trend_bars, params, False)
        if signal is not None:
            assert signal.time_stop_ts == signal.signal_bar_ts + TIME_STOP_BARS * BAR_MS_5M
            return
    pytest.fail("no signal produced")


def test_missing_symbol_raises(params):
    with pytest.raises(StrategyError):
        evaluate("", _crossing_up_series(), ramp(80, Decimal("100"), Decimal("1")), params, False)


# --- parameters ------------------------------------------------------------


def test_slow_must_be_slower_than_fast():
    with pytest.raises(ValueError):
        ScalperParams(ema_fast=21, ema_slow=9)
    with pytest.raises(ValueError):
        ScalperParams(ema_fast=9, ema_slow=9)


@pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-1")])
def test_non_positive_atr_mult_and_target_are_rejected(bad):
    with pytest.raises(ValueError):
        ScalperParams(atr_mult=bad)
    with pytest.raises(ValueError):
        ScalperParams(target_r=bad)


def test_tiny_ema_periods_are_rejected():
    with pytest.raises(ValueError):
        ScalperParams(ema_fast=1)
