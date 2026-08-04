"""
EMA scalper strategy.

PURE MODULE. Takes two OHLCV arrays and returns a Signal or None. No network,
no clock, no side effects -- same contract as strategy.py, so the backtester can
drive the identical function the live bot calls.

Structure
---------
    15m  EMA(trend)          defines direction. Longs only above it, shorts only
                             below it. This filter is the whole reason a cross
                             system is survivable: without it, every sideways
                             session is a sequence of whipsaw losses.

    5m   EMA(fast)/EMA(slow) triggers the entry, but ONLY on the bar where the
                             cross actually happens and ONLY in the direction
                             the 15m filter allows.

    Stop    entry -/+ atr_mult * ATR(14)   -- exchange-side, attached to entry
    Target  2R                             -- reduce-only limit
    Time    24 bars (2h)                   -- flatten at market

WHY THE STOP IS WIDE
--------------------
Fees are charged on notional, and at 1% risk the notional is set by the stop
distance. A 0.2% stop means 5x equity of notional and a 0.6R round-trip fee
before the market moves at all. A wider stop means smaller notional and less
fee drag per unit of risk. On a market-entry scalper this is not a detail --
it is usually the difference between a positive and a negative expectancy.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Sequence

from cf_bot.strategy import ATR_PERIOD, Bar, Signal, StrategyError, atr_series

# ---------------------------------------------------------------------------
# Fixed by convention -- NOT tunable.
# ---------------------------------------------------------------------------
SIGNAL_TIMEFRAME = "5m"
TREND_TIMEFRAME = "15m"

BAR_MS_5M = 5 * 60 * 1000
BAR_MS_15M = 15 * 60 * 1000

TIME_STOP_BARS = 24  # 24 * 5m = 2 hours
ENTRY_VALID_BARS = 1  # a cross is stale almost immediately; do not chase it

# How much history the evaluator needs before it will produce anything.
MIN_SIGNAL_BARS = 60
MIN_TREND_BARS = 60


@dataclass(frozen=True)
class ScalperParams:
    """
    Five tunables. Kept deliberately small -- every extra knob is another
    dimension to curve-fit in.
    """

    ema_fast: int = 9
    ema_slow: int = 21
    ema_trend: int = 50
    atr_mult: Decimal = Decimal("1.5")
    target_r: Decimal = Decimal("2.0")

    def __post_init__(self) -> None:
        if self.ema_fast < 2:
            raise ValueError(f"ema_fast must be >= 2, got {self.ema_fast}")
        if self.ema_slow <= self.ema_fast:
            raise ValueError(
                f"ema_slow ({self.ema_slow}) must be greater than ema_fast "
                f"({self.ema_fast}), or the cross has no meaning"
            )
        if self.ema_trend < 2:
            raise ValueError(f"ema_trend must be >= 2, got {self.ema_trend}")
        if self.atr_mult <= 0:
            raise ValueError(f"atr_mult must be positive, got {self.atr_mult}")
        if self.target_r <= 0:
            raise ValueError(f"target_r must be positive, got {self.target_r}")

    @property
    def warmup_bars(self) -> int:
        return max(self.ema_slow, self.ema_fast, ATR_PERIOD) * 3


# ---------------------------------------------------------------------------
# Indicator
# ---------------------------------------------------------------------------


def ema_series(values: Sequence[Decimal], period: int) -> list[Optional[Decimal]]:
    """
    Exponential moving average, aligned index-for-index with `values`.

    Seeded with a simple average of the first `period` values, then smoothed
    with alpha = 2/(period+1). Entries before the seed are None rather than a
    partial average, so a caller cannot trade off a warm-up artefact.
    """
    if period < 1:
        raise StrategyError(f"EMA period must be >= 1, got {period}")

    out: list[Optional[Decimal]] = [None] * len(values)
    if len(values) < period:
        return out

    seed = sum(values[:period]) / Decimal(period)
    out[period - 1] = seed

    alpha = Decimal(2) / Decimal(period + 1)
    previous = seed
    for i in range(period, len(values)):
        current = (values[i] - previous) * alpha + previous
        out[i] = current
        previous = current

    return out


def _closes(bars: Sequence[Bar]) -> list[Decimal]:
    return [b.close for b in bars]


# ---------------------------------------------------------------------------
# Trend filter
# ---------------------------------------------------------------------------


def trend_direction(trend_bars: Sequence[Bar], period: int) -> Optional[str]:
    """
    'long', 'short', or None if the trend cannot be established.

    None means the system is OFF -- not that direction is unconstrained. An
    unknown trend fails closed, same as every other filter in this codebase.
    """
    if len(trend_bars) < max(period, MIN_TREND_BARS):
        return None

    emas = ema_series(_closes(trend_bars), period)
    latest_ema = emas[-1]
    if latest_ema is None:
        return None

    latest_close = trend_bars[-1].close
    if latest_close > latest_ema:
        return "long"
    if latest_close < latest_ema:
        return "short"
    return None  # exactly equal: no opinion


# ---------------------------------------------------------------------------
# Cross detection
# ---------------------------------------------------------------------------


def cross_direction(
    signal_bars: Sequence[Bar], fast_period: int, slow_period: int
) -> Optional[str]:
    """
    Did fast cross slow on the MOST RECENT closed bar?

    Returns 'long' on an upward cross, 'short' on a downward cross, None
    otherwise. Deliberately only the most recent bar: a cross three bars ago is
    already priced in, and entering on it is chasing.
    """
    if len(signal_bars) < max(slow_period, MIN_SIGNAL_BARS):
        return None

    closes = _closes(signal_bars)
    fast = ema_series(closes, fast_period)
    slow = ema_series(closes, slow_period)

    fast_now, fast_prev = fast[-1], fast[-2]
    slow_now, slow_prev = slow[-1], slow[-2]

    if None in (fast_now, fast_prev, slow_now, slow_prev):
        return None

    was_below = fast_prev <= slow_prev
    is_above = fast_now > slow_now
    if was_below and is_above:
        return "long"

    was_above = fast_prev >= slow_prev
    is_below = fast_now < slow_now
    if was_above and is_below:
        return "short"

    return None


# ---------------------------------------------------------------------------
# The strategy
# ---------------------------------------------------------------------------


def evaluate(
    symbol: str,
    signal_bars: Sequence[Bar],
    trend_bars: Sequence[Bar],
    params: ScalperParams,
    in_settlement_blackout: bool,
) -> Optional[Signal]:
    """
    Evaluate the most recent CLOSED 5m bar and return a Signal, or None.

    Both bar arrays must contain only CLOSED bars, oldest first. The caller
    drops the forming candle -- this function has no clock and cannot tell a
    closed bar from a live one.

    `in_settlement_blackout` is passed in rather than computed so the module
    stays pure and the backtester can drive it deterministically.
    """
    if not symbol:
        raise StrategyError("symbol is required")

    if in_settlement_blackout:
        return None

    if len(signal_bars) < max(params.warmup_bars, MIN_SIGNAL_BARS):
        return None
    if len(trend_bars) < max(params.ema_trend, MIN_TREND_BARS):
        return None

    # 1. Direction, from the 15m.
    trend = trend_direction(trend_bars, params.ema_trend)
    if trend is None:
        return None

    # 2. Trigger, from the 5m -- and only in the trend's direction.
    cross = cross_direction(signal_bars, params.ema_fast, params.ema_slow)
    if cross is None or cross != trend:
        return None

    side = trend
    signal_bar = signal_bars[-1]

    # 3. Risk, from ATR on the signal timeframe.
    atrs = atr_series(signal_bars, ATR_PERIOD)
    atr = atrs[-1]
    if atr is None or atr <= 0:
        return None

    # Entry is the last close. The executor treats this as a reference price and
    # is permitted to fill at market, so this is an estimate, not a guarantee --
    # position size is always recomputed from what actually filled.
    entry_price = signal_bar.close
    stop_distance = params.atr_mult * atr

    if side == "long":
        stop_price = entry_price - stop_distance
        target_price = entry_price + stop_distance * params.target_r
    else:
        stop_price = entry_price + stop_distance
        target_price = entry_price - stop_distance * params.target_r

    # A stop at or through the entry would make the size denominator zero or
    # negative. Refuse rather than divide.
    if side == "long" and stop_price >= entry_price:
        return None
    if side == "short" and stop_price <= entry_price:
        return None
    if stop_price <= 0 or target_price <= 0:
        return None

    return Signal(
        symbol=symbol,
        side=side,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        signal_bar_ts=signal_bar.timestamp_ms,
        atr=atr,
        # Reused field: for this strategy it carries the fast/slow EMA spread at
        # the cross, which is the closest analogue to signal strength. It is
        # logged for diagnostics and is NOT used to scale position size.
        displacement=stop_distance,
        entry_expires_after_ts=signal_bar.timestamp_ms + ENTRY_VALID_BARS * BAR_MS_5M,
        time_stop_ts=signal_bar.timestamp_ms + TIME_STOP_BARS * BAR_MS_5M,
    )
