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
    cancel_expired_entries,
    fetch_protection_levels,
    flatten,
    place_entry_limit_then_market,
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


async def test_unverifiable_protection_keeps_the_position(log, limiter):
    """
    CHANGED after two live losses. Failing to READ a stop is not the same as
    there being no stop -- both times it was our own query that was wrong.

    Flattening on a failed read is a guaranteed loss every time it misfires:
    open and close at market, taker both ways, about -0.5R a cycle, repeating.
    The entry was accepted carrying preset stop and target, so the submission
    did not fail and the spec's "flatten if the stop submission fails" does not
    apply. Shout, emit diagnostics, keep the position.
    """
    client = FakeBitgetClient(positions=[position_payload(preset_stop=None)])
    result = await entry(client, log, limiter)

    assert result.protected is False
    assert result.filled_amount == Decimal("0.01"), "size must reflect what filled"
    assert not any(o["kind"] == "close" for o in client.sent_orders),         "flattened on a failed READ -- this is the bug that cost two positions"


async def test_late_registering_protection_is_picked_up_on_the_recheck(log, limiter):
    """A preset may not appear as a plan order the instant the fill lands."""
    client = FakeBitgetClient(positions=[position_payload(preset_stop=None)])
    looks = {"n": 0}

    async def appears_late(symbol):
        looks["n"] += 1
        return [trigger_order()] if looks["n"] > 1 else []

    client.fetch_trigger_orders = appears_late
    result = await entry(client, log, limiter)

    assert result.protected is True
    assert not any(o["kind"] == "close" for o in client.sent_orders)


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


# --- limit-then-market entry (scalper) --------------------------------------


async def scalper_entry(client, log, limiter, timeout=0.0, amount="0.01"):
    return await place_entry_limit_then_market(
        client=client,
        symbol="BTC/USDT:USDT",
        side="long",
        amount=Decimal(amount),
        price=Decimal("64000"),
        stop_price=Decimal("63000"),
        take_profit_price=Decimal("66000"),
        signal_bar_ts=1717000000000,
        timeout_seconds=timeout,
        log=log,
        limiter=limiter,
    )


async def test_a_limit_that_fills_never_pays_taker(log, limiter):
    """
    The whole point of the two-leg entry: when the market comes to us we stop
    there and pay maker.
    """
    client = FakeBitgetClient(positions=[position_payload()])
    result = await scalper_entry(client, log, limiter)

    assert result.any_fill is True
    assert result.protected is True
    kinds = [o["kind"] for o in client.sent_orders]
    assert kinds == ["entry"], f"expected only a limit leg, got {kinds}"


async def test_an_unfilled_limit_is_cancelled_then_marketed(log, limiter):
    client = FakeBitgetClient(positions=[])  # nothing fills passively

    async def fill_on_market(**kwargs):
        client.sent_orders.append({"kind": "market_entry", **kwargs})
        client.positions = [position_payload()]
        return {"id": "market-1"}

    client.create_market_entry_with_protection = fill_on_market
    result = await scalper_entry(client, log, limiter)

    kinds = [o["kind"] for o in client.sent_orders]
    assert kinds == ["entry", "market_entry"]
    assert "BTC/USDT:USDT" in client.cancel_all_calls, "stale limit was not cancelled"
    assert result.any_fill is True


async def test_the_two_legs_use_different_client_order_ids(log, limiter):
    """
    They must differ, or the venue rejects the fallback as a duplicate of the
    order we just cancelled -- and the entry would silently never happen.
    """
    client = FakeBitgetClient(positions=[])

    async def fill_on_market(**kwargs):
        client.sent_orders.append({"kind": "market_entry", **kwargs})
        client.positions = [position_payload()]
        return {"id": "market-1"}

    client.create_market_entry_with_protection = fill_on_market
    await scalper_entry(client, log, limiter)

    limit_oid = client.sent_orders[0]["client_order_id"]
    market_oid = client.sent_orders[1]["client_order_id"]
    assert limit_oid != market_oid


async def test_both_legs_are_still_deterministic(log, limiter):
    """Different from each other, but each stable across runs."""
    ids = []
    for _ in range(2):
        client = FakeBitgetClient(positions=[])

        async def fill_on_market(**kwargs):
            client.sent_orders.append({"kind": "market_entry", **kwargs})
            client.positions = [position_payload()]
            return {"id": "m"}

        client.create_market_entry_with_protection = fill_on_market
        await scalper_entry(client, log, limiter)
        ids.append([o["client_order_id"] for o in client.sent_orders])

    assert ids[0] == ids[1]


async def test_a_post_only_rejection_goes_straight_to_market(log, limiter):
    """
    Post-only rejects when price has already moved through our level. That is
    the venue saying a passive fill is impossible, not an error.
    """
    client = FakeBitgetClient(positions=[])

    async def reject_post_only(**kwargs):
        raise OrderRejected("post only would take liquidity")

    async def fill_on_market(**kwargs):
        client.sent_orders.append({"kind": "market_entry", **kwargs})
        client.positions = [position_payload()]
        return {"id": "market-1"}

    client.create_entry_with_protection = reject_post_only
    client.create_market_entry_with_protection = fill_on_market

    result = await scalper_entry(client, log, limiter)
    assert [o["kind"] for o in client.sent_orders] == ["market_entry"]
    assert result.any_fill is True


async def test_the_market_leg_still_carries_stop_and_target(log, limiter):
    """No entry path in this codebase may leave a position bare."""
    client = FakeBitgetClient(positions=[])

    async def fill_on_market(**kwargs):
        client.sent_orders.append({"kind": "market_entry", **kwargs})
        client.positions = [position_payload()]
        return {"id": "market-1"}

    client.create_market_entry_with_protection = fill_on_market
    await scalper_entry(client, log, limiter)

    market_leg = client.sent_orders[-1]
    assert market_leg["stop_price"] == Decimal("63000")
    assert market_leg["take_profit_price"] == Decimal("66000")


async def test_a_market_fill_with_unverifiable_protection_is_kept(log, limiter):
    client = FakeBitgetClient(positions=[])

    async def fill_unprotected(**kwargs):
        client.sent_orders.append({"kind": "market_entry", **kwargs})
        client.positions = [position_payload(preset_stop=None)]
        return {"id": "market-1"}

    client.create_market_entry_with_protection = fill_unprotected
    result = await scalper_entry(client, log, limiter)

    assert result.protected is False
    assert not any(o["kind"] == "close" for o in client.sent_orders)


async def test_scalper_entry_is_refused_in_demo(log, limiter):
    client = FakeBitgetClient(mode="demo")
    with pytest.raises(DemoModeRefusal):
        await scalper_entry(client, log, limiter)
    assert client.sent_orders == []


# --- entry expiry ----------------------------------------------------------
#
# REGRESSION: the first version compared now_ms against a deadline the caller
# passed as now_ms, so the condition was always false and NOTHING was ever
# cancelled. Unfilled entries would have rested on the exchange forever. Orders
# are now aged by their own exchange-reported timestamp.

NOW = 1_717_000_000_000
THREE_BARS_MS = 3 * 5 * 60 * 1000


def resting_entry(order_id="e-1", age_minutes=0.0, reduce_only=False):
    return {
        "id": order_id,
        "symbol": "BTC/USDT:USDT",
        "side": "buy",
        "type": "limit",
        "price": 64000.0,
        "amount": 0.01,
        "reduceOnly": reduce_only,
        "status": "open",
        "timestamp": NOW - int(age_minutes * 60_000),
    }


async def test_an_entry_older_than_its_window_is_cancelled(log, limiter):
    client = FakeBitgetClient(open_orders=[resting_entry(age_minutes=20)])
    cancelled = await cancel_expired_entries(
        client, "BTC/USDT:USDT", NOW, THREE_BARS_MS, log, limiter
    )
    assert cancelled == 1
    assert client.cancelled == ["e-1"]


async def test_a_fresh_entry_is_left_alone(log, limiter):
    client = FakeBitgetClient(open_orders=[resting_entry(age_minutes=5)])
    assert (
        await cancel_expired_entries(client, "BTC/USDT:USDT", NOW, THREE_BARS_MS, log, limiter)
        == 0
    )
    assert client.cancelled == []


async def test_protection_is_never_cancelled_however_old(log, limiter):
    """A reduce-only order is the stop. Ageing it out would strip the position bare."""
    client = FakeBitgetClient(
        open_orders=[resting_entry(order_id="stop-1", age_minutes=600, reduce_only=True)]
    )
    assert (
        await cancel_expired_entries(client, "BTC/USDT:USDT", NOW, THREE_BARS_MS, log, limiter)
        == 0
    )
    assert client.cancelled == []


async def test_an_order_with_no_timestamp_is_left_alone(log, limiter):
    """Cancelling on a guess could kill an order placed seconds ago."""
    stale = resting_entry(age_minutes=99)
    del stale["timestamp"]
    client = FakeBitgetClient(open_orders=[stale])
    assert (
        await cancel_expired_entries(client, "BTC/USDT:USDT", NOW, THREE_BARS_MS, log, limiter)
        == 0
    )


async def test_other_symbols_are_untouched(log, limiter):
    other = resting_entry(order_id="eth-1", age_minutes=20)
    other["symbol"] = "ETH/USDT:USDT"
    client = FakeBitgetClient(open_orders=[other])
    assert (
        await cancel_expired_entries(client, "BTC/USDT:USDT", NOW, THREE_BARS_MS, log, limiter)
        == 0
    )


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


# --- protection detection --------------------------------------------------
#
# PRODUCTION INCIDENT 2026-08-04. A market entry on LTC filled and Bitget
# attached the preset stop and take-profit correctly. _has_protection() checked
# only fetch_open_orders() and reported UNPROTECTED, because a preset stop is a
# PLAN order on a different endpoint and never appears there. The bot tried to
# flatten a perfectly healthy position; the flatten then failed on an unrelated
# bug, which is the only reason the position survived.


def trigger_order(symbol="BTC/USDT:USDT"):
    """A preset stop as Bitget actually reports it, via the plan endpoint."""
    return {
        "id": "plan-1",
        "symbol": symbol,
        "side": "sell",
        "type": "market",
        "triggerPrice": 62000.0,
        "reduceOnly": True,
        "info": {"planType": "profit_loss", "stopLossTriggerPrice": "62000"},
    }


async def test_a_preset_stop_visible_only_as_a_trigger_order_counts_as_protected(
    log, limiter
):
    """The exact production false negative. Must not regress."""
    client = FakeBitgetClient(positions=[position_payload(preset_stop=None)])
    client.open_orders = []          # presets never appear here
    client.trigger_orders = [trigger_order()]

    result = await entry(client, log, limiter)

    assert result.protected is True
    assert not any(o["kind"] == "close" for o in client.sent_orders), \
        "flattened a position that WAS protected"


async def test_a_bare_position_is_reported_but_not_destroyed(log, limiter):
    """Reported as unprotected so the operator acts -- but not auto-closed."""
    client = FakeBitgetClient(positions=[position_payload(preset_stop=None)])
    client.open_orders = []
    client.trigger_orders = []

    result = await entry(client, log, limiter)
    assert result.protected is False
    assert not any(o["kind"] == "close" for o in client.sent_orders)


# --- cancel_all_orders on an empty book ------------------------------------


async def test_flatten_still_closes_when_there_is_nothing_to_cancel(log, limiter):
    """
    "No order to cancel" is the state we asked for, not a failure.

    Production: a market entry left nothing resting, Bitget returned 22001, the
    retry wrapper burned three attempts and raised, and the market close never
    ran. The bot halted holding the position.
    """
    client = FakeBitgetClient(positions=[position_payload()])

    async def nothing_to_cancel(symbol):
        raise ExchangeError(
            'cancel_all_orders failed: bitget {"code":"22001","msg":"No order to cancel"}'
        )

    client.cancel_all_orders = nothing_to_cancel

    assert await flatten(client, "BTC/USDT:USDT", log, limiter, reason="test") is True
    assert any(o["kind"] == "close" for o in client.sent_orders), \
        "cancel failure blocked the close"


async def test_cancel_failure_never_blocks_the_close(log, limiter):
    """Cancelling is housekeeping. Closing is the safety-critical action."""
    client = FakeBitgetClient(positions=[position_payload()])

    async def boom(symbol):
        raise ExchangeError("venue exploded")

    client.cancel_all_orders = boom

    assert await flatten(client, "BTC/USDT:USDT", log, limiter, reason="test") is True
    assert any(o["kind"] == "close" for o in client.sent_orders)


# --- protection levels for the alert ---------------------------------------


def _trigger(price, symbol="BTC/USDT:USDT"):
    return {"id": f"p{price}", "symbol": symbol, "triggerPrice": float(price),
            "reduceOnly": True, "info": {}}


async def test_levels_for_a_long_are_classified_by_geometry(log, limiter):
    """
    Long: the trigger BELOW entry is the stop, the one ABOVE is the target.
    Geometry cannot be invalidated by Bitget renaming a field.
    """
    client = FakeBitgetClient()
    client.trigger_orders = [_trigger(66000), _trigger(62000)]

    stop, target = await fetch_protection_levels(
        client, "BTC/USDT:USDT", "long", Decimal("64000")
    )
    assert stop == Decimal("62000")
    assert target == Decimal("66000")


async def test_levels_for_a_short_are_mirrored(log, limiter):
    client = FakeBitgetClient()
    client.trigger_orders = [_trigger(66000), _trigger(62000)]

    stop, target = await fetch_protection_levels(
        client, "BTC/USDT:USDT", "short", Decimal("64000")
    )
    assert stop == Decimal("66000")
    assert target == Decimal("62000")


async def test_a_stop_with_no_target_still_resolves(log, limiter):
    client = FakeBitgetClient()
    client.trigger_orders = [_trigger(62000)]

    stop, target = await fetch_protection_levels(
        client, "BTC/USDT:USDT", "long", Decimal("64000")
    )
    assert stop == Decimal("62000")
    assert target is None


async def test_level_lookup_failure_never_raises(log, limiter, exchange_error):
    """This feeds a notification. It must not disturb trading."""
    client = FakeBitgetClient()
    client.trigger_orders = exchange_error

    assert await fetch_protection_levels(
        client, "BTC/USDT:USDT", "long", Decimal("64000")
    ) == (None, None)


async def test_no_entry_price_means_no_levels(log, limiter):
    client = FakeBitgetClient()
    client.trigger_orders = [_trigger(62000)]
    assert await fetch_protection_levels(client, "BTC/USDT:USDT", "long", None) == (None, None)
