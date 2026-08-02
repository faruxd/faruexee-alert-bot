"""
Phase 2 -- Order execution primitives.

Policy lives here; transport lives in exchange.py.

THE RULE THIS MODULE EXISTS TO ENFORCE
--------------------------------------
A position must never exist on the exchange without a reduce-only stop attached
or resting. The entry carries its stop as a preset, so the venue attaches it at
fill time -- but "should have" is not "did". After any fill we re-read the
exchange and verify protection is really there. If it is not, we flatten
immediately at market. We do not retry the stop, we do not wait a loop, we do
not monitor the price ourselves.

There is no in-memory stop-loss anywhere in this file. The exchange holds it.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from cf_bot.exchange import (
    BitgetClient,
    DemoModeRefusal,
    ExchangeError,
    OrderRejected,
)
from cf_bot.ids import (
    PURPOSE_ENTRY,
    PURPOSE_FLATTEN,
    client_order_id,
)
from cf_bot.state import Position

# Retry policy. Deliberately small: every attempt carries the same deterministic
# clientOid, so a duplicate is impossible, but each attempt still costs latency
# at exactly the moment the market is moving.
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 0.5

# Rate limiting. Bitget publishes 10 requests/second/UID on most private mix
# endpoints; we run at a fraction of that. ccxt's own limiter sits underneath
# this one -- this is the deliberate safety margin on top.
MAX_REQUESTS_PER_SECOND = 4.0
RATE_LIMIT_BURST = 4.0


class ExecutionError(Exception):
    """An execution primitive could not complete safely."""


class UnprotectedPositionError(ExecutionError):
    """
    A position exists without protection and could not be flattened.

    This is the worst state the bot can reach. It is raised loudly so the caller
    escalates and halts rather than continuing to trade around it.
    """


@dataclass
class RateLimiter:
    """
    Token bucket, async. Sits above ccxt's own limiter as the safety margin.

    Not a semaphore: we care about sustained request rate, not concurrency.
    """

    rate_per_second: float = MAX_REQUESTS_PER_SECOND
    burst: float = RATE_LIMIT_BURST
    _tokens: float = field(default=0.0, init=False)
    _last_refill: float = field(default_factory=time.monotonic, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self) -> None:
        # Start full, from the CONFIGURED burst. Defaulting this to the module
        # constant would silently ignore a caller's burst size.
        self._tokens = float(self.burst)

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._last_refill = now
                self._tokens = min(self.burst, self._tokens + elapsed * self.rate_per_second)

                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return

                deficit = (1.0 - self._tokens) / self.rate_per_second
                await asyncio.sleep(deficit)


@dataclass(frozen=True)
class EntryResult:
    """Outcome of an entry attempt, sized on what ACTUALLY filled."""

    symbol: str
    side: str
    requested_amount: Decimal
    filled_amount: Decimal
    order_id: Optional[str]
    client_order_id: str
    protected: bool

    @property
    def any_fill(self) -> bool:
        return self.filled_amount > 0


async def _with_retry(operation, description: str, log, limiter: RateLimiter):
    """
    Exponential backoff, max 3 attempts.

    Safe only because every operation passed in carries a deterministic
    clientOid: a retry after a timeout either succeeds or is rejected by the
    venue as a duplicate. It can never open a second position.

    A rejection is final and is not retried -- retrying a rejected order
    unchanged just burns rate limit and delays the failure.
    """
    last_error: Optional[Exception] = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        await limiter.acquire()
        try:
            return await operation()
        except DemoModeRefusal:
            raise
        except OrderRejected as exc:
            log.error("order.rejected", operation=description, error=str(exc), attempt=attempt)
            raise
        except ExchangeError as exc:
            last_error = exc
            log.warning(
                "order.attempt_failed",
                operation=description,
                error=str(exc),
                attempt=attempt,
                max_attempts=MAX_ATTEMPTS,
            )
            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))

    raise ExecutionError(f"{description} failed after {MAX_ATTEMPTS} attempts: {last_error}")


async def _live_position(client: BitgetClient, symbol: str) -> Optional[Position]:
    """Re-read the exchange for this symbol's position. The exchange is the authority."""
    from cf_bot.reconcile import normalise_position

    raw_positions = await client.fetch_positions()
    for raw in raw_positions:
        if raw.get("symbol") != symbol:
            continue
        position = normalise_position(raw)
        if position is not None:
            return position
    return None


async def _has_protection(client: BitgetClient, symbol: str) -> bool:
    """
    Is there a reduce-only order resting for this symbol?

    Bitget attaches preset stops as plan orders rather than ordinary resting
    orders, so this checks both the open-order book and the position's own
    preset stop field. Either counts as protected.
    """
    raw_positions = await client.fetch_positions()
    for raw in raw_positions:
        if raw.get("symbol") != symbol:
            continue
        info = raw.get("info") or {}
        for key in ("presetStopLossPrice", "presetStopLossExecutePrice", "stopLossPrice"):
            value = info.get(key)
            if value not in (None, "", "0", 0):
                return True

    raw_orders = await client.fetch_open_orders()
    for raw in raw_orders:
        if raw.get("symbol") != symbol:
            continue
        if raw.get("reduceOnly"):
            return True
        info = raw.get("info") or {}
        if str(info.get("reduceOnly", "")).lower() == "yes":
            return True
        if info.get("planType") in ("loss_plan", "pos_loss", "normal_plan"):
            return True

    return False


async def flatten(
    client: BitgetClient, symbol: str, log, limiter: RateLimiter, reason: str = "unspecified"
) -> bool:
    """
    Cancel every order for `symbol`, close any position at market, verify flat.

    Returns True once the exchange confirms no position remains. Raises if it
    cannot get there -- a flatten that silently failed is indistinguishable from
    one that worked, and that difference is the whole account.
    """
    log.warning("flatten.start", symbol=symbol, reason=reason)

    await _with_retry(
        lambda: client.cancel_all_orders(symbol), f"cancel_all_orders({symbol})", log, limiter
    )

    position = await _live_position(client, symbol)
    if position is None:
        log.info("flatten.already_flat", symbol=symbol)
        return True

    oid = client_order_id(symbol, int(time.time() * 1000), position.side, PURPOSE_FLATTEN)
    await _with_retry(
        lambda: client.create_reduce_only_market(
            symbol, position.side, position.contracts, oid
        ),
        f"market close {position.describe()}",
        log,
        limiter,
    )

    # Verify. Do not trust the close response -- re-read the exchange.
    for attempt in range(MAX_ATTEMPTS):
        await asyncio.sleep(BACKOFF_BASE_SECONDS * (attempt + 1))
        await limiter.acquire()
        remaining = await _live_position(client, symbol)
        if remaining is None:
            log.warning("flatten.confirmed", symbol=symbol, reason=reason)
            return True
        log.warning(
            "flatten.still_open", symbol=symbol, remaining=remaining.describe(), attempt=attempt + 1
        )

    raise ExecutionError(
        f"flatten({symbol}) could not confirm the position was closed. "
        "MANUAL INTERVENTION REQUIRED."
    )


async def place_entry_with_protection(
    client: BitgetClient,
    symbol: str,
    side: str,
    amount: Decimal,
    price: Decimal,
    stop_price: Decimal,
    take_profit_price: Decimal,
    signal_bar_ts: int,
    log,
    limiter: RateLimiter,
) -> EntryResult:
    """
    Place a post-only limit entry with its stop and target preset onto the order.

    Then verify. If any quantity filled and protection is NOT present on the
    exchange, flatten immediately at market. All risk figures are computed from
    FILLED quantity; requested quantity is never used for anything but the
    request itself.
    """
    oid = client_order_id(symbol, signal_bar_ts, side, PURPOSE_ENTRY)

    log.info(
        "entry.submitting",
        symbol=symbol,
        side=side,
        amount=str(amount),
        price=str(price),
        stop=str(stop_price),
        target=str(take_profit_price),
        client_order_id=oid,
    )

    response = await _with_retry(
        lambda: client.create_entry_with_protection(
            symbol=symbol,
            side=side,
            amount=amount,
            price=price,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            client_order_id=oid,
        ),
        f"entry {side} {symbol}",
        log,
        limiter,
    )

    order_id = response.get("id")
    filled = Decimal(str(response.get("filled") or 0))

    # A post-only limit usually rests rather than filling instantly. Re-read the
    # exchange to find out what actually happened.
    await limiter.acquire()
    position = await _live_position(client, symbol)
    if position is not None:
        filled = position.contracts

    if filled <= 0:
        log.info("entry.resting", symbol=symbol, client_order_id=oid, order_id=order_id)
        return EntryResult(
            symbol=symbol,
            side=side,
            requested_amount=amount,
            filled_amount=Decimal(0),
            order_id=order_id,
            client_order_id=oid,
            protected=True,  # nothing to protect yet
        )

    # Something filled. It MUST be protected.
    await limiter.acquire()
    protected = await _has_protection(client, symbol)

    if not protected:
        log.error(
            "entry.unprotected",
            symbol=symbol,
            filled=str(filled),
            action="flattening immediately at market",
        )
        try:
            await flatten(client, symbol, log, limiter, reason="entry filled without protection")
        except ExecutionError as exc:
            raise UnprotectedPositionError(
                f"{symbol} filled {filled} with no stop on the exchange and could not be "
                f"flattened: {exc}. MANUAL INTERVENTION REQUIRED."
            ) from exc

        return EntryResult(
            symbol=symbol,
            side=side,
            requested_amount=amount,
            filled_amount=Decimal(0),
            order_id=order_id,
            client_order_id=oid,
            protected=False,
        )

    log.info(
        "entry.filled_and_protected",
        symbol=symbol,
        side=side,
        filled=str(filled),
        requested=str(amount),
        partial=bool(filled < amount),
        client_order_id=oid,
    )

    return EntryResult(
        symbol=symbol,
        side=side,
        requested_amount=amount,
        filled_amount=filled,
        order_id=order_id,
        client_order_id=oid,
        protected=True,
    )


async def cancel_expired_entries(
    client: BitgetClient, symbol: str, now_ms: int, expiry_ms: int, log, limiter: RateLimiter
) -> int:
    """
    Cancel any unfilled entry remainder past its 3-bar validity window.

    Returns how many orders were cancelled.
    """
    if now_ms <= expiry_ms:
        return 0

    await limiter.acquire()
    raw_orders = await client.fetch_open_orders()
    cancelled = 0

    for raw in raw_orders:
        if raw.get("symbol") != symbol:
            continue
        if raw.get("reduceOnly"):
            continue  # never cancel protection
        order_id = raw.get("id")
        if not order_id:
            continue
        await _with_retry(
            lambda oid=order_id: client.cancel_order(oid, symbol),
            f"cancel expired entry {order_id}",
            log,
            limiter,
        )
        cancelled += 1
        log.info("entry.expired_cancelled", symbol=symbol, order_id=order_id)

    return cancelled
