"""
Phase 2 -- Order execution primitives.

Policy lives here; transport lives in exchange.py.

THE RULE THIS MODULE EXISTS TO ENFORCE
--------------------------------------
A position must never exist on the exchange without a reduce-only stop attached
or resting. Every entry carries its stop as a PRESET on the order itself, so the
venue attaches it at fill time and there is no window where the position exists
bare. That submission succeeding is the guarantee.

After a fill we also read the protection back -- but a failed READ is treated as
a warning, not as proof of a bare position. That distinction was learned
expensively: two separate bugs in our own query reported healthy positions as
unprotected, and the response then closed them at market, taker both ways, about
-0.5R per misfire. Flattening on a failed read is a certain loss whenever the
reader is wrong; keeping a position whose preset the venue accepted is a risk
only if the venue silently ignored it, which it has not been observed to do.

So: shout, dump what the venue actually returned, and keep the position. The
time stop and settlement flatten still bound the exposure.

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
    InsufficientBalance,
    OrderRejected,
)
from cf_bot.ids import (
    PURPOSE_ENTRY,
    PURPOSE_ENTRY_MARKET,
    PURPOSE_FLATTEN,
    client_order_id,
)
from cf_bot.state import Position

# Retry policy. Deliberately small: every attempt carries the same deterministic
# clientOid, so a duplicate is impossible, but each attempt still costs latency
# at exactly the moment the market is moving.
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 0.5

# A preset stop does not always appear as a plan order the instant the fill
# lands. Wait this long and look again before declaring anything unverified.
PROTECTION_RECHECK_SECONDS = 3.0

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
        except InsufficientBalance:
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


def _has_stop_field(info: dict) -> bool:
    """Any populated stop-loss field in a Bitget payload."""
    for key in (
        "presetStopLossPrice",
        "presetStopLossExecutePrice",
        "stopLossPrice",
        "stopLossTriggerPrice",
        "slTriggerPrice",
        "triggerPrice",
    ):
        value = info.get(key)
        if value not in (None, "", "0", 0, "0.0"):
            return True
    return False


async def fetch_protection_levels(
    client: BitgetClient, symbol: str, side: str, entry_price: Optional[Decimal]
) -> tuple[Optional[Decimal], Optional[Decimal]]:
    """
    Return (stop_price, target_price) for an open position, or (None, None).

    Read from the venue's TRIGGER orders, which is the source proven to actually
    contain them -- the position payload's preset fields are not dependable, and
    trusting them is what broke the protection check twice.

    Which trigger is the stop and which is the target is decided by GEOMETRY,
    not by field names:

        long  -> stop is BELOW entry, target is ABOVE
        short -> stop is ABOVE entry, target is BELOW

    That holds by construction for every signal this bot produces, and it cannot
    be invalidated by Bitget renaming a field.

    Never raises. This feeds a notification; a failure here must not disturb
    trading.
    """
    if entry_price is None or entry_price <= 0:
        return None, None

    try:
        triggers = await client.fetch_trigger_orders(symbol)
    except Exception:
        return None, None

    below: list[Decimal] = []
    above: list[Decimal] = []
    for raw in triggers:
        price = raw.get("triggerPrice") or raw.get("stopPrice")
        if price is None:
            info = raw.get("info") or {}
            price = (
                info.get("triggerPrice")
                or info.get("stopLossTriggerPrice")
                or info.get("stopSurplusTriggerPrice")
            )
        if price in (None, "", 0, "0"):
            continue
        try:
            value = Decimal(str(price))
        except Exception:
            continue
        if value <= 0:
            continue
        (below if value < entry_price else above).append(value)

    if side == "long":
        # Nearest below is the stop; nearest above is the target.
        stop = max(below) if below else None
        target = min(above) if above else None
    else:
        stop = min(above) if above else None
        target = max(below) if below else None

    return stop, target


async def _protection_diagnostics(client: BitgetClient, symbol: str) -> dict:
    """
    Exactly what the venue returned when we failed to find a stop.

    Exists because two protection bugs in one day were both diagnosed by
    guessing at Bitget's response shape and both guesses were wrong. The next
    occurrence should hand over evidence instead.
    """
    payload: dict = {}
    try:
        triggers = await client.fetch_trigger_orders(symbol)
        payload["trigger_order_count"] = len(triggers)
        payload["trigger_orders"] = [
            {
                "planType": t.get("_planType"),
                "id": t.get("id"),
                "reduceOnly": t.get("reduceOnly"),
                "triggerPrice": t.get("triggerPrice"),
                "info_keys": sorted((t.get("info") or {}).keys()),
            }
            for t in triggers[:5]
        ]
    except Exception as exc:
        payload["trigger_orders_error"] = str(exc)[:200]

    try:
        for raw in await client.fetch_positions():
            if raw.get("symbol") == symbol:
                info = raw.get("info") or {}
                payload["position_info_keys"] = sorted(info.keys())
                payload["position_stop_fields"] = {
                    k: v for k, v in info.items() if "stop" in k.lower() or "tp" in k.lower()
                    or "sl" in k.lower() or "preset" in k.lower()
                }
    except Exception as exc:
        payload["position_error"] = str(exc)[:200]

    return payload


async def _has_protection(client: BitgetClient, symbol: str) -> bool:
    """
    Is this symbol's position covered by a stop on the exchange?

    Checked in three places, because Bitget reports a stop differently
    depending on how it was created:

      1. TRIGGER (plan) orders -- where a preset stop actually lives. This is
         the one that matters and the one the first version missed entirely:
         preset stops do NOT appear in fetch_open_orders, so checking only
         that endpoint was a guaranteed false negative. In production it made
         the bot try to flatten a correctly protected position.
      2. The position payload's own preset fields.
      3. Ordinary reduce-only resting orders, for a manually placed stop.

    Any one of them counts as protected.
    """
    trigger_orders = await client.fetch_trigger_orders(symbol)
    for raw in trigger_orders:
        if raw.get("symbol") not in (symbol, None):
            continue
        if raw.get("reduceOnly") or raw.get("triggerPrice") or raw.get("stopLossPrice"):
            return True
        if _has_stop_field(raw.get("info") or {}):
            return True

    raw_positions = await client.fetch_positions()
    for raw in raw_positions:
        if raw.get("symbol") != symbol:
            continue
        if _has_stop_field(raw.get("info") or {}):
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
        if info.get("planType") in ("loss_plan", "pos_loss", "normal_plan", "profit_loss"):
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

    # Cancelling is BEST EFFORT. Closing the position is the safety-critical
    # part, and it must not be blocked by housekeeping.
    #
    # Production incident: Bitget returned "no order to cancel" (22001) because
    # a market entry had left nothing resting. The retry wrapper treated that as
    # a transport failure, exhausted its attempts and raised -- so the market
    # close below never ran and the bot halted still holding the position.
    # Whatever happens here, we go on to close.
    try:
        await _with_retry(
            lambda: client.cancel_all_orders(symbol),
            f"cancel_all_orders({symbol})",
            log,
            limiter,
        )
    except (ExecutionError, ExchangeError) as exc:
        log.warning(
            "flatten.cancel_failed",
            symbol=symbol,
            error=str(exc),
            note="proceeding to close the position anyway",
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

    # Something filled. It MUST be protected. ONE shared code path with the
    # limit-then-market entry, so the two can never drift again -- they already
    # did once, and the stale copy kept flattening live positions after the
    # shared version was fixed.
    return await _verify_or_flatten(
        client, symbol, side, filled, amount, oid, log, limiter, stop_price
    )


async def place_entry_limit_then_market(
    client: BitgetClient,
    symbol: str,
    side: str,
    amount: Decimal,
    price: Decimal,
    stop_price: Decimal,
    take_profit_price: Decimal,
    signal_bar_ts: int,
    timeout_seconds: float,
    log,
    limiter: RateLimiter,
) -> EntryResult:
    """
    Rest a post-only limit; if it has not filled within `timeout_seconds`,
    cancel it and take the fill at market.

    The point is fee control. A market entry pays 0.060% taker; a passive fill
    pays 0.020% maker. On a scalper at 1% risk those are 0.40R and 0.13R of
    round-trip drag respectively, so paying taker only when the market refuses
    to come to us is worth the added complexity.

    Both legs carry the stop and target as presets, so neither can leave a
    position unprotected. The legs use DIFFERENT deterministic ids, because the
    venue would reject the fallback as a duplicate otherwise.
    """
    limit_oid = client_order_id(symbol, signal_bar_ts, side, PURPOSE_ENTRY)

    log.info(
        "entry.submitting_limit",
        symbol=symbol,
        side=side,
        amount=str(amount),
        price=str(price),
        stop=str(stop_price),
        target=str(take_profit_price),
        timeout_seconds=timeout_seconds,
        client_order_id=limit_oid,
    )

    limit_placed = True
    try:
        await _with_retry(
            lambda: client.create_entry_with_protection(
                symbol=symbol,
                side=side,
                amount=amount,
                price=price,
                stop_price=stop_price,
                take_profit_price=take_profit_price,
                client_order_id=limit_oid,
            ),
            f"limit entry {side} {symbol}",
            log,
            limiter,
        )
    except InsufficientBalance:
        # NOT a post-only problem. The order is unaffordable in any form, so
        # falling back to market would just collect a second rejection.
        raise
    except OrderRejected as exc:
        # Post-only rejects when the price has already moved through our level.
        # That is not a failure -- it is the venue telling us a passive fill is
        # no longer possible, so go straight to the fallback.
        log.info("entry.post_only_rejected", symbol=symbol, error=str(exc))
        limit_placed = False

    if limit_placed:
        await asyncio.sleep(timeout_seconds)

        await limiter.acquire()
        position = await _live_position(client, symbol)
        if position is not None and position.contracts > 0:
            return await _verify_or_flatten(
                client, symbol, side, position.contracts, amount, limit_oid, log,
                limiter, stop_price
            )

        log.info("entry.limit_unfilled", symbol=symbol, action="cancelling for market fallback")
        await _with_retry(
            lambda: client.cancel_all_orders(symbol),
            f"cancel unfilled limit {symbol}",
            log,
            limiter,
        )

    market_oid = client_order_id(symbol, signal_bar_ts, side, PURPOSE_ENTRY_MARKET)
    log.warning(
        "entry.market_fallback",
        symbol=symbol,
        side=side,
        amount=str(amount),
        client_order_id=market_oid,
        note="paying taker fee because the passive limit did not fill",
    )

    await _with_retry(
        lambda: client.create_market_entry_with_protection(
            symbol=symbol,
            side=side,
            amount=amount,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            client_order_id=market_oid,
        ),
        f"market entry {side} {symbol}",
        log,
        limiter,
    )

    await limiter.acquire()
    position = await _live_position(client, symbol)
    filled = position.contracts if position is not None else Decimal(0)

    if filled <= 0:
        log.error("entry.market_did_not_fill", symbol=symbol, client_order_id=market_oid)
        return EntryResult(
            symbol=symbol,
            side=side,
            requested_amount=amount,
            filled_amount=Decimal(0),
            order_id=None,
            client_order_id=market_oid,
            protected=True,
        )

    return await _verify_or_flatten(
        client, symbol, side, filled, amount, market_oid, log, limiter, stop_price
    )


async def _verify_or_flatten(
    client: BitgetClient,
    symbol: str,
    side: str,
    filled: Decimal,
    requested: Decimal,
    oid: str,
    log,
    limiter: RateLimiter,
    stop_price: Optional[Decimal] = None,
) -> EntryResult:
    """
    Confirm a filled position is protected, or close it at market.

    Shared by both entry paths. This is the enforcement point for the rule that
    a position never exists on the exchange without a stop.
    """
    await limiter.acquire()
    protected = await _has_protection(client, symbol)

    if not protected:
        # A preset does not necessarily register as a plan order the instant the
        # fill lands. Look once more before concluding anything.
        await asyncio.sleep(PROTECTION_RECHECK_SECONDS)
        await limiter.acquire()
        protected = await _has_protection(client, symbol)
        if protected:
            log.info("entry.protection_confirmed_late", symbol=symbol)

    if not protected:
        # WE COULD NOT SEE A STOP. That is NOT the same as there being no stop,
        # and the difference cost two live positions.
        #
        # The rule is "if the stop SUBMISSION fails, flatten". The submission did
        # not fail here: the venue accepted an entry carrying presetStopLossPrice
        # and presetStopSurplusPrice without error. What failed is our ability to
        # read the resulting protection back, and twice that was our own query
        # being wrong rather than the exchange being bare.
        #
        # Flattening on a failed READ is a guaranteed loss every single time it
        # misfires -- open and close at market, taker both ways, roughly -0.5R a
        # cycle, repeating. Keeping a position whose stop we merely cannot see is
        # a risk only if the venue silently ignored the presets, which it has
        # been observed not to do.
        #
        # So: shout, dump exactly what the venue returned so the next occurrence
        # produces evidence instead of another blind loss, and DO NOT flatten.
        diagnostics = await _protection_diagnostics(client, symbol)
        log.error(
            "entry.protection_unverified",
            symbol=symbol,
            filled=str(filled),
            action="KEEPING the position; entry was accepted with preset stop and target",
            note=(
                "Could not read protection back from the venue. Check this symbol in "
                "the Bitget UI now and confirm SL/TP are present."
            ),
            **diagnostics,
        )
        return EntryResult(
            symbol=symbol,
            side=side,
            requested_amount=requested,
            filled_amount=filled,
            order_id=None,
            client_order_id=oid,
            protected=False,
        )

    log.info(
        "entry.filled_and_protected",
        symbol=symbol,
        side=side,
        filled=str(filled),
        requested=str(requested),
        partial=bool(filled < requested),
        client_order_id=oid,
    )
    return EntryResult(
        symbol=symbol,
        side=side,
        requested_amount=requested,
        filled_amount=filled,
        order_id=None,
        client_order_id=oid,
        protected=True,
    )


async def cancel_expired_entries(
    client: BitgetClient,
    symbol: str,
    now_ms: int,
    max_age_ms: int,
    log,
    limiter: RateLimiter,
) -> int:
    """
    Cancel unfilled entry orders older than `max_age_ms`.

    Each order is aged by its OWN exchange-reported timestamp rather than
    against a deadline we remember, so this keeps working across a restart --
    the process has no memory of when it placed anything.

    An order whose timestamp the venue did not report is left alone: cancelling
    on a guess could kill an order placed seconds ago.

    Returns how many orders were cancelled.
    """
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

        placed_at = raw.get("timestamp")
        if not placed_at:
            log.warning("entry.age_unknown", symbol=symbol, order_id=order_id)
            continue

        age_ms = now_ms - int(placed_at)
        if age_ms <= max_age_ms:
            continue

        await _with_retry(
            lambda oid=order_id: client.cancel_order(oid, symbol),
            f"cancel expired entry {order_id}",
            log,
            limiter,
        )
        cancelled += 1
        log.info(
            "entry.expired_cancelled",
            symbol=symbol,
            order_id=order_id,
            age_seconds=age_ms // 1000,
        )

    return cancelled
