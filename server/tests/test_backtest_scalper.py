"""
The scalper's backtest path: 15m resampling, no lookahead, and taker fees.
"""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from backtest.data import resample
from backtest.engine import BacktestConfig, _trend_bars_up_to, run_backtest
from backtest.fills import TAKER_FEE, market_entry_fill_price
from cf_bot.scalper import BAR_MS_15M, ScalperParams
from cf_bot.strategy import BAR_MS_5M, Bar, StrategyParams


def bar(ts_ms, o, h, low, c, v="10") -> Bar:
    return Bar(
        timestamp_ms=ts_ms,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=Decimal(v),
    )


# --- resampling ------------------------------------------------------------


def test_three_5m_bars_become_one_15m_bar():
    bars = [
        bar(0, "100", "105", "99", "101"),
        bar(BAR_MS_5M, "101", "108", "100", "107"),
        bar(2 * BAR_MS_5M, "107", "109", "95", "96"),
    ]
    out = resample(bars, 3)

    assert len(out) == 1
    assert out[0].open == Decimal("100")  # first open
    assert out[0].high == Decimal("109")  # highest high
    assert out[0].low == Decimal("95")  # lowest low
    assert out[0].close == Decimal("96")  # last close
    assert out[0].volume == Decimal("30")


def test_buckets_align_to_wall_clock_not_to_the_array():
    """
    A 15m series must land on :00/:15/:30/:45, or the backtest reads different
    candles than the exchange serves live.
    """
    start = BAR_MS_5M  # deliberately start at 00:05, mid-bucket
    bars = [bar(start + i * BAR_MS_5M, "100", "101", "99", "100") for i in range(7)]
    out = resample(bars, 3)

    for candle in out:
        assert candle.timestamp_ms % BAR_MS_15M == 0


def test_incomplete_trailing_bucket_is_dropped():
    """A partial bucket is a forming candle and must not be evaluated."""
    bars = [bar(i * BAR_MS_5M, "100", "101", "99", "100") for i in range(5)]
    out = resample(bars, 3)
    assert len(out) == 1  # bars 0-2 complete; 3-4 dropped


def test_resample_factor_one_is_identity():
    bars = [bar(i * BAR_MS_5M, "100", "101", "99", "100") for i in range(4)]
    assert resample(bars, 1) == bars


def test_resample_of_empty_is_empty():
    assert resample([], 3) == []


def test_bad_factor_raises():
    with pytest.raises(ValueError):
        resample([], 0)


# --- lookahead -------------------------------------------------------------


def test_a_forming_15m_bar_is_excluded():
    """
    The 15m bar stamped 00:00 covers 00:00-00:15 and is complete only at 00:15.
    Reading it while evaluating the 00:05 signal bar would be lookahead.
    """
    trend = [bar(0, "100", "101", "99", "100"), bar(BAR_MS_15M, "100", "101", "99", "100")]

    at_0005 = _trend_bars_up_to(trend, BAR_MS_5M)
    assert at_0005 == [], "used a 15m bar that had not closed yet"

    at_0015 = _trend_bars_up_to(trend, BAR_MS_15M)
    assert len(at_0015) == 1


def test_trend_bars_accumulate_as_time_advances():
    trend = [bar(i * BAR_MS_15M, "100", "101", "99", "100") for i in range(4)]
    assert len(_trend_bars_up_to(trend, 2 * BAR_MS_15M)) == 2
    assert len(_trend_bars_up_to(trend, 4 * BAR_MS_15M)) == 4


# --- fills -----------------------------------------------------------------


def test_market_entry_fills_at_next_open_plus_slippage():
    """Not at the signal bar's close -- that close is already history."""
    next_bar = bar(0, "100", "102", "98", "101")

    long_fill = market_entry_fill_price(next_bar, "long")
    short_fill = market_entry_fill_price(next_bar, "short")

    assert long_fill > Decimal("100"), "a long must pay up, not fill at the touch"
    assert short_fill < Decimal("100")
    assert long_fill - Decimal("100") == Decimal("100") - short_fill


def test_unknown_side_is_rejected():
    with pytest.raises(ValueError):
        market_entry_fill_price(bar(0, "100", "101", "99", "100"), "up")


# --- end to end ------------------------------------------------------------


def _trending_series(count=2400) -> list[Bar]:
    """
    Long trends with regular pullbacks.

    A perfectly smooth ramp produces NO fast/slow crosses inside a trend, so the
    strategy would never fire. Real trends pull back; the sine term supplies
    that, giving crosses that the slower 15m filter still agrees with. Fully
    deterministic -- no RNG, so the test cannot flake.
    """
    bars: list[Bar] = []
    previous = Decimal("100")
    for i in range(count):
        # Half the series trends up, half down. Each leg is long enough for the
        # 15m EMA(50) -- which needs ~12h -- to actually align.
        drift = Decimal("0.06") if i < count // 2 else Decimal("-0.06")
        base = Decimal("100") + drift * Decimal(i if i < count // 2 else i - count // 2)
        if i >= count // 2:
            base = Decimal("100") + Decimal("0.06") * Decimal(count // 2) + base - Decimal("100")

        pullback = Decimal(str(round(math.sin(i / 6.0) * 2.5, 4)))
        close = base + pullback

        bars.append(
            bar(
                i * BAR_MS_5M,
                str(previous),
                str(max(previous, close) + Decimal("0.3")),
                str(min(previous, close) - Decimal("0.3")),
                str(close),
            )
        )
        previous = close
    return bars


def _scalper_config(**overrides) -> BacktestConfig:
    base = dict(
        symbol="BTC/USDT:USDT",
        params=StrategyParams(),
        risk_pct=Decimal("1.0"),
        starting_equity=Decimal("1000"),
        scalper_params=ScalperParams(),
    )
    base.update(overrides)
    return BacktestConfig(**base)


def test_scalper_backtest_runs_and_produces_trades():
    trades, _ = run_backtest(_trending_series(), _scalper_config())
    assert len(trades) > 0, "a clearly trending series should produce at least one cross"


def test_every_scalper_trade_pays_a_fee():
    """No free entries. Taker on the way in is the modelled worst case."""
    trades, _ = run_backtest(_trending_series(), _scalper_config())
    assert all(t.fees > 0 for t in trades)


def test_scalper_entry_fee_is_taker_not_maker():
    trades, _ = run_backtest(_trending_series(), _scalper_config())
    assert trades, "no trades to check"
    trade = trades[0]
    entry_fee_at_taker = trade.entry_price * trade.qty * TAKER_FEE
    # Entry alone is taker; the exit adds more, so total fees must be at least this.
    assert trade.fees >= entry_fee_at_taker


def test_only_one_position_is_held_at_a_time():
    trades, _ = run_backtest(_trending_series(), _scalper_config())
    ordered = sorted(trades, key=lambda t: t.entry_ts)
    for earlier, later in zip(ordered, ordered[1:]):
        assert later.entry_ts >= earlier.exit_ts, "positions overlapped"


def test_a_flat_series_produces_no_trades():
    flat = [bar(i * BAR_MS_5M, "100", "100.1", "99.9", "100") for i in range(1500)]
    trades, _ = run_backtest(flat, _scalper_config())
    assert trades == []


def test_wider_stops_reduce_fee_drag_per_trade():
    """
    The core economic claim behind atr_mult: a wider stop means a smaller
    position, so the same round trip costs less in absolute fees.
    """
    series = _trending_series()
    tight, _ = run_backtest(
        series, _scalper_config(scalper_params=ScalperParams(atr_mult=Decimal("0.5")))
    )
    wide, _ = run_backtest(
        series, _scalper_config(scalper_params=ScalperParams(atr_mult=Decimal("3.0")))
    )
    assert tight and wide

    avg_tight = sum(t.fees for t in tight) / Decimal(len(tight))
    avg_wide = sum(t.fees for t in wide) / Decimal(len(wide))
    assert avg_wide < avg_tight
