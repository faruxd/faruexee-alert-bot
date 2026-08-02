"""
The trading loop body.

Ordering here is deliberate and is the safety model. Every iteration does exit
work BEFORE entry work, so a bot that is behind on its housekeeping never opens
a new position on top of an unresolved one:

    1. kill switch          (in main)
    2. reconcile            (the exchange is the authority)
    3. settlement flatten   -- close ahead of funding
    4. time stop            -- close at entry_bar + 12
    5. cancel expired entries
    6. guards               -- any block ends the iteration
    7. scan for a signal, size it, place the entry

Steps 3-5 run even when the guards are blocking entries. A daily loss limit
stops new trades; it must not strand an open one.

WHY THE BAR CACHE EXISTS
------------------------
The regime filter needs a full 30 days of trailing 5m ATR -- 8640 bars, which
Bitget serves ~1000 at a time. Re-fetching that per symbol on a 15s loop would
be ~9 requests x 8 symbols every 15 seconds: it would exhaust the rate limit and
never keep up. So bars are fetched once per symbol, then topped up with a small
tail, and a symbol is only re-evaluated when a NEW bar has actually closed.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from cf_bot import guards
from cf_bot.config import AppConfig
from cf_bot.exchange import BitgetClient, DemoModeRefusal, ExchangeError
from cf_bot.guards import GuardContext
from cf_bot.orders import (
    ExecutionError,
    RateLimiter,
    UnprotectedPositionError,
    cancel_expired_entries,
    flatten,
    place_entry_with_protection,
)
from cf_bot.state import AccountState, Position
from cf_bot.strategy import (
    BAR_MS_5M,
    PERCENTILE_LOOKBACK_BARS,
    TIME_STOP_BARS,
    Bar,
    Signal,
    StrategyParams,
    evaluate,
    position_size,
)

# The percentile window plus ATR warm-up and a little headroom.
REQUIRED_BARS = PERCENTILE_LOOKBACK_BARS + 32

# How many bars to pull on a top-up. Generous enough to cover a stalled loop or
# a brief outage without needing the full history again.
REFRESH_BARS = 50


class BarCache:
    """
    Per-symbol OHLCV cache.

    Holds only closed bars, oldest first, capped at REQUIRED_BARS. Purely a
    performance cache -- it is never the authority on anything, and losing it
    (a restart) costs one full refetch, not correctness.
    """

    def __init__(self) -> None:
        self._bars: dict[str, list[Bar]] = {}

    def has_full_history(self, symbol: str) -> bool:
        return len(self._bars.get(symbol, [])) >= REQUIRED_BARS

    def latest_closed_ts(self, symbol: str) -> Optional[int]:
        bars = self._bars.get(symbol)
        return bars[-1].timestamp_ms if bars else None

    async def refresh(
        self,
        client: BitgetClient,
        symbol: str,
        timeframe: str,
        limiter: RateLimiter,
    ) -> list[Bar]:
        """Fetch or top up, then return the closed-bar series for this symbol."""
        await limiter.acquire()

        if self.has_full_history(symbol):
            rows = await client.fetch_ohlcv(symbol, timeframe, limit=REFRESH_BARS)
        else:
            rows = await client.fetch_ohlcv(symbol, timeframe, limit=REQUIRED_BARS)

        if not rows:
            return self._bars.get(symbol, [])

        # The final row is the FORMING candle. The strategy is defined on closed
        # bars only; acting on a forming bar means acting on a close that has
        # not happened.
        incoming = [Bar.from_ccxt(row) for row in rows[:-1]]

        merged: dict[int, Bar] = {b.timestamp_ms: b for b in self._bars.get(symbol, [])}
        for candle in incoming:
            merged[candle.timestamp_ms] = candle

        ordered = [merged[ts] for ts in sorted(merged)]
        self._bars[symbol] = ordered[-REQUIRED_BARS:]
        return self._bars[symbol]


class Trader:
    """Holds the state that must persist across loop iterations."""

    def __init__(self) -> None:
        self.bars = BarCache()
        self.order_timestamps: tuple[int, ...] = ()
        self.last_evaluated_bar: dict[str, int] = {}


def build_guard_context(state: AccountState, order_timestamps: tuple[int, ...]) -> GuardContext:
    return GuardContext(
        now_ms=state.fetched_at_ms,
        positions=state.positions,
        todays_fills=state.todays_fills,
        todays_closed_positions=state.todays_closed_positions,
        recent_order_timestamps_ms=order_timestamps,
        current_equity=state.equity,
    )


def time_stop_due(position: Position, now_ms: int) -> bool:
    """
    True once the position has been open for TIME_STOP_BARS bars.

    A position whose open time the exchange did not report cannot be aged, so it
    is NOT force-closed here -- it still has its exchange-side stop and target,
    and closing on a guess is worse than letting protection do its job.
    """
    if position.opened_at_ms is None:
        return False
    return now_ms >= position.opened_at_ms + TIME_STOP_BARS * BAR_MS_5M


async def handle_open_positions(
    client: BitgetClient, state: AccountState, log, limiter: RateLimiter
) -> bool:
    """Exit work. Runs regardless of guard state. Returns True if anything was flattened."""
    acted = False
    now_ms = state.fetched_at_ms
    settlement_due = guards.should_flatten_for_settlement(now_ms)

    for position in state.live_positions:
        if settlement_due:
            await flatten(
                client,
                position.symbol,
                log,
                limiter,
                reason=f"settlement in {guards.minutes_until_next_settlement(now_ms):.1f} min",
            )
            acted = True
            continue

        if time_stop_due(position, now_ms):
            await flatten(
                client,
                position.symbol,
                log,
                limiter,
                reason=f"time stop ({TIME_STOP_BARS} bars)",
            )
            acted = True
            continue

        if position.opened_at_ms is None:
            log.warning(
                "position.age_unknown",
                symbol=position.symbol,
                position=position.describe(),
                note=(
                    "exchange reported no open time, so the time stop cannot be applied. "
                    "Exchange-side stop and target still protect this position."
                ),
            )

    return acted


async def scan_for_signal(
    client: BitgetClient,
    config: AppConfig,
    state: AccountState,
    trader: Trader,
    log,
    limiter: RateLimiter,
) -> Optional[Signal]:
    """
    Evaluate every configured symbol in priority order; return the first signal.

    Only ONE position is held across all symbols, so the first match wins.
    """
    params = StrategyParams(
        k=config.settings.strategy.k,
        s=config.settings.strategy.s,
        p=config.settings.strategy.p,
    )
    timeframe = config.settings.exchange.timeframe
    in_blackout = guards.in_settlement_blackout(state.fetched_at_ms)

    for symbol in client.symbols:
        try:
            bars = await trader.bars.refresh(client, symbol, timeframe, limiter)
        except ExchangeError as exc:
            log.warning("scan.ohlcv_failed", symbol=symbol, error=str(exc))
            continue

        if len(bars) < 2:
            continue

        latest_ts = bars[-1].timestamp_ms
        if trader.last_evaluated_bar.get(symbol) == latest_ts:
            continue  # no new closed bar since we last looked

        trader.last_evaluated_bar[symbol] = latest_ts

        try:
            await limiter.acquire()
            funding = await client.fetch_funding_rate(symbol)
        except ExchangeError as exc:
            log.warning("scan.funding_failed", symbol=symbol, error=str(exc))
            continue  # unknown funding fails the regime filter closed

        signal = evaluate(
            symbol=symbol,
            bars=bars,
            funding_rate=funding,
            params=params,
            in_settlement_blackout=in_blackout,
        )
        if signal is not None:
            log.info("signal.found", symbol=symbol, signal=signal.describe())
            return signal

    return None


async def try_enter(
    client: BitgetClient,
    config: AppConfig,
    state: AccountState,
    signal: Signal,
    log,
    limiter: RateLimiter,
) -> bool:
    """Size the signal and place the protected entry. Returns True if anything filled."""
    if state.equity is None or state.equity <= 0:
        log.error(
            "entry.skipped_no_equity",
            symbol=signal.symbol,
            note="cannot size a trade without equity; failing closed",
        )
        return False

    amount = position_size(
        equity=state.equity,
        risk_pct=config.settings.risk.risk_pct,
        entry_price=signal.entry_price,
        stop_price=signal.stop_price,
    )
    amount = client.amount_to_precision(signal.symbol, amount)

    minimum = client.min_amount(signal.symbol)
    if minimum is not None and amount < minimum:
        # Do NOT round up to the minimum. That would silently exceed the risk
        # ceiling, which is the one thing the ceiling exists to prevent.
        log.warning(
            "entry.below_min_size",
            symbol=signal.symbol,
            computed=str(amount),
            venue_minimum=str(minimum),
            note=(
                f"{config.settings.risk.risk_pct}% of equity does not buy the venue "
                "minimum on this symbol. Skipping rather than rounding up past the "
                "risk ceiling."
            ),
        )
        return False

    if amount <= 0:
        log.warning("entry.zero_size", symbol=signal.symbol)
        return False

    result = await place_entry_with_protection(
        client=client,
        symbol=signal.symbol,
        side=signal.side,
        amount=amount,
        price=signal.entry_price,
        stop_price=signal.stop_price,
        take_profit_price=signal.target_price,
        signal_bar_ts=signal.signal_bar_ts,
        log=log,
        limiter=limiter,
    )
    return result.any_fill


async def run_iteration(
    client: BitgetClient,
    config: AppConfig,
    state: AccountState,
    trader: Trader,
    log,
    limiter: RateLimiter,
) -> None:
    """
    One full pass. Mutates `trader` in place.

    Raises UnprotectedPositionError if a position could not be protected or
    closed -- the caller must halt on that, not continue.
    """
    await handle_open_positions(client, state, log, limiter)

    for symbol in client.symbols:
        try:
            await cancel_expired_entries(
                client, symbol, state.fetched_at_ms, state.fetched_at_ms, log, limiter
            )
        except DemoModeRefusal:
            pass
        except (ExecutionError, ExchangeError) as exc:
            log.error("entry.expiry_cancel_failed", symbol=symbol, error=str(exc))

    ctx = build_guard_context(state, trader.order_timestamps)
    verdict = guards.check_all(ctx)
    if not verdict.allowed:
        log.info("entry.blocked", reason=verdict.reason)
        return

    signal = await scan_for_signal(client, config, state, trader, log, limiter)
    if signal is None:
        return

    try:
        await try_enter(client, config, state, signal, log, limiter)
        trader.order_timestamps = trader.order_timestamps + (state.fetched_at_ms,)
    except DemoModeRefusal as exc:
        log.info(
            "entry.demo_refused",
            symbol=signal.symbol,
            signal=signal.describe(),
            note=str(exc),
        )
    except UnprotectedPositionError:
        raise
    except (ExecutionError, ExchangeError) as exc:
        log.error("entry.failed", symbol=signal.symbol, error=str(exc))
