"""Startup assertions. Each must fail closed when it cannot prove the safe answer."""

from __future__ import annotations

import pytest

from cf_bot.preflight import (
    PreflightError,
    assert_cannot_withdraw,
    assert_one_way_position_mode,
)
from tests.conftest import FakeBitgetClient


# --- position mode ---------------------------------------------------------


async def test_one_way_mode_passes():
    assert await assert_one_way_position_mode(FakeBitgetClient()) == "one_way_mode"


async def test_hedge_mode_is_refused():
    client = FakeBitgetClient(position_mode="hedge_mode")
    with pytest.raises(PreflightError) as exc:
        await assert_one_way_position_mode(client)
    assert "hedge_mode" in str(exc.value)


@pytest.mark.parametrize("weird", ["", "oneway", "ONE_WAY_MODE", "single", None])
async def test_unrecognised_position_mode_is_refused(weird):
    client = FakeBitgetClient(position_mode=weird)
    with pytest.raises(PreflightError):
        await assert_one_way_position_mode(client)


async def test_position_mode_lookup_failure_is_refused(exchange_error):
    client = FakeBitgetClient(position_mode=exchange_error)
    with pytest.raises(PreflightError) as exc:
        await assert_one_way_position_mode(client)
    assert "could not determine position mode" in str(exc.value)


# --- permissions -----------------------------------------------------------


async def test_read_and_trade_key_passes():
    assert await assert_cannot_withdraw(FakeBitgetClient()) == ("readonly", "trade")


@pytest.mark.parametrize(
    "authorities",
    [
        ("readonly", "trade", "withdraw"),
        ("withdraw",),
        ("readonly", "transfer"),
        ("readonly", "trade", "WITHDRAW"),
        ("readonly", "trade", "withdrawal"),
        ("readonly", "trade", "spot_transfer"),
    ],
)
async def test_key_with_fund_moving_permission_is_refused(authorities):
    client = FakeBitgetClient(authorities=authorities)
    with pytest.raises(PreflightError) as exc:
        await assert_cannot_withdraw(client)
    assert "forbidden permission" in str(exc.value)


async def test_empty_permission_list_is_refused():
    """Cannot prove the key is safe, so we do not start."""
    client = FakeBitgetClient(authorities=())
    with pytest.raises(PreflightError) as exc:
        await assert_cannot_withdraw(client)
    assert "empty permission list" in str(exc.value)


async def test_permission_lookup_failure_is_refused(exchange_error):
    client = FakeBitgetClient(authorities=exchange_error)
    with pytest.raises(PreflightError) as exc:
        await assert_cannot_withdraw(client)
    assert "cannot prove" in str(exc.value)
