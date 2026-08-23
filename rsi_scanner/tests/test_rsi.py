"""
RSI correctness and reset detection.

The reference series below is Wilder's own worked example, at the precision
it is actually published at. The expected values are the canonical RSI(14)
outputs for it. If this test fails, the RSI is wrong -- do not adjust the
expected numbers to make it pass.
"""

import pytest

from rsi_scanner.rsi import BEARISH, BULLISH, detect_reset, wilder_rsi

WILDER_CLOSES = [
    44.3389, 44.0902, 44.1497, 43.6124, 44.3278, 44.8264, 45.0955, 45.4245,
    45.8433, 46.0826, 45.8931, 46.0328, 45.6140, 46.2820, 46.2820, 46.0028,
    46.0328, 46.4116, 46.2222, 45.6439, 46.2122, 46.2521, 45.7137, 46.4515,
    45.7835, 45.3548, 44.0288, 44.1783, 44.2181, 44.5672, 43.4205, 42.6628,
    43.1314,
]

WILDER_RSI14 = [
    70.53, 66.32, 66.55, 69.41, 66.36, 57.97, 62.93, 63.26, 56.06, 62.38,
    54.71, 50.42, 39.99, 41.46, 41.87, 45.46, 37.30, 33.08, 37.77,
]


def test_matches_wilder_reference():
    values = [v for v in wilder_rsi(WILDER_CLOSES, 14) if v is not None]
    assert len(values) == len(WILDER_RSI14)
    for got, want in zip(values, WILDER_RSI14):
        assert got == pytest.approx(want, abs=0.005)


def test_alignment_is_index_for_index():
    """result[i] must describe closes[i] -- an off-by-one here reads the wrong bar."""
    series = wilder_rsi(WILDER_CLOSES, 14)
    assert len(series) == len(WILDER_CLOSES)
    assert series[:14] == [None] * 14
    assert series[14] == pytest.approx(70.53, abs=0.005)


def test_too_few_bars_is_all_none():
    assert wilder_rsi([1.0, 2.0, 3.0], 14) == [None, None, None]
    assert wilder_rsi([], 14) == []


def test_monotonic_rise_is_100_not_a_crash():
    """avg_loss == 0 must not divide by zero."""
    assert wilder_rsi([float(i) for i in range(30)], 14)[-1] == 100.0


def test_flat_series_is_50_not_100():
    """
    No gains AND no losses. Reporting 100 -- maximum strength -- for a market
    that has not moved would be actively misleading.
    """
    assert wilder_rsi([100.0] * 30, 14)[-1] == 50.0


def test_monotonic_fall_is_zero():
    assert wilder_rsi([float(30 - i) for i in range(30)], 14)[-1] == 0.0


# --------------------------------------------------------------------------
# Reset detection
# --------------------------------------------------------------------------

def test_bullish_cross_up_through_30():
    assert detect_reset([None, 26.1, 32.6]) == BULLISH


def test_bearish_cross_down_through_70():
    assert detect_reset([None, 74.2, 68.1]) == BEARISH


def test_no_cross_returns_none():
    assert detect_reset([40.0, 45.0]) is None
    assert detect_reset([26.0, 28.0]) is None   # still oversold
    assert detect_reset([75.0, 78.0]) is None   # still overbought


def test_stale_cross_does_not_fire():
    """Crossed out of oversold two bars ago. Today is not the crossing bar."""
    assert detect_reset([25.0, 33.0, 38.0]) is None


def test_touching_threshold_exactly_counts_as_crossed():
    """prev < 30 <= curr. Landing exactly on 30 is out of oversold."""
    assert detect_reset([29.9, 30.0]) == BULLISH
    assert detect_reset([70.1, 70.0]) == BEARISH


def test_sitting_exactly_on_threshold_then_rising_does_not_fire():
    """prev == 30 is not 'below 30', so there is no crossing."""
    assert detect_reset([30.0, 34.0]) is None


def test_none_values_are_not_a_signal():
    assert detect_reset([None, None]) is None
    assert detect_reset([None, 32.0]) is None
    assert detect_reset([26.0]) is None
    assert detect_reset([]) is None


def test_custom_thresholds_are_honoured():
    assert detect_reset([19.0, 21.0], oversold=20.0, overbought=80.0) == BULLISH
    assert detect_reset([19.0, 21.0]) is None   # default 30 -> no cross


def test_default_period_is_7_not_wilders_14():
    """
    Locked deliberately. The deployed setting is 7; if someone "corrects" this
    back to the textbook 14, every threshold crossing changes and the alert
    rate drops by roughly two thirds without anything looking broken.
    """
    from rsi_scanner.rsi import DEFAULT_PERIOD
    assert DEFAULT_PERIOD == 7


def test_reference_test_pins_its_own_period():
    """
    The Wilder check above must pass 14 explicitly rather than relying on the
    default -- it validates the algorithm, not the deployed configuration.
    Changing DEFAULT_PERIOD must never silently invalidate the reference.
    """
    import inspect
    from rsi_scanner.tests import test_rsi
    assert "wilder_rsi(WILDER_CLOSES, 14)" in inspect.getsource(
        test_rsi.test_matches_wilder_reference
    )


def test_shorter_period_swings_wider():
    """
    The reason the alert rate roughly triples: RSI(7) reaches further from 50
    than RSI(14) on the same data.
    """
    closes = WILDER_CLOSES
    r7 = [v for v in wilder_rsi(closes, 7) if v is not None]
    r14 = [v for v in wilder_rsi(closes, 14) if v is not None]
    assert max(abs(v - 50) for v in r7) > max(abs(v - 50) for v in r14)
