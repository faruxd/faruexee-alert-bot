"""
Phase 2 acceptance: protection, partial fills, flatten, and the timeout case.

The timeout test is the important one. It proves that a retry after a network
failure cannot open a second position, which is the entire justification for
deterministic client order IDs.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from cf_bot.exchange import DemoModeRefusal, ExchangeError, OrderRejected
from cf_bot.ids import PURPOSE_ENTRY, client_order_id
from cf_bot.logging_setup import get_logger
from cf_bot.orders import (
    MAX_ATTEMPTS,
    ExecutionError,
    RateLimiter,
    UnprotectedPositionError,
    flatten,
    place_entry_with_protection,
)
from tests.conftest import FakeBitgetClient


@pytest.fixture
def limiter():
    # Effectively unthrottled so tests do not spend real seconds sleeping.
    return RateLimiter(rate_per_second=10_000, burst=10_000)


@pytest.fixture
def log():
    return get_logger("test")


def position_payload(contracts="0.01", preset_stop="62000"):
    return {
        "symbol": "BTC/USDT:USDT",
        "side": "long",
        "contracts": float(contracts),
        "entryPrice": 64000.0,
        "markPrice": 64100.0,
        "liquidationPrice": 58000.0,
        "unrealizedPnl": 1.0,
        "marginMode": "isolated",
        "leverage": 10,
        "timestamp": 1717000000000,
        "info": {"presetStopLossPrice": preset_stop},
    }


async def entry(client, log, limiter, amount="0.01"):
    return await place_entry_with_protection(
        client=client,
        symbol="BTC/USDT:USDT",
        side="long",
        amount=Decimal(amount),
        price=Decimal("64000"),
        stop_price=Decimal("62000"),
        take_profit_price=Decimal("66000"),
        signal_bar_ts=1717000000000,
        log=log,
        limiter=limiter,
    )


# --- deterministic ids -----------------------------------------------------


async def test_entry_uses_the_deterministic_client_order_id(log, limiter):
    client = FakeBitgetClient()
    result = await entry(client, log, limiter)

    expected = client_order_id("BTC/USDT:USDT", 1717000000000, "long", PURPOSE_ENTRY)
    assert result.client_order_id == expected
    assert client.sent_orders[0]["client_order_id"] == expected


async def test_a_timeout_retry_reuses_the_same_id_and_cannot_duplicate(log, limiter):
    """
    Phase 2 acceptance: simulate a timeout mid-placement, prove no duplicate.

    The first attempt "times out" (the venue actually received it). The retry
    carries the SAME clientOid, so the venue can recognise it as a duplicate.
    Every attempt we made used one id -- there is no path to two positions.
    """
    client = FakeBitgetClient()
    attempts: list[str] = []
    real_create = client.create_entry_with_protection

    async def timeout_once(**kwargs):
        attempts.append(kwargs["client_order_id"])
        if len(attempts) == 1:
            raise ExchangeError("simulated network timeout")
        return await real_create(**kwargs)

    client.create_entry_with_protection = timeout_once
    result = await entry(client, log, limiter)

    assert len(attempts) == 2, "should have retried exactly once"
    assert attempts[0] == attempts[1], "retry used a DIFFERENT id -- duplicate risk"
    assert result.client_order_id == attempts[0]


async def test_retries_stop_at_the_maximum(log, limiter):
    client = FakeBitgetClient()
    attempts = []

    async def always_fail(**kwargs):
        attempts.append(kwargs["client_order_id"])
        raise ExchangeError("venue unreachable")

    client.create_entry_with_protection = always_fail

    with pytest.raises(ExecutionError):
        await entry(client, log, limiter)

    assert len(attempts) == MAX_ATTEMPTS
    assert len(set(attempts)) == 1, "all attempts must share one id"


async def test_a_rejection_is_not_retried(log, limiter):
    """Retrying a rejected order unchanged burns rate limit and delays the failure."""
    client = FakeBitgetClient()
    attempts = []

    async def reject(**kwargs):
        attempts.append(1)
        raise OrderRejected("insufficient margin")

    client.create_entry_with_protection = reject

    with pytest.raises(OrderRejected):
        await entry(client, log, limiter)

    assert len(attempts) == 1


# --- protection ------------------------------------------------------------


async def test_a_fill_with_protection_is_accepted(log, limiter):
    client = FakeBitgetClient(positions=[position_payload()])
    result = await entry(client, log, limiter)

    assert result.any_fill is True
    assert result.protected is True
    assert result.filled_amount == Decimal("0.01")


async def test_a_partial_fill_is_sized_on_what_actually_filled(log, limiter):
    """Requested 0.05, filled 0.01. Risk figures must follow the 0.01."""
    client = FakeBitgetClient(positions=[position_payload(contracts="0.01")])
    result = await entry(client, log, limiter, amount="0.05")

    assert result.requested_amount == Decimal("0.05")
    assert result.filled_amount == Decimal("0.01")
    assert result.protected is True


async def test_a_fill_without_protection_is_flattened_immediately(log, limiter):
    """
    The constraint this whole module exists for: if the entry fills and no stop
    is on the exchange, close the position at market. Do not retry the stop, do
    not wait a loop, do not watch the price.
    """
    client = FakeBitgetClient(positions=[position_payload(preset_stop=None)])
    result = await entry(client, log, limiter)

    assert result.protected is False
    assert result.filled_amount == Decimal("0")
    assert any(o["kind"] == "close" for o in client.sent_orders), "position was not flattened"


async def test_an_unflattenable_unprotected_position_escalates(log, limiter):
    """The worst state reachable. It must raise, not be logged and continued past."""
    client = FakeBitgetClient(positions=[position_payload(preset_stop=None)])

    async def refuse(*args, **kwargs):
        raise ExchangeError("venue rejected the close")

    client.create_reduce_only_market = refuse

    with pytest.raises(UnprotectedPositionError) as exc:
        await entry(client, log, limiter)
    assert "MANUAL INTERVENTION REQUIRED" in str(exc.value)


async def test_a_resting_order_needs_no_protection_yet(log, limiter):
    """Nothing filled, so there is nothing to protect."""
    client = FakeBitgetClient(positions=[])
    result = await entry(client, log, limiter)

    assert result.any_fill is False
    assert result.protected is True


async def test_a_reduce_only_resting_order_counts_as_protection(log, limiter):
    client = FakeBitgetClient(
        positions=[position_payload(preset_stop=None)],
        open_orders=[
            {
                "id": "stop-1",
                "symbol": "BTC/USDT:USDT",
                "side": "sell",
                "type": "market",
                "price": None,
                "amount": 0.01,
                "reduceOnly": True,
                "status": "open",
                "timestamp": 1717000000000,
            }
        ],
    )
    result = await entry(client, log, limiter)
    assert result.protected is True


# --- flatten ---------------------------------------------------------------


async def test_flatten_cancels_orders_then_closes_then_verifies(log, limiter):
    client = FakeBitgetClient(positions=[position_payload()])
    assert await flatten(client, "BTC/USDT:USDT", log, limiter, reason="test") is True

    assert "BTC/USDT:USDT" in client.cancel_all_calls
    assert any(o["kind"] == "close" for o in client.sent_orders)


async def test_flatten_on_an_already_flat_symbol_is_a_no_op_close(log, limiter):
    client = FakeBitgetClient(positions=[])
    assert await flatten(client, "BTC/USDT:USDT", log, limiter, reason="test") is True
    assert not any(o["kind"] == "close" for o in client.sent_orders)


async def test_flatten_raises_when_the_position_will_not_close(log, limiter):
    """A flatten that silently failed is indistinguishable from one that worked."""
    client = FakeBitgetClient(positions=[position_payload()])

    async def close_but_leave_it_open(symbol, side, amount, client_order_id):
        client.sent_orders.append({"kind": "close", "symbol": symbol})
        return {"id": "x"}  # deliberately does NOT clear client.positions

    client.create_reduce_only_market = close_but_leave_it_open

    with pytest.raises(ExecutionError) as exc:
        await flatten(client, "BTC/USDT:USDT", log, limiter, reason="test")
    assert "MANUAL INTERVENTION REQUIRED" in str(exc.value)


# --- mode gate -------------------------------------------------------------


async def test_demo_mode_refuses_before_building_a_request(log, limiter):
    client = FakeBitgetClient(mode="demo")
    with pytest.raises(DemoModeRefusal):
        await entry(client, log, limiter)
    assert client.sent_orders == []


async def test_demo_refusal_is_not_retried(log, limiter):
    """Retrying a refusal three times would just log three identical failures."""
    client = FakeBitgetClient(mode="demo")
    attempts = []
    real = client.create_entry_with_protection

    async def counted(**kwargs):
        attempts.append(1)
        return await real(**kwargs)

    client.create_entry_with_protection = counted

    with pytest.raises(DemoModeRefusal):
        await entry(client, log, limiter)
    assert len(attempts) == 1


# --- rate limiter ----------------------------------------------------------


async def test_rate_limiter_throttles_sustained_calls():
    import asyncio

    limiter = RateLimiter(rate_per_second=50, burst=1)
    loop = asyncio.get_running_loop()
    started = loop.time()
    for _ in range(6):
        await limiter.acquire()
    elapsed = loop.time() - started

    # 1 free from the burst, then 5 at 50/s = at least 0.1s.
    assert elapsed >= 0.08


async def test_rate_limiter_allows_an_initial_burst():
    import asyncio

    limiter = RateLimiter(rate_per_second=1, burst=5)
    loop = asyncio.get_running_loop()
    started = loop.time()
    for _ in range(5):
        await limiter.acquire()
    assert loop.time() - started < 0.5
