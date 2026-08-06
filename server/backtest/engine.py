"""
Backtest engine.

Imports the SAME `evaluate` the live bot calls. If these could drift, the
backtest would be worthless -- so the strategy is never reimplemented here, only
driven.

It also replicates the live guards that affect which trades are available:
settlement blackout, the settlement flatten, one position at a time, and the
daily entry cap. A backtest that ignores the guards measures a strategy the bot
will never actually run.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Sequence

from backtest.data import resample
from backtest.fills import (
    EntryFill,
    fee_for,
    find_entry_fill,
    gross_pnl,
    market_entry_fill_price,
    stop_fill_price,
    stop_hit,
    target_hit,
)
from cf_bot import guards
from cf_bot.scalper import (
    MIN_SIGNAL_BARS as SCALPER_MIN_SIGNAL_BARS,
    MIN_TREND_BARS as SCALPER_MIN_TREND_BARS,
    TIME_STOP_BARS as SCALPER_TIME_STOP_BARS,
    ScalperParams,
)
from cf_bot.scalper import evaluate as evaluate_scalper
from cf_bot.scalper import BAR_MS_5M, BAR_MS_15M
from cf_bot.strategy import (
    ENTRY_VALID_BARS,
    atr_series,
    TIME_STOP_BARS,
    Bar,
    StrategyParams,
    evaluate,
    position_size,
)


@dataclass(frozen=True)
class Trade:
    symbol: str
    side: str
    entry_ts: int
    exit_ts: int
    entry_price: Decimal
    exit_price: Decimal
    qty: Decimal
    gross: Decimal
    fees: Decimal
    funding: Decimal
    net: Decimal
    r_multiple: Decimal
    exit_reason: str

    @property
    def is_win(self) -> bool:
        return self.net > 0


@dataclass
class BacktestConfig:
    symbol: str
    params: StrategyParams
    risk_pct: Decimal = Decimal("1.0")
    starting_equity: Decimal = Decimal("1000")
    # Funding rate assumed at every bar, as a fraction per 8h. The regime filter
    # requires abs(funding) <= 0.1%, so a value here of 0 means "assume funding
    # was always benign" -- which is OPTIMISTIC. Supply real funding data for a
    # trustworthy result. The report states which was used.
    assumed_funding: Optional[Decimal] = Decimal("0")
    apply_guards: bool = True

    # --- scalper ----------------------------------------------------------
    # When set, the EMA scalper is driven instead of forced flow.
    scalper_params: Optional[ScalperParams] = None
    # How many 5m bars make one SIGNAL bar and one TREND bar. The input series
    # is always 5m; both are resampled from it so the two can never disagree.
    signal_factor: int = 1      # 1=5m, 3=15m, 12=1h, 48=4h
    trend_factor: int = 3       # conventionally 3-4x the signal timeframe

    @property
    def is_scalper(self) -> bool:
        return self.scalper_params is not None

    @property
    def time_stop_bars(self) -> int:
        return SCALPER_TIME_STOP_BARS if self.is_scalper else TIME_STOP_BARS


def _reason_none(bars_needed: int, available: int) -> str:
    return f"insufficient history: need {bars_needed}, have {available}"


def _trend_bars_up_to(
    trend_bars: list[Bar], timestamp_ms: int, trend_bar_ms: int
) -> list[Bar]:
    """
    Trend bars that had CLOSED by the time this signal bar closed.

    A 15m bar stamped 08:00 covers 08:00-08:15 and is only complete at 08:15.
    Including it while evaluating the 08:05 signal bar would be lookahead --
    reading a candle that has not finished forming.
    """
    return [b for b in trend_bars if b.timestamp_ms + trend_bar_ms <= timestamp_ms]


def run_backtest(bars: Sequence[Bar], config: BacktestConfig) -> tuple[list[Trade], list[str]]:
    """
    Walk the bar series and return (trades, warnings).

    Equity compounds: each trade is sized off equity at the time it was taken,
    matching the live sizing rule.
    """
    trades: list[Trade] = []
    warnings: list[str] = []
    bars = list(bars)

    if len(bars) < 100:
        warnings.append(_reason_none(100, len(bars)))
        return trades, warnings

    # The scalper's 15m trend series is derived from the same 5m data rather
    # than fetched separately, so the two series cannot disagree.
    # Both series are resampled from the SAME 5m input, so the trend filter can
    # never disagree with the signal series about what price did.
    if config.is_scalper:
        trend_bars = resample(bars, config.trend_factor)
        if config.signal_factor > 1:
            bars = resample(bars, config.signal_factor)
    else:
        trend_bars = []

    # Bound the windows handed to the strategy to exactly what the LIVE bot
    # passes it. Two reasons, and the second matters more:
    #
    #  1. Speed. Slicing bars[:i+1] re-ran the EMAs over the whole history on
    #     every bar -- O(n^2), which on a year of 5m data is billions of Decimal
    #     operations and simply never finishes.
    #  2. Fidelity. The live BarCache is capped at these same counts, so a
    #     backtest feeding unbounded history would be evaluating a longer EMA
    #     seed than production ever sees. Bounded is the faithful version.
    scalper = config.scalper_params
    if scalper is not None:
        signal_window = max(scalper.warmup_bars, SCALPER_MIN_SIGNAL_BARS) + 5
        trend_window = max(scalper.ema_trend * 3, SCALPER_MIN_TREND_BARS) + 5
    else:
        signal_window = trend_window = 0

    # Forced flow: compute the ATR series ONCE over the whole set and hand the
    # strategy a slice. Recomputing an 8640-bar Wilder ATR on every one of
    # 105,000 bars is ~900M Decimal operations and never finishes. After ~100
    # bars of smoothing the seed is gone, so a full-series ATR and a
    # window-seeded one are identical to far more precision than matters.
    ff_atrs = None
    if not config.is_scalper:
        if config.signal_factor > 1:
            bars = resample(bars, config.signal_factor)
        ff_atrs = atr_series(bars)

    trend_bar_ms = config.trend_factor * BAR_MS_5M
    # Close time of each trend bar, for an O(log n) "which had closed by now".
    trend_close_ts = [b.timestamp_ms + trend_bar_ms for b in trend_bars]

    equity = config.starting_equity
    open_until_index = -1  # index through which a position is held
    entries_by_day: dict[str, int] = {}
    daily_realised: dict[str, Decimal] = {}
    day_open_equity: dict[str, Decimal] = {}
    # Losing streak WITHIN the current UTC day.
    #
    # This must reset daily, because the live guard derives it from
    # todays_closed_positions and therefore cannot see yesterday. A single
    # running counter deadlocks the backtest: once it reaches the limit, no
    # further trade can occur, so no win can ever reset it, so the strategy
    # stops trading permanently on the first three-loss day.
    losses_by_day: dict[str, int] = {}

    for i in range(1, len(bars)):
        if i <= open_until_index:
            continue  # a position is still open; only one at a time

        signal_bar = bars[i]
        day_key = _utc_day(signal_bar.timestamp_ms)
        day_open_equity.setdefault(day_key, equity)

        if config.apply_guards:
            if entries_by_day.get(day_key, 0) >= guards.MAX_ENTRIES_PER_UTC_DAY:
                continue
            if losses_by_day.get(day_key, 0) >= guards.MAX_CONSECUTIVE_LOSSES:
                continue
            realised = daily_realised.get(day_key, Decimal(0))
            opening = day_open_equity[day_key]
            if opening > 0 and realised / opening * Decimal(100) <= guards.DAILY_LOSS_LIMIT_PCT:
                continue

        in_blackout = (
            guards.in_settlement_blackout(signal_bar.timestamp_ms)
            if config.apply_guards
            else False
        )

        if config.is_scalper:
            if i < SCALPER_MIN_SIGNAL_BARS:
                continue
            closed_trend = bisect_right(trend_close_ts, signal_bar.timestamp_ms)
            signal = evaluate_scalper(
                symbol=config.symbol,
                signal_bars=bars[max(0, i + 1 - signal_window) : i + 1],
                trend_bars=trend_bars[max(0, closed_trend - trend_window) : closed_trend],
                params=config.scalper_params,
                in_settlement_blackout=in_blackout,
            )
        else:
            lo = max(0, i + 1 - (config.params.lookback_bars + 64))
            signal = evaluate(
                symbol=config.symbol,
                bars=bars[lo : i + 1],
                funding_rate=config.assumed_funding,
                params=config.params,
                in_settlement_blackout=in_blackout,
                precomputed_atrs=ff_atrs[lo : i + 1],
            )

        if signal is None:
            continue

        if config.is_scalper:
            # Worst-case fill assumption: always a market order at the next
            # bar's open, always paying taker. Live, the passive leg fills some
            # of the time at maker, so real fees land BELOW this. Modelling the
            # optimistic case would be the easy way to manufacture an edge.
            if i + 1 >= len(bars):
                continue
            fill = EntryFill(
                bar_index=i + 1,
                timestamp_ms=bars[i + 1].timestamp_ms,
                price=market_entry_fill_price(bars[i + 1], signal.side),
            )
        else:
            fill = find_entry_fill(
                bars=bars,
                signal_index=i,
                side=signal.side,
                limit_price=signal.entry_price,
                valid_bars=ENTRY_VALID_BARS,
            )
            if fill is None:
                continue  # order expired unfilled -- this is the common case

        qty = position_size(
            equity=equity,
            risk_pct=config.risk_pct,
            entry_price=signal.entry_price,
            stop_price=signal.stop_price,
        )
        if qty <= 0:
            continue

        trade = _simulate_exit(
            bars=bars,
            fill_index=fill.bar_index,
            entry_price=fill.price,
            signal=signal,
            qty=qty,
            config=config,
        )
        trades.append(trade)

        equity += trade.net
        entries_by_day[day_key] = entries_by_day.get(day_key, 0) + 1
        daily_realised[day_key] = daily_realised.get(day_key, Decimal(0)) + trade.net
        losses_by_day[day_key] = 0 if trade.is_win else losses_by_day.get(day_key, 0) + 1

        open_until_index = _index_of_ts(bars, trade.exit_ts, fallback=fill.bar_index)

        if equity <= 0:
            warnings.append(f"equity reached {equity} at {trade.exit_ts}; stopping")
            break

    return trades, warnings


def _simulate_exit(
    bars: list[Bar],
    fill_index: int,
    entry_price: Decimal,
    signal,
    qty: Decimal,
    config: BacktestConfig,
) -> Trade:
    """
    Walk forward from the fill bar to whichever exit comes first.

    Precedence within a single bar is STOP > TARGET. Bar data cannot resolve
    intrabar ordering, and assuming the favourable one manufactures edge.
    """
    side = signal.side
    risk_per_unit = abs(signal.entry_price - signal.stop_price)
    last_index = min(fill_index + config.time_stop_bars, len(bars) - 1)

    exit_price: Optional[Decimal] = None
    exit_ts = bars[last_index].timestamp_ms
    exit_reason = "time_stop"
    exit_is_maker = False

    for index in range(fill_index, last_index + 1):
        bar = bars[index]

        if config.apply_guards and guards.should_flatten_for_settlement(bar.timestamp_ms):
            exit_price = bar.close
            exit_ts = bar.timestamp_ms
            exit_reason = "settlement_flatten"
            exit_is_maker = False
            break

        if stop_hit(bar, side, signal.stop_price):
            exit_price = stop_fill_price(side, signal.stop_price)
            exit_ts = bar.timestamp_ms
            exit_reason = "stop"
            exit_is_maker = False
            break

        if target_hit(bar, side, signal.target_price):
            exit_price = signal.target_price
            exit_ts = bar.timestamp_ms
            exit_reason = "target"
            exit_is_maker = True  # resting reduce-only limit
            break

    if exit_price is None:
        # Time stop: flatten at market on the close of entry_bar + 12.
        exit_price = bars[last_index].close
        exit_ts = bars[last_index].timestamp_ms
        exit_reason = "time_stop"
        exit_is_maker = False

    gross = gross_pnl(side, entry_price, exit_price, qty)

    # Entry was a post-only limit -> maker. Exit fee depends on how it left.
    entry_fee = fee_for(entry_price * qty, is_maker=not config.is_scalper)
    exit_fee = fee_for(exit_price * qty, is_maker=exit_is_maker)
    fees = entry_fee + exit_fee

    funding = _funding_cost(
        bars, fill_index, exit_ts, side, entry_price, qty, config.assumed_funding
    )

    net = gross - fees - funding
    r_multiple = net / (risk_per_unit * qty) if risk_per_unit > 0 and qty > 0 else Decimal(0)

    return Trade(
        symbol=config.symbol,
        side=side,
        entry_ts=bars[fill_index].timestamp_ms,
        exit_ts=exit_ts,
        entry_price=entry_price,
        exit_price=exit_price,
        qty=qty,
        gross=gross,
        fees=fees,
        funding=funding,
        net=net,
        r_multiple=r_multiple,
        exit_reason=exit_reason,
    )


def _funding_cost(
    bars: list[Bar],
    fill_index: int,
    exit_ts: int,
    side: str,
    entry_price: Decimal,
    qty: Decimal,
    rate: Optional[Decimal],
) -> Decimal:
    """
    Funding paid while the position was open.

    Applies the rate known at bar time for each settlement boundary crossed. The
    settlement flatten normally closes us before a boundary, so this is usually
    zero -- but it is charged when it does apply rather than quietly ignored.
    """
    if rate is None or rate == 0:
        return Decimal(0)

    entry_ts = bars[fill_index].timestamp_ms
    crossings = 0
    for index in range(fill_index, len(bars)):
        bar = bars[index]
        if bar.timestamp_ms > exit_ts:
            break
        if bar.timestamp_ms <= entry_ts:
            continue
        if guards.minutes_until_next_settlement(bar.timestamp_ms) == 0:
            crossings += 1

    if crossings == 0:
        return Decimal(0)

    notional = entry_price * qty
    cost = notional * abs(rate) * Decimal(crossings)
    return cost  # charged as a cost regardless of side: pessimistic


def _utc_day(timestamp_ms: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _index_of_ts(bars: list[Bar], timestamp_ms: int, fallback: int) -> int:
    for index in range(fallback, len(bars)):
        if bars[index].timestamp_ms >= timestamp_ms:
            return index
    return fallback
