"""
The loop body: bar caching, exit work, and the end-to-end signal -> entry path.
Still no network.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from cf_bot.config import AppConfig, load_settings
from cf_bot.logging_setup import get_logger
from cf_bot.orders import RateLimiter
from cf_bot.state import AccountState, Position
from cf_bot.strategy import BAR_MS_5M
from cf_bot.trader import (
    REFRESH_BARS,
    REQUIRED_BARS,
    BarCache,
    Trader,
    handle_open_positions,
    run_iteration,
    scan_for_signal,
    time_stop_due,
    try_enter,
)
from tests.conftest import FakeBitgetClient


@pytest.fixture
def limiter():
    return RateLimiter(rate_per_second=10_000, burst=10_000)


@pytest.fixture
def log():
    return get_logger("test")


def ohlcv_row(index: int, o=100.0, h=101.0, low=99.0, c=100.0):
    return [index * BAR_MS_5M, o, h, low, c, 10.0]


@pytest.fixture(scope="module")
def flat_rows():
    """Enough closed bars to fill the percentile window, plus a forming candle."""
    return [ohlcv_row(i) for i in range(REQUIRED_BARS + 1)]


@pytest.fixture(scope="module")
def cascade_rows(flat_rows):
    """Same history, but the last CLOSED bar is a 6-point up-cascade."""
    rows = [list(r) for r in flat_rows]
    last_closed = len(rows) - 2
    rows[last_closed] = ohlcv_row(last_closed, o=100.0, h=106.0, low=100.0, c=106.0)
    return rows


@pytest.fixture
def app_config(write_config, valid_config_yaml, tmp_path) -> AppConfig:
    return AppConfig(
        settings=load_settings(write_config(valid_config_yaml)),
        mode="live",
        working_dir=tmp_path,
    )


def make_state(**overrides) -> AccountState:
    base = dict(
        fetched_at_ms=int(datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc).timestamp() * 1000),
        mode="live",
        position_mode="one_way_mode",
        equity=Decimal("1000"),
        available=Decimal("1000"),
        positions=(),
        open_orders=(),
        todays_fills=(),
        todays_closed_positions=(),
    )
    base.update(overrides)
    return AccountState(**base)


def raw_position(symbol="BTC/USDT:USDT", contracts=0.01):
    """A complete ccxt position payload, as the venue actually returns one."""
    return {
        "symbol": symbol,
        "side": "long",
        "contracts": contracts,
        "entryPrice": 64000.0,
        "markPrice": 64100.0,
        "liquidationPrice": 58000.0,
        "unrealizedPnl": 1.0,
        "marginMode": "isolated",
        "leverage": 10,
        "timestamp": 1717000000000,
        "info": {"presetStopLossPrice": "62000"},
    }


def make_position(opened_at_ms=None, symbol="BTC/USDT:USDT") -> Position:
    return Position(
        symbol=symbol,
        side="long",
        contracts=Decimal("0.01"),
        entry_price=Decimal("64000"),
        mark_price=Decimal("64100"),
        liquidation_price=None,
        unrealized_pnl=Decimal("1"),
        margin_mode="isolated",
        leverage=Decimal("10"),
        opened_at_ms=opened_at_ms,
    )


# --- bar cache -------------------------------------------------------------


async def test_cache_drops_the_forming_candle(flat_rows, limiter):
    """
    The last row from the venue is the candle still forming. Acting on it means
    acting on a close that has not happened.
    """
    client = FakeBitgetClient(ohlcv=flat_rows)
    bars = await BarCache().refresh(client, "BTC/USDT:USDT", "5m", REQUIRED_BARS, limiter)

    assert len(bars) == len(flat_rows) - 1
    assert bars[-1].timestamp_ms == flat_rows[-2][0]


async def test_cache_fetches_full_history_then_tops_up(flat_rows, limiter):
    """
    The whole reason the cache exists: 8600+ bars once, then a small tail.

    Without this, an 8-symbol scan on a 15s loop would issue ~72 paginated
    requests every iteration and never keep up.
    """
    client = FakeBitgetClient(ohlcv=flat_rows)
    requested: list[int] = []
    real_fetch = client.fetch_ohlcv

    async def record(symbol, timeframe, limit, since=None):
        requested.append(limit)
        return await real_fetch(symbol, timeframe, limit, since)

    client.fetch_ohlcv = record
    cache = BarCache()

    await cache.refresh(client, "BTC/USDT:USDT", "5m", REQUIRED_BARS, limiter)
    await cache.refresh(client, "BTC/USDT:USDT", "5m", REQUIRED_BARS, limiter)

    assert requested[0] == REQUIRED_BARS
    assert requested[1] == REFRESH_BARS


async def test_cache_is_capped(flat_rows, limiter):
    client = FakeBitgetClient(ohlcv=flat_rows)
    bars = await BarCache().refresh(client, "BTC/USDT:USDT", "5m", REQUIRED_BARS, limiter)
    assert len(bars) <= REQUIRED_BARS


async def test_cache_merges_without_duplicating(flat_rows, limiter):
    client = FakeBitgetClient(ohlcv=flat_rows)
    cache = BarCache()
    first = await cache.refresh(client, "BTC/USDT:USDT", "5m", REQUIRED_BARS, limiter)
    second = await cache.refresh(client, "BTC/USDT:USDT", "5m", REQUIRED_BARS, limiter)

    assert len(first) == len(second)
    timestamps = [b.timestamp_ms for b in second]
    assert len(timestamps) == len(set(timestamps))


async def test_empty_response_leaves_the_cache_intact(limiter):
    client = FakeBitgetClient(ohlcv=[])
    assert await BarCache().refresh(client, "BTC/USDT:USDT", "5m", REQUIRED_BARS, limiter) == []


# --- re-evaluation gating --------------------------------------------------


async def test_symbol_is_not_re_evaluated_without_a_new_bar(
    cascade_rows, app_config, limiter, log
):
    client = FakeBitgetClient(ohlcv=cascade_rows)
    trader = Trader()
    state = make_state()

    first = await scan_for_signal(client, app_config, state, trader, log, limiter)
    second = await scan_for_signal(client, app_config, state, trader, log, limiter)

    assert first is not None, "the cascade should produce a signal"
    assert second is None, "same bar re-evaluated -- would double-enter"


# --- time stop -------------------------------------------------------------


def test_time_stop_fires_after_twelve_bars():
    now = 1_000_000_000_000
    assert time_stop_due(make_position(opened_at_ms=now - 12 * BAR_MS_5M), now) is True
    assert time_stop_due(make_position(opened_at_ms=now - 11 * BAR_MS_5M), now) is False


def test_a_position_with_no_open_time_is_not_force_closed():
    """
    Cannot be aged, so it is left alone -- it still has its exchange-side stop
    and target. Closing on a guess is worse.
    """
    assert time_stop_due(make_position(opened_at_ms=None), 1_000_000_000_000) is False


async def test_time_stop_flattens(limiter, log):
    now = int(datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    client = FakeBitgetClient(positions=[raw_position()])
    state = make_state(
        fetched_at_ms=now, positions=(make_position(opened_at_ms=now - 13 * BAR_MS_5M),)
    )

    assert await handle_open_positions(client, state, log, limiter) is True
    assert "BTC/USDT:USDT" in client.cancel_all_calls


async def test_settlement_flatten_fires_before_the_boundary(limiter, log):
    """23:59 UTC is inside the 2-minute pre-settlement flatten window."""
    now = int(datetime(2026, 6, 15, 23, 59, tzinfo=timezone.utc).timestamp() * 1000)
    client = FakeBitgetClient(positions=[raw_position()])
    state = make_state(fetched_at_ms=now, positions=(make_position(opened_at_ms=now),))

    assert await handle_open_positions(client, state, log, limiter) is True


async def test_a_fresh_position_outside_settlement_is_left_alone(limiter, log):
    now = int(datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    client = FakeBitgetClient(positions=[raw_position()])
    state = make_state(fetched_at_ms=now, positions=(make_position(opened_at_ms=now),))

    assert await handle_open_positions(client, state, log, limiter) is False
    assert client.sent_orders == []


# --- sizing on entry -------------------------------------------------------


async def test_entry_is_sized_to_risk_exactly_one_percent(
    cascade_rows, app_config, limiter, log
):
    client = FakeBitgetClient(ohlcv=cascade_rows)
    trader = Trader()
    state = make_state(equity=Decimal("10000"))

    signal = await scan_for_signal(client, app_config, state, trader, log, limiter)
    assert signal is not None

    await try_enter(client, app_config, state, signal, log, limiter)

    sent = client.sent_orders[0]
    risk = abs(signal.entry_price - signal.stop_price) * sent["amount"]
    # 1% of 10000 = 100, within the rounding imposed by amount precision.
    assert abs(risk - Decimal("100")) < Decimal("1")


async def test_entry_below_the_venue_minimum_is_skipped_not_rounded_up(
    cascade_rows, app_config, limiter, log
):
    """
    Rounding up to the venue minimum would silently exceed the risk ceiling --
    the one thing the ceiling exists to prevent.
    """
    client = FakeBitgetClient(ohlcv=cascade_rows)
    client.min_amount = lambda symbol: Decimal("1000")
    trader = Trader()
    state = make_state(equity=Decimal("10"))

    signal = await scan_for_signal(client, app_config, state, trader, log, limiter)
    assert await try_enter(client, app_config, state, signal, log, limiter) is False
    assert client.sent_orders == []


async def test_no_equity_means_no_entry(cascade_rows, app_config, limiter, log):
    client = FakeBitgetClient(ohlcv=cascade_rows)
    trader = Trader()
    state = make_state(equity=None)

    signal = await scan_for_signal(client, app_config, state, trader, log, limiter)
    assert await try_enter(client, app_config, state, signal, log, limiter) is False
    assert client.sent_orders == []


# --- guard integration -----------------------------------------------------


async def test_a_blocking_guard_prevents_any_order(cascade_rows, app_config, limiter, log):
    client = FakeBitgetClient(ohlcv=cascade_rows)
    trader = Trader()
    # An open position blocks entries via the max-concurrent guard.
    state = make_state(positions=(make_position(opened_at_ms=make_state().fetched_at_ms),))

    await run_iteration(client, app_config, state, trader, log, limiter)
    assert not any(o["kind"] == "entry" for o in client.sent_orders)


async def test_exit_work_still_runs_when_guards_block(limiter, log, app_config):
    """A daily loss limit stops new trades; it must not strand an open one."""
    now = int(datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    client = FakeBitgetClient(positions=[raw_position()])
    trader = Trader()
    state = make_state(
        fetched_at_ms=now,
        positions=(make_position(opened_at_ms=now - 13 * BAR_MS_5M),),  # time stop due
    )

    await run_iteration(client, app_config, state, trader, log, limiter)
    assert "BTC/USDT:USDT" in client.cancel_all_calls, "exit work was skipped"


async def test_a_clean_signal_produces_exactly_one_entry(
    cascade_rows, app_config, limiter, log
):
    client = FakeBitgetClient(ohlcv=cascade_rows)
    trader = Trader()
    state = make_state()

    await run_iteration(client, app_config, state, trader, log, limiter)

    entries = [o for o in client.sent_orders if o["kind"] == "entry"]
    assert len(entries) == 1
    assert entries[0]["stop_price"] is not None
    assert entries[0]["take_profit_price"] is not None
