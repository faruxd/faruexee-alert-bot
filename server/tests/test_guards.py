"""
Every guard, independently, plus the restart property that matters most:
a process restart must NOT reset the daily entry counter or the loss limit.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from cf_bot import guards
from cf_bot.guards import (
    DAILY_LOSS_LIMIT_PCT,
    MAX_CONCURRENT_POSITIONS,
    MAX_CONSECUTIVE_LOSSES,
    MAX_ENTRIES_PER_UTC_DAY,
    MAX_ORDERS_PER_HOUR,
    GuardContext,
    check_all,
    consecutive_losses,
    count_entries_today,
    day_start_equity,
    guard_daily_loss_limit,
    guard_max_concurrent_positions,
    guard_max_entries_per_day,
    guard_max_orders_per_hour,
    guard_settlement_blackout,
    in_settlement_blackout,
    should_flatten_for_settlement,
)
from cf_bot.ids import PURPOSE_ENTRY, PURPOSE_STOP, client_order_id
from cf_bot.state import ClosedPosition, Fill, Position


def ts(hour: int, minute: int = 0, day: int = 15) -> int:
    return int(
        datetime(2026, 6, day, hour, minute, tzinfo=timezone.utc).timestamp() * 1000
    )


def make_position(symbol="BTC/USDT:USDT", contracts="0.01") -> Position:
    return Position(
        symbol=symbol,
        side="long",
        contracts=Decimal(contracts),
        entry_price=Decimal("64000"),
        mark_price=Decimal("64100"),
        liquidation_price=None,
        unrealized_pnl=Decimal("1"),
        margin_mode="isolated",
        leverage=Decimal("10"),
    )


def make_fill(client_oid=None, order_id="o1", trade_id="t1") -> Fill:
    return Fill(
        trade_id=trade_id,
        order_id=order_id,
        client_order_id=client_oid,
        symbol="BTC/USDT:USDT",
        side="buy",
        price=Decimal("64000"),
        amount=Decimal("0.01"),
        cost=Decimal("640"),
        fee_cost=Decimal("0.4"),
        fee_currency="USDT",
        timestamp_ms=ts(10),
    )


def make_closed(pnl, closed_at=None) -> ClosedPosition:
    return ClosedPosition(
        symbol="BTC/USDT:USDT",
        side="long",
        realised_pnl=Decimal(pnl),
        closed_at_ms=closed_at or ts(10),
    )


def make_ctx(**overrides) -> GuardContext:
    base = dict(
        now_ms=ts(12),
        positions=(),
        todays_fills=(),
        todays_closed_positions=(),
        recent_order_timestamps_ms=(),
        current_equity=Decimal("1000"),
    )
    base.update(overrides)
    return GuardContext(**base)


# --- max concurrent positions ----------------------------------------------


def test_flat_account_allows_an_entry():
    assert guard_max_concurrent_positions(make_ctx()).allowed is True


def test_one_open_position_blocks_a_second():
    ctx = make_ctx(positions=(make_position(),))
    assert guard_max_concurrent_positions(ctx).allowed is False


def test_the_limit_is_global_not_per_symbol():
    """
    A position on BTC blocks an entry on ETH.

    With 8 symbols at 1% risk each, per-symbol limits would put 8% at risk at
    once and would breach the -2% daily loss limit on the first two losers.
    """
    ctx = make_ctx(positions=(make_position(symbol="BTC/USDT:USDT"),))
    assert MAX_CONCURRENT_POSITIONS == 1
    assert guard_max_concurrent_positions(ctx).allowed is False


def test_zero_size_positions_do_not_count():
    ctx = make_ctx(positions=(make_position(contracts="0"),))
    assert guard_max_concurrent_positions(ctx).allowed is True


# --- daily entry cap -------------------------------------------------------


def test_entries_are_counted_from_positions_not_fills():
    """
    REGRESSION. Counting fills by client order id broke in production: an exit
    through a preset TP/SL carries BITGET's order id, not ours, so every exit
    was counted as a fresh entry. One completed round trip reported "4/3
    entries" and locked the bot out for the rest of the day.
    """
    closed = tuple(make_closed("-0.1", closed_at=ts(10) + i) for i in range(3))
    assert count_entries_today(closed, ()) == MAX_ENTRIES_PER_UTC_DAY
    assert guard_max_entries_per_day(
        make_ctx(todays_closed_positions=closed)
    ).allowed is False


def test_exit_fills_do_not_inflate_the_count():
    """A completed round trip is ONE entry, however many fills it produced."""
    one_round_trip = (make_closed("-0.7", closed_at=ts(10)),)
    noisy_fills = tuple(
        make_fill(client_oid=None, order_id=f"bitget-generated-{i}", trade_id=f"t{i}")
        for i in range(11)
    )
    verdict = guard_max_entries_per_day(
        make_ctx(todays_closed_positions=one_round_trip, todays_fills=noisy_fills)
    )
    assert verdict.allowed is True, "exit fills were counted as entries again"
    assert "1/3" in verdict.reason


def test_an_open_position_counts_as_an_entry():
    assert count_entries_today((), (make_position(),)) == 1


def test_zero_size_positions_are_not_entries():
    assert count_entries_today((), (make_position(contracts="0"),)) == 0


def test_open_and_closed_positions_are_summed():
    closed = (make_closed("-0.1"), make_closed("0.3"))
    assert count_entries_today(closed, (make_position(),)) == 3


def test_a_flat_day_with_no_history_allows_trading():
    assert guard_max_entries_per_day(make_ctx()).allowed is True


def test_a_restart_does_not_reset_the_daily_entry_counter():
    """
    The whole point of deriving from the exchange. Two independently built
    contexts -- as a fresh process would produce -- see the same count.
    """
    closed = tuple(make_closed("-0.1", closed_at=ts(10) + i) for i in range(3))
    before = guard_max_entries_per_day(make_ctx(todays_closed_positions=closed))
    after = guard_max_entries_per_day(make_ctx(todays_closed_positions=closed))
    assert before.allowed is False
    assert after.allowed is False


# --- orders per hour -------------------------------------------------------


def test_orders_per_hour_blocks_a_runaway_loop():
    now = ts(12)
    stamps = tuple(now - i * 1000 for i in range(MAX_ORDERS_PER_HOUR))
    ctx = make_ctx(now_ms=now, recent_order_timestamps_ms=stamps)
    assert guard_max_orders_per_hour(ctx).allowed is False


def test_orders_older_than_an_hour_do_not_count():
    now = ts(12)
    stale = tuple(now - 3_600_001 - i for i in range(MAX_ORDERS_PER_HOUR * 2))
    ctx = make_ctx(now_ms=now, recent_order_timestamps_ms=stale)
    assert guard_max_orders_per_hour(ctx).allowed is True


# --- daily loss limit ------------------------------------------------------


def test_day_start_equity_is_reconstructed_from_realised_pnl():
    assert day_start_equity(Decimal("980"), Decimal("-20")) == Decimal("1000")


def test_loss_at_the_limit_blocks():
    """-2% of a 1000 open is -20."""
    ctx = make_ctx(
        current_equity=Decimal("980"),
        todays_closed_positions=(make_closed("-20"),),
    )
    verdict = guard_daily_loss_limit(ctx)
    assert verdict.allowed is False
    assert "daily PnL" in verdict.reason


def test_loss_below_the_limit_allows():
    ctx = make_ctx(
        current_equity=Decimal("990"), todays_closed_positions=(make_closed("-10"),)
    )
    assert guard_daily_loss_limit(ctx).allowed is True


def test_three_consecutive_losses_block_regardless_of_size():
    """Even tiny losses: three in a row disables the day."""
    closed = tuple(
        make_closed("-0.01", closed_at=ts(10) + i) for i in range(MAX_CONSECUTIVE_LOSSES)
    )
    ctx = make_ctx(current_equity=Decimal("999"), todays_closed_positions=closed)
    verdict = guard_daily_loss_limit(ctx)
    assert verdict.allowed is False
    assert "consecutive losses" in verdict.reason


def test_a_win_resets_the_losing_streak():
    closed = (
        make_closed("-1", closed_at=ts(9)),
        make_closed("-1", closed_at=ts(10)),
        make_closed("+5", closed_at=ts(11)),  # most recent
    )
    assert consecutive_losses(closed) == 0


def test_streak_counts_backwards_from_the_most_recent_close():
    closed = (
        make_closed("+5", closed_at=ts(9)),
        make_closed("-1", closed_at=ts(10)),
        make_closed("-1", closed_at=ts(11)),
    )
    assert consecutive_losses(closed) == 2


def test_unknown_equity_fails_closed():
    """Cannot evaluate the limit, so we do not permit the trade."""
    ctx = make_ctx(current_equity=None, todays_closed_positions=(make_closed("-5"),))
    verdict = guard_daily_loss_limit(ctx)
    assert verdict.allowed is False
    assert "failing closed" in verdict.reason


def test_nonsensical_day_start_equity_fails_closed():
    ctx = make_ctx(current_equity=Decimal("10"), todays_closed_positions=(make_closed("50"),))
    assert guard_daily_loss_limit(ctx).allowed is False


def test_the_limit_survives_a_restart():
    closed = (make_closed("-20"),)
    ctx_a = make_ctx(current_equity=Decimal("980"), todays_closed_positions=closed)
    ctx_b = make_ctx(current_equity=Decimal("980"), todays_closed_positions=closed)
    assert guard_daily_loss_limit(ctx_a).allowed is False
    assert guard_daily_loss_limit(ctx_b).allowed is False


# --- settlement ------------------------------------------------------------


@pytest.mark.parametrize("hour,minute", [(0, 0), (8, 0), (16, 0), (7, 50), (8, 10), (23, 55)])
def test_blackout_windows(hour, minute):
    assert in_settlement_blackout(ts(hour, minute)) is True


@pytest.mark.parametrize("hour,minute", [(4, 0), (12, 0), (20, 0), (7, 40), (8, 20)])
def test_outside_blackout_windows(hour, minute):
    assert in_settlement_blackout(ts(hour, minute)) is False


def test_blackout_blocks_entries():
    assert guard_settlement_blackout(make_ctx(now_ms=ts(8, 5))).allowed is False


def test_no_blackout_allows_entries():
    assert guard_settlement_blackout(make_ctx(now_ms=ts(12))).allowed is True


@pytest.mark.parametrize("hour,minute", [(7, 59), (15, 58), (23, 59)])
def test_flatten_window_fires_just_before_settlement(hour, minute):
    assert should_flatten_for_settlement(ts(hour, minute)) is True


@pytest.mark.parametrize("hour,minute", [(7, 50), (12, 0), (8, 5)])
def test_flatten_window_does_not_fire_elsewhere(hour, minute):
    assert should_flatten_for_settlement(ts(hour, minute)) is False


# --- composition -----------------------------------------------------------


def test_check_all_passes_a_clean_context():
    assert check_all(make_ctx()).allowed is True


def test_check_all_reports_which_guard_blocked():
    ctx = make_ctx(positions=(make_position(),))
    verdict = check_all(ctx)
    assert verdict.allowed is False
    assert "guard_max_concurrent_positions" in verdict.reason


def test_any_single_block_is_fatal_to_the_entry():
    ctx = make_ctx(now_ms=ts(8, 5))  # only the blackout is violated
    assert check_all(ctx).allowed is False


def test_every_guard_is_covered_by_this_file():
    """
    If someone adds a guard, this fails until it is tested.

    The spec requires every guard to be independently unit-tested; this makes
    that a check rather than a hope.
    """
    tested = {
        "guard_position_history_sane",
        "guard_max_concurrent_positions",
        "guard_max_entries_per_day",
        "guard_max_orders_per_hour",
        "guard_daily_loss_limit",
        "guard_settlement_blackout",
    }
    actual = {g.__name__ for g in guards.ALL_GUARDS}
    assert actual == tested, f"untested guards: {actual - tested}"


# --- data integrity --------------------------------------------------------


def test_fills_with_no_closed_and_no_open_positions_blocks():
    """
    REGRESSION. ccxt's bitget fetch_positions_history uses only symbols[0], so
    passing 20 symbols queried BTC alone. A day of LTC/PEPE/UNI closes read as
    zero, which emptied the data BOTH the entry cap and the loss limit derive
    from -- so both failed OPEN and the bot traded past its limits.

    Fills today with nothing closed and nothing open is impossible. It means
    the history feed is lying, and the answer is to stop.
    """
    ctx = make_ctx(todays_fills=(make_fill(),), todays_closed_positions=(), positions=())
    verdict = guards.guard_position_history_sane(ctx)
    assert verdict.allowed is False
    assert "cannot be trusted" in verdict.reason


def test_fills_with_an_open_position_are_consistent():
    """Mid-trade: fills exist, nothing closed yet, one position open. Fine."""
    ctx = make_ctx(
        todays_fills=(make_fill(),), todays_closed_positions=(), positions=(make_position(),)
    )
    assert guards.guard_position_history_sane(ctx).allowed is True


def test_fills_with_a_closed_position_are_consistent():
    ctx = make_ctx(todays_fills=(make_fill(),), todays_closed_positions=(make_closed("-1"),))
    assert guards.guard_position_history_sane(ctx).allowed is True


def test_a_quiet_day_with_no_fills_is_consistent():
    assert guards.guard_position_history_sane(make_ctx()).allowed is True


def test_the_integrity_check_runs_before_the_limits():
    """
    It must come first. The guards after it are evaluating numbers derived from
    the very data it is checking.
    """
    names = [g.__name__ for g in guards.ALL_GUARDS]
    assert names[0] == "guard_position_history_sane"
    assert names.index("guard_position_history_sane") < names.index("guard_daily_loss_limit")
