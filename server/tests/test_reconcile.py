"""Normalisation of exchange payloads, and the all-or-nothing snapshot rule."""

from __future__ import annotations

from decimal import Decimal

import pytest

from cf_bot.exchange import ExchangeError
from cf_bot.reconcile import (
    ReconcileError,
    extract_equity,
    normalise_fill,
    normalise_order,
    normalise_position,
    reconcile,
)
from tests.conftest import FakeBitgetClient


# --- positions -------------------------------------------------------------


def test_normalise_long_position(ccxt_position_long):
    pos = normalise_position(ccxt_position_long)
    assert pos.side == "long"
    assert pos.contracts == Decimal("0.01")
    assert pos.entry_price == Decimal("64000.5")
    assert pos.leverage == Decimal("10")


def test_zero_size_position_is_not_a_position(ccxt_position_zero):
    """A leverage-config placeholder row must not make is_flat lie."""
    assert normalise_position(ccxt_position_zero) is None


def test_short_position_size_is_positive_with_direction_in_side(ccxt_position_long):
    raw = dict(ccxt_position_long, side="short", contracts=-0.05)
    pos = normalise_position(raw)
    assert pos.side == "short"
    assert pos.contracts == Decimal("0.05")


def test_unrecognised_side_raises_rather_than_guessing(ccxt_position_long):
    raw = dict(ccxt_position_long, side="both")
    with pytest.raises(ReconcileError) as exc:
        normalise_position(raw)
    assert "side" in str(exc.value)


def test_position_prices_are_decimal_not_float(ccxt_position_long):
    pos = normalise_position(ccxt_position_long)
    assert isinstance(pos.entry_price, Decimal)
    assert isinstance(pos.contracts, Decimal)


def test_missing_optional_price_becomes_none_not_zero(ccxt_position_long):
    """A missing liquidation price must not read as 0, which would look catastrophic."""
    raw = dict(ccxt_position_long, liquidationPrice=None)
    assert normalise_position(raw).liquidation_price is None


# --- orders ----------------------------------------------------------------


def test_normalise_open_order(ccxt_open_order):
    order = normalise_order(ccxt_open_order)
    assert order.order_id == "1234567890"
    assert order.reduce_only is False
    assert order.price == Decimal("63500.0")


def test_reduce_only_flag_is_preserved(ccxt_stop_order):
    assert normalise_order(ccxt_stop_order).reduce_only is True


def test_absent_reduce_only_is_false_not_assumed_true(ccxt_open_order):
    """We must not optimistically treat an unlabelled order as protective."""
    raw = dict(ccxt_open_order)
    raw["reduceOnly"] = None
    assert normalise_order(raw).reduce_only is False


def test_order_without_id_raises(ccxt_open_order):
    raw = dict(ccxt_open_order, id=None)
    with pytest.raises(ReconcileError):
        normalise_order(raw)


# --- fills -----------------------------------------------------------------


def test_normalise_fill(ccxt_trade):
    fill = normalise_fill(ccxt_trade)
    assert fill.trade_id == "t-1"
    assert fill.order_id == "1234567890"
    assert fill.fee_cost == Decimal("0.127")
    assert fill.fee_currency == "USDT"


def test_fill_without_fee_block_is_handled(ccxt_trade):
    raw = dict(ccxt_trade)
    del raw["fee"]
    fill = normalise_fill(raw)
    assert fill.fee_cost is None


# --- balance ---------------------------------------------------------------


def test_extract_equity():
    total, free = extract_equity({"USDT": {"total": "1000.5", "free": "980.25"}}, "USDT")
    assert total == Decimal("1000.5")
    assert free == Decimal("980.25")


def test_extract_equity_absent_coin():
    assert extract_equity({}, "USDT") == (None, None)


# --- whole-snapshot behaviour ----------------------------------------------


async def test_reconcile_builds_complete_snapshot(ccxt_position_long, ccxt_open_order, ccxt_trade):
    client = FakeBitgetClient(
        positions=[ccxt_position_long],
        open_orders=[ccxt_open_order],
        fills=[ccxt_trade],
    )
    state = await reconcile(client, "demo")

    assert state.position_mode == "one_way_mode"
    assert state.mode == "demo"
    assert len(state.positions) == 1
    assert len(state.open_orders) == 1
    assert len(state.todays_fills) == 1
    assert state.equity == Decimal("1000.5")
    assert state.is_flat is False


async def test_reconcile_flat_account():
    state = await reconcile(FakeBitgetClient(), "demo")
    assert state.is_flat is True
    assert state.positions == ()


async def test_reconcile_sees_a_manually_opened_position(ccxt_position_long):
    """Acceptance: open a position in the Bitget UI, the bot must see it."""
    client = FakeBitgetClient()
    assert (await reconcile(client, "demo")).is_flat is True

    client.positions = [ccxt_position_long]  # someone opened it by hand
    state = await reconcile(client, "demo")
    assert state.is_flat is False
    assert state.positions[0].describe() == "long 0.01 BTC/USDT:USDT @ 64000.5"


@pytest.mark.parametrize(
    "leg",
    ["position_mode", "balance", "positions", "open_orders", "fills"],
)
async def test_any_failed_leg_fails_the_whole_snapshot(leg, exchange_error):
    """
    No partial snapshots. "No positions found" and "the positions call errored"
    must never be indistinguishable to downstream code.
    """
    client = FakeBitgetClient()
    setattr(client, leg, exchange_error)
    with pytest.raises(ReconcileError):
        await reconcile(client, "demo")


async def test_reconcile_is_identical_across_a_restart(ccxt_position_long, ccxt_open_order, ccxt_trade):
    """
    Acceptance: kill it mid-run, restart, it reports identical state.

    Phase 1 persists nothing, so two independent reconciles against the same
    exchange state must compare equal. Mark price and PnL are excluded because
    they move with the market.
    """
    payload = dict(
        positions=[ccxt_position_long],
        open_orders=[ccxt_open_order],
        fills=[ccxt_trade],
    )
    before = await reconcile(FakeBitgetClient(**payload), "demo")

    moved = dict(ccxt_position_long, markPrice=70000.0, unrealizedPnl=55.0)
    after = await reconcile(
        FakeBitgetClient(positions=[moved], open_orders=[ccxt_open_order], fills=[ccxt_trade]),
        "demo",
    )

    assert before.comparable() == after.comparable()


async def test_snapshot_differs_when_the_position_actually_changes(ccxt_position_long):
    before = await reconcile(FakeBitgetClient(positions=[ccxt_position_long]), "demo")
    bigger = dict(ccxt_position_long, contracts=0.02)
    after = await reconcile(FakeBitgetClient(positions=[bigger]), "demo")
    assert before.comparable() != after.comparable()
