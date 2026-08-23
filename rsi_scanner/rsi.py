"""
Wilder's RSI and the reset-cross detector.

Pure functions -- no network, no clock, no config. Everything in here is
deterministic given a list of closes, which is what makes it testable.
"""

from __future__ import annotations

from typing import List, Optional

DEFAULT_PERIOD = 14
DEFAULT_OVERSOLD = 30.0
DEFAULT_OVERBOUGHT = 70.0

BULLISH = "bullish"
BEARISH = "bearish"


def wilder_rsi(closes: List[float], period: int = DEFAULT_PERIOD) -> List[Optional[float]]:
    """
    RSI using Wilder's smoothing -- the original, and what TradingView's
    built-in ta.rsi() computes.

    NOT a simple moving average of gains/losses. Wilder's smoothing is an
    EMA with alpha = 1/period, seeded by an SMA of the first `period`
    changes. Using a plain SMA instead is the single most common way a
    hand-rolled RSI silently disagrees with the chart.

    Returns a list the same length as `closes`, with None for the first
    `period` entries where RSI is not yet defined. Index i of the result
    corresponds to closes[i].
    """
    n = len(closes)
    out: List[Optional[float]] = [None] * n
    if n < period + 1:
        return out

    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, n):
        delta = closes[i] - closes[i - 1]
        gains.append(delta if delta > 0 else 0.0)
        losses.append(-delta if delta < 0 else 0.0)

    # Seed from the first `period` changes, which consume closes[0..period].
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out[period] = _rsi_from(avg_gain, avg_loss)

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = _rsi_from(avg_gain, avg_loss)

    return out


def _rsi_from(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0.0:
        # A window with no down-closes at all. Conventionally 100 -- but if
        # there were no up-closes either the market was flat, and calling
        # that "maximum strength" would be wrong.
        return 100.0 if avg_gain > 0.0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def detect_reset(
    rsi_series: List[Optional[float]],
    oversold: float = DEFAULT_OVERSOLD,
    overbought: float = DEFAULT_OVERBOUGHT,
) -> Optional[str]:
    """
    The 30/70 reset cross, evaluated on the last two RSI values.

    bullish -- RSI was below `oversold` on the prior closed bar and is at or
               above it on the latest closed bar.
    bearish -- RSI was above `overbought` on the prior bar and is at or below
               it on the latest.

    Deliberately strict: the crossing bar itself must be the latest one. A
    symbol that climbed out of oversold three days ago does not fire today.

    Note this signal can retrigger. RSI wobbling around 30 for a week can
    cross up, fall back, and cross up again -- two alerts in three days. That
    is the honest behaviour of a threshold cross, not a bug; the fix is a
    different signal, not a filter bolted on here.
    """
    if len(rsi_series) < 2:
        return None
    prev, curr = rsi_series[-2], rsi_series[-1]
    if prev is None or curr is None:
        return None

    if prev < oversold <= curr:
        return BULLISH
    if prev > overbought >= curr:
        return BEARISH
    return None
