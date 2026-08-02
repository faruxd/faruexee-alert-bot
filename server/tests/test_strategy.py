"""
The strategy module, driven by handcrafted bars.

The strategy is pure, so every one of these runs with no network, no clock and
no exchange. That is the property that lets the backtester drive the identical
function the live bot calls.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from cf_bot.strategy import (
    ATR_PERIOD,
    BAR_MS_5M,
    ENTRY_VALID_BARS,
    PERCENTILE_LOOKBACK_BARS,
    TIME_STOP_BARS,
    Bar,
    StrategyParams,
    StrategyError,
    atr_series,
    check_funding,
    evaluate,
    percentile,
    position_size,
    true_range,
)

# A flat bar with a true range of exactly 2.0, so ATR converges to 2.0.
FLAT_RANGE = Decimal("2")
FLAT_ATR = Decimal("2")

# Enough history that the 30-day trailing percentile window is completely full.
WARMUP_BARS = PERCENTILE_LOOKBACK_BARS + ATR_PERIOD + 2


def flat_bar(index: int, mid: Decimal = Decimal("100")) -> Bar:
    return Bar(
        timestamp_ms=index * BAR_MS_5M,
        open=mid,
        high=mid + Decimal("1"),
        low=mid - Decimal("1"),
        close=mid,
        volume=Decimal("10"),
    )


def flat_series(count: int = WARMUP_BARS) -> list[Bar]:
    return [flat_bar(i) for i in range(count)]


def cascade_bar(index: int, displacement: Decimal, mid: Decimal = Decimal("100")) -> Bar:
    """A bar that moves `displacement` from open to close, wicking to the extreme."""
    close = mid + displacement
    return Bar(
        timestamp_ms=index * BAR_MS_5M,
        open=mid,
        high=max(mid, close),
        low=min(mid, close),
        close=close,
        volume=Decimal("500"),
    )


@pytest.fixture(scope="module")
def warm_history() -> list[Bar]:
    """Shared across the module -- building 8600+ bars per test would be wasteful."""
    return flat_series()


@pytest.fixture
def params() -> StrategyParams:
    return StrategyParams()


# --- indicators ------------------------------------------------------------


def test_true_range_uses_the_widest_of_the_three_measures():
    previous = Bar(0, Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), Decimal("1"))
    gap_up = Bar(1, Decimal("110"), Decimal("111"), Decimal("109"), Decimal("110"), Decimal("1"))
    # high-low is 2, but high - prev_close is 11. The gap must win.
    assert true_range(gap_up, previous) == Decimal("11")


def test_atr_is_none_until_warmed_up():
    bars = flat_series(ATR_PERIOD + 5)
    atrs = atr_series(bars)
    assert all(a is None for a in atrs[:ATR_PERIOD])
    assert atrs[ATR_PERIOD] is not None


def test_atr_converges_to_the_constant_true_range():
    atrs = atr_series(flat_series(200))
    assert atrs[-1] == FLAT_ATR


def test_atr_series_is_index_aligned_with_bars():
    bars = flat_series(100)
    assert len(atr_series(bars)) == len(bars)


def test_atr_rejects_a_non_positive_period():
    with pytest.raises(StrategyError):
        atr_series(flat_series(50), period=0)


# --- percentile ------------------------------------------------------------


def test_percentile_matches_linear_interpolation():
    values = [Decimal(n) for n in range(1, 11)]  # 1..10
    assert percentile(values, Decimal("0")) == Decimal("1")
    assert percentile(values, Decimal("100")) == Decimal("10")
    assert percentile(values, Decimal("50")) == Decimal("5.5")


def test_percentile_of_a_single_value():
    assert percentile([Decimal("7")], Decimal("42")) == Decimal("7")


def test_percentile_rejects_an_empty_series():
    with pytest.raises(StrategyError):
        percentile([], Decimal("50"))


@pytest.mark.parametrize("bad", [Decimal("-1"), Decimal("101")])
def test_percentile_rejects_an_out_of_range_pct(bad):
    with pytest.raises(StrategyError):
        percentile([Decimal("1"), Decimal("2")], bad)


# --- regime filter ---------------------------------------------------------


def test_funding_within_limit_passes():
    assert check_funding(Decimal("0.0005")).passed is True


@pytest.mark.parametrize("rate", [Decimal("0.0011"), Decimal("-0.0011"), Decimal("0.05")])
def test_funding_outside_limit_fails(rate):
    assert check_funding(rate).passed is False


def test_unknown_funding_fails_closed():
    """Not knowing the funding rate is not permission to trade."""
    assert check_funding(None).passed is False


def test_no_signal_without_a_full_30_day_percentile_window(params):
    """
    Fail closed on short history.

    Without a full 30 days the percentile is not the statistic the strategy was
    specified against, and trading on a proxy would silently change the regime
    filter.
    """
    bars = flat_series(500)
    bars.append(cascade_bar(500, Decimal("6")))
    assert evaluate("BTC/USDT:USDT", bars, Decimal("0"), params, False) is None


# --- signals ---------------------------------------------------------------


def test_up_cascade_produces_a_short(warm_history, params):
    """D >= k*ATR fades the move: we sell into an up-cascade."""
    bars = warm_history + [cascade_bar(len(warm_history), Decimal("6"))]  # 6 >= 2.5*2
    signal = evaluate("BTC/USDT:USDT", bars, Decimal("0"), params, False)

    assert signal is not None
    assert signal.side == "short"
    assert signal.entry_price == Decimal("106")  # close
    assert signal.target_price == Decimal("100")  # open -- where the cascade started
    assert signal.stop_price == Decimal("106") + params.s * FLAT_ATR  # high + s*ATR


def test_down_cascade_produces_a_long(warm_history, params):
    bars = warm_history + [cascade_bar(len(warm_history), Decimal("-6"))]
    signal = evaluate("BTC/USDT:USDT", bars, Decimal("0"), params, False)

    assert signal is not None
    assert signal.side == "long"
    assert signal.entry_price == Decimal("94")
    assert signal.target_price == Decimal("100")
    assert signal.stop_price == Decimal("94") - params.s * FLAT_ATR  # low - s*ATR


def test_displacement_below_threshold_produces_nothing(warm_history, params):
    # k*ATR = 5.0; a 4.0 move must not trigger.
    bars = warm_history + [cascade_bar(len(warm_history), Decimal("4"))]
    assert evaluate("BTC/USDT:USDT", bars, Decimal("0"), params, False) is None


def test_threshold_is_inclusive(warm_history, params):
    """D >= k*ATR, exactly at the boundary, is a signal."""
    bars = warm_history + [cascade_bar(len(warm_history), params.k * FLAT_ATR)]
    assert evaluate("BTC/USDT:USDT", bars, Decimal("0"), params, False) is not None


def test_settlement_blackout_suppresses_the_signal(warm_history, params):
    bars = warm_history + [cascade_bar(len(warm_history), Decimal("6"))]
    assert evaluate("BTC/USDT:USDT", bars, Decimal("0"), params, True) is None


def test_excess_funding_suppresses_the_signal(warm_history, params):
    bars = warm_history + [cascade_bar(len(warm_history), Decimal("6"))]
    assert evaluate("BTC/USDT:USDT", bars, Decimal("0.002"), params, False) is None


def test_higher_k_suppresses_a_marginal_signal(warm_history):
    bars = warm_history + [cascade_bar(len(warm_history), Decimal("6"))]
    assert evaluate("BTC/USDT:USDT", bars, Decimal("0"), StrategyParams(k=Decimal("2.5")), False)
    assert (
        evaluate("BTC/USDT:USDT", bars, Decimal("0"), StrategyParams(k=Decimal("4")), False)
        is None
    )


def test_signal_carries_its_expiry_and_time_stop(warm_history, params):
    bars = warm_history + [cascade_bar(len(warm_history), Decimal("6"))]
    signal = evaluate("BTC/USDT:USDT", bars, Decimal("0"), params, False)

    assert signal.entry_expires_after_ts == signal.signal_bar_ts + ENTRY_VALID_BARS * BAR_MS_5M
    assert signal.time_stop_ts == signal.signal_bar_ts + TIME_STOP_BARS * BAR_MS_5M


def test_risk_per_unit_is_the_entry_stop_distance(warm_history, params):
    bars = warm_history + [cascade_bar(len(warm_history), Decimal("6"))]
    signal = evaluate("BTC/USDT:USDT", bars, Decimal("0"), params, False)
    assert signal.risk_per_unit == abs(signal.entry_price - signal.stop_price)
    assert signal.risk_per_unit > 0


def test_too_few_bars_returns_none(params):
    assert evaluate("BTC/USDT:USDT", [], Decimal("0"), params, False) is None
    assert evaluate("BTC/USDT:USDT", [flat_bar(0)], Decimal("0"), params, False) is None


def test_missing_symbol_raises(warm_history, params):
    with pytest.raises(StrategyError):
        evaluate("", warm_history, Decimal("0"), params, False)


# --- parameters ------------------------------------------------------------


@pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-1")])
def test_non_positive_k_and_s_are_rejected(bad):
    with pytest.raises(ValueError):
        StrategyParams(k=bad)
    with pytest.raises(ValueError):
        StrategyParams(s=bad)


@pytest.mark.parametrize("bad", [Decimal("-1"), Decimal("101")])
def test_p_outside_percentile_range_is_rejected(bad):
    with pytest.raises(ValueError):
        StrategyParams(p=bad)


# --- sizing ----------------------------------------------------------------


def test_position_size_risks_exactly_risk_pct():
    """1% of 1000 is 10; a 5-wide stop buys 2 units."""
    qty = position_size(
        equity=Decimal("1000"),
        risk_pct=Decimal("1.0"),
        entry_price=Decimal("100"),
        stop_price=Decimal("95"),
    )
    assert qty == Decimal("2")
    assert qty * Decimal("5") == Decimal("10")  # 1% of equity


def test_position_size_is_direction_agnostic():
    long_qty = position_size(Decimal("1000"), Decimal("1"), Decimal("100"), Decimal("95"))
    short_qty = position_size(Decimal("1000"), Decimal("1"), Decimal("100"), Decimal("105"))
    assert long_qty == short_qty


def test_position_size_scales_inversely_with_stop_distance():
    tight = position_size(Decimal("1000"), Decimal("1"), Decimal("100"), Decimal("99"))
    wide = position_size(Decimal("1000"), Decimal("1"), Decimal("100"), Decimal("90"))
    assert tight == wide * 10


def test_position_size_refuses_a_zero_width_stop():
    """The one input that would divide by zero."""
    with pytest.raises(StrategyError):
        position_size(Decimal("1000"), Decimal("1"), Decimal("100"), Decimal("100"))


@pytest.mark.parametrize("equity", [Decimal("0"), Decimal("-5")])
def test_position_size_refuses_non_positive_equity(equity):
    with pytest.raises(StrategyError):
        position_size(equity, Decimal("1"), Decimal("100"), Decimal("95"))


def test_position_size_treats_risk_pct_as_percent_not_fraction():
    """
    The unit that matters. risk_pct=1.0 must mean 1%, not 100%.

    Getting this backwards is a 100x sizing error.
    """
    qty = position_size(Decimal("10000"), Decimal("1.0"), Decimal("100"), Decimal("99"))
    assert qty * Decimal("1") == Decimal("100")  # 1% of 10000, not 10000
