"""
Phase 4 -- "Forced Flow" (CF) strategy.

THIS MODULE IS PURE.

It takes an OHLCV array and returns a Signal or None. Zero network calls, zero
side effects, zero clock reads that are not passed in. That is what lets the
backtester import the exact same function the live bot calls -- if these could
drift, the backtest would be worth nothing.

Thesis: perp liquidation engines submit price-insensitive market orders.
Cascades overshoot fair value and revert once the forced flow exhausts. We fade
the overshoot.

Three tunable parameters. k, s, p. There is no fourth, there are no indicators,
and there is no confidence score or signal-strength position scaling.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Sequence

# ---------------------------------------------------------------------------
# Fixed by convention -- NOT tunable. Changing any of these is a source edit.
# ---------------------------------------------------------------------------
ATR_PERIOD = 14
PERCENTILE_LOOKBACK_DAYS = 30
BARS_PER_DAY_5M = 288
PERCENTILE_LOOKBACK_BARS = PERCENTILE_LOOKBACK_DAYS * BARS_PER_DAY_5M  # 8640
ENTRY_VALID_BARS = 3
TIME_STOP_BARS = 12
ATR_PERCENTILE_CEILING = Decimal("90")
MAX_ABS_FUNDING_RATE = Decimal("0.001")  # 0.10% per 8h, as a fraction

BAR_MS_5M = 5 * 60 * 1000


@dataclass(frozen=True)
class Bar:
    """One CLOSED OHLCV bar. Never construct one from a forming candle."""

    timestamp_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    @staticmethod
    def from_ccxt(row: Sequence) -> "Bar":
        """ccxt returns [timestamp, open, high, low, close, volume]."""
        return Bar(
            timestamp_ms=int(row[0]),
            open=Decimal(str(row[1])),
            high=Decimal(str(row[2])),
            low=Decimal(str(row[3])),
            close=Decimal(str(row[4])),
            volume=Decimal(str(row[5])),
        )


@dataclass(frozen=True)
class StrategyParams:
    """The only three tunable knobs in the system."""

    k: Decimal = Decimal("2.5")  # displacement threshold, in ATR units
    s: Decimal = Decimal("1.25")  # stop distance, in ATR units
    p: Decimal = Decimal("30")  # ATR percentile floor

    # NOT a fourth tunable. The convention is a 30-day trailing window; this
    # expresses that in BARS, which differs per timeframe. Hardcoding 8640
    # silently meant "360 days" at 1h and "30 days" only at 5m.
    lookback_bars: int = PERCENTILE_LOOKBACK_BARS

    def __post_init__(self) -> None:
        if self.k <= 0:
            raise ValueError(f"k must be positive, got {self.k}")
        if self.s <= 0:
            raise ValueError(f"s must be positive, got {self.s}")
        if not (0 <= self.p <= 100):
            raise ValueError(f"p must be a percentile in [0, 100], got {self.p}")
        if self.lookback_bars < 100:
            raise ValueError(
                f"lookback_bars={self.lookback_bars} is too short for a percentile "
                "to mean anything"
            )


@dataclass(frozen=True)
class Signal:
    """
    A complete trade instruction. Every price the executor needs is already here;
    the executor makes no strategy decisions of its own.
    """

    symbol: str
    side: str  # "long" | "short" -- the direction we are ENTERING
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    signal_bar_ts: int
    atr: Decimal
    displacement: Decimal
    entry_expires_after_ts: int  # cancel any unfilled remainder after this bar ts
    time_stop_ts: int  # flatten at market on the close of this bar

    @property
    def risk_per_unit(self) -> Decimal:
        """abs(entry - stop). The denominator of the position-size calculation."""
        return abs(self.entry_price - self.stop_price)

    def describe(self) -> str:
        return (
            f"{self.side} {self.symbol} entry={self.entry_price} stop={self.stop_price} "
            f"target={self.target_price} atr={self.atr}"
        )


class StrategyError(Exception):
    """Malformed input. Never raised for 'no signal' -- that is a None return."""


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------


def true_range(current: Bar, previous: Bar) -> Decimal:
    """max(h-l, |h - prev_close|, |l - prev_close|)"""
    return max(
        current.high - current.low,
        abs(current.high - previous.close),
        abs(current.low - previous.close),
    )


def atr_series(bars: Sequence[Bar], period: int = ATR_PERIOD) -> list[Optional[Decimal]]:
    """
    Wilder's ATR, aligned index-for-index with `bars`.

    Entries before enough history exists are None rather than a partial average,
    so a caller cannot accidentally size a trade off a warm-up value. The first
    real value lands at index `period`.
    """
    if period <= 0:
        raise StrategyError(f"ATR period must be positive, got {period}")

    out: list[Optional[Decimal]] = [None] * len(bars)
    if len(bars) <= period:
        return out

    trs = [true_range(bars[i], bars[i - 1]) for i in range(1, len(bars))]

    # Seed with a simple average of the first `period` true ranges, then apply
    # Wilder smoothing. trs[j] corresponds to bars[j+1].
    seed = sum(trs[:period]) / Decimal(period)
    out[period] = seed

    previous = seed
    for j in range(period, len(trs)):
        current = (previous * Decimal(period - 1) + trs[j]) / Decimal(period)
        out[j + 1] = current
        previous = current

    return out


def percentile(values: Sequence[Decimal], pct: Decimal) -> Decimal:
    """
    Linear-interpolated percentile, matching numpy's default 'linear' method.

    Implemented in pure Python because the live path takes no numpy/pandas
    dependency -- and because the backtester must compute this identically.
    """
    if not values:
        raise StrategyError("cannot take a percentile of an empty series")
    if not (0 <= pct <= 100):
        raise StrategyError(f"percentile must be in [0, 100], got {pct}")

    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]

    rank = (Decimal(len(ordered)) - 1) * pct / Decimal(100)
    lower_index = int(rank)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    fraction = rank - Decimal(lower_index)

    return ordered[lower_index] + (ordered[upper_index] - ordered[lower_index]) * fraction


# ---------------------------------------------------------------------------
# Regime filter
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegimeVerdict:
    passed: bool
    reason: str


def check_atr_percentile(
    atrs: Sequence[Optional[Decimal]],
    signal_index: int,
    p: Decimal,
    lookback_bars: int = PERCENTILE_LOOKBACK_BARS,
) -> RegimeVerdict:
    """
    Require ATR[i-1] to sit within [p-th, 90th] percentile of the TRAILING
    30 days of 5m ATR.

    "Trailing" is load-bearing. A full-sample percentile leaks future volatility
    into a past decision, which is lookahead bias and would silently inflate
    every backtest result. The window here ends at signal_index-1 and never
    reaches beyond it.
    """
    reference_index = signal_index - 1
    if reference_index < 0:
        return RegimeVerdict(False, "no prior bar for ATR reference")

    reference_atr = atrs[reference_index]
    if reference_atr is None:
        return RegimeVerdict(False, "ATR not yet warmed up")

    window_start = max(0, reference_index - lookback_bars + 1)
    window = [a for a in atrs[window_start : reference_index + 1] if a is not None]

    if len(window) < lookback_bars:
        # Fail closed. Without a full 30 days the percentile is not the statistic
        # the strategy was specified against, and we do not trade on a proxy.
        return RegimeVerdict(
            False,
            f"insufficient ATR history: {len(window)}/{lookback_bars} bars",
        )

    floor = percentile(window, p)
    ceiling = percentile(window, ATR_PERCENTILE_CEILING)

    if reference_atr < floor:
        return RegimeVerdict(False, f"ATR {reference_atr} below {p}th pct {floor}")
    if reference_atr > ceiling:
        return RegimeVerdict(False, f"ATR {reference_atr} above 90th pct {ceiling}")

    return RegimeVerdict(True, "atr percentile ok")


def check_funding(funding_rate: Optional[Decimal]) -> RegimeVerdict:
    """Require abs(last settled funding) <= 0.10% per 8h. Unknown funding fails closed."""
    if funding_rate is None:
        return RegimeVerdict(False, "funding rate unknown")
    if abs(funding_rate) > MAX_ABS_FUNDING_RATE:
        return RegimeVerdict(
            False, f"funding {funding_rate} exceeds +/-{MAX_ABS_FUNDING_RATE}"
        )
    return RegimeVerdict(True, "funding ok")


# ---------------------------------------------------------------------------
# The strategy
# ---------------------------------------------------------------------------


def evaluate(
    symbol: str,
    bars: Sequence[Bar],
    funding_rate: Optional[Decimal],
    params: StrategyParams,
    in_settlement_blackout: bool,
    precomputed_atrs: Optional[Sequence[Optional[Decimal]]] = None,
) -> Optional[Signal]:
    """
    Evaluate the most recent CLOSED bar and return a Signal, or None.

    `bars` must contain only closed bars, oldest first. The caller is
    responsible for dropping the forming candle -- this function has no clock
    and cannot tell a closed bar from a live one.

    `in_settlement_blackout` is passed in rather than computed here so that this
    function stays pure and the backtester can drive it deterministically.
    """
    if not symbol:
        raise StrategyError("symbol is required")
    if len(bars) < 2:
        return None

    signal_index = len(bars) - 1
    signal_bar = bars[signal_index]

    # --- regime filter: all three must pass, or the system is OFF -----------
    if in_settlement_blackout:
        return None

    funding_verdict = check_funding(funding_rate)
    if not funding_verdict.passed:
        return None

    atrs = precomputed_atrs if precomputed_atrs is not None else atr_series(bars)
    atr_verdict = check_atr_percentile(
        atrs, signal_index, params.p, params.lookback_bars
    )
    if not atr_verdict.passed:
        return None

    reference_atr = atrs[signal_index - 1]
    assert reference_atr is not None  # guaranteed by check_atr_percentile

    # --- signal ------------------------------------------------------------
    displacement = signal_bar.close - signal_bar.open
    threshold = params.k * reference_atr

    if displacement >= threshold:
        # Up-cascade. We fade it: enter SHORT.
        side = "short"
        stop_price = signal_bar.high + params.s * reference_atr
    elif displacement <= -threshold:
        # Down-cascade. We fade it: enter LONG.
        side = "long"
        stop_price = signal_bar.low - params.s * reference_atr
    else:
        return None

    entry_price = signal_bar.close
    target_price = signal_bar.open  # the level the cascade started from

    # A stop on the wrong side of entry, or a zero-width stop, would make the
    # position-size denominator zero or negative. Refuse rather than divide.
    if side == "long" and stop_price >= entry_price:
        return None
    if side == "short" and stop_price <= entry_price:
        return None

    return Signal(
        symbol=symbol,
        side=side,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        signal_bar_ts=signal_bar.timestamp_ms,
        atr=reference_atr,
        displacement=displacement,
        entry_expires_after_ts=signal_bar.timestamp_ms + ENTRY_VALID_BARS * BAR_MS_5M,
        time_stop_ts=signal_bar.timestamp_ms + TIME_STOP_BARS * BAR_MS_5M,
    )


def position_size(
    equity: Decimal,
    risk_pct: Decimal,
    entry_price: Decimal,
    stop_price: Decimal,
) -> Decimal:
    """
    qty = (equity * risk_pct) / abs(entry - stop)

    risk_pct is in PERCENT, matching config and MAX_RISK_PCT, so it is divided
    by 100 here and nowhere else.

    In live use this is called with FILLED quantity in mind: the caller sizes
    the protective orders from what actually filled, never from what was
    requested.
    """
    if equity <= 0:
        raise StrategyError(f"equity must be positive to size a trade, got {equity}")
    if risk_pct <= 0:
        raise StrategyError(f"risk_pct must be positive, got {risk_pct}")

    risk_per_unit = abs(entry_price - stop_price)
    if risk_per_unit == 0:
        raise StrategyError("entry and stop are equal; refusing to divide by zero")

    risk_capital = equity * risk_pct / Decimal(100)
    return risk_capital / risk_per_unit
