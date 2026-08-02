"""
Reconciliation: exchange responses -> one immutable AccountState.

The rule this module enforces is all-or-nothing. If any single leg of the
snapshot fails, the whole reconcile raises and the caller gets nothing. A
partially populated snapshot is the dangerous case: downstream code cannot tell
"there are no positions" apart from "the positions call errored", and those two
must never be confused.

The normalisation functions are pure and take plain dicts, so the entire mapping
layer is unit-testable against handcrafted ccxt payloads with no network.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from cf_bot.exchange import BitgetClient, ExchangeError
from cf_bot.state import (
    AccountState,
    ClosedPosition,
    Fill,
    OpenOrder,
    Position,
    to_decimal,
)


class ReconcileError(Exception):
    """The snapshot could not be built. Always fatal to the current loop iteration."""


def normalise_position(raw: dict[str, Any]) -> Optional[Position]:
    """
    Map one ccxt position dict to our frozen Position.

    Returns None for zero-size entries. ccxt routinely reports a placeholder row
    for a symbol you merely have leverage configured on; a zero-size row is not
    a position and counting it as one would make is_flat lie.
    """
    contracts = to_decimal(raw.get("contracts"))
    if contracts is None or contracts == 0:
        return None

    side = (raw.get("side") or "").strip().lower()
    if side not in ("long", "short"):
        raise ReconcileError(
            f"position for {raw.get('symbol')!r} had unrecognised side {raw.get('side')!r}; "
            "refusing to guess direction"
        )

    symbol = raw.get("symbol")
    if not symbol:
        raise ReconcileError("position payload had no symbol")

    # When the position was opened, per the EXCHANGE. The time stop is measured
    # from this rather than from anything we remember, so a restart mid-position
    # does not reset the clock and leave a trade running forever.
    info = raw.get("info") or {}
    opened_at = raw.get("timestamp") or info.get("cTime") or info.get("ctime")

    return Position(
        symbol=str(symbol),
        side=side,
        contracts=abs(contracts),
        entry_price=to_decimal(raw.get("entryPrice")),
        mark_price=to_decimal(raw.get("markPrice")),
        liquidation_price=to_decimal(raw.get("liquidationPrice")),
        unrealized_pnl=to_decimal(raw.get("unrealizedPnl")),
        margin_mode=(str(raw["marginMode"]) if raw.get("marginMode") else None),
        leverage=to_decimal(raw.get("leverage")),
        opened_at_ms=(int(opened_at) if opened_at else None),
    )


def normalise_order(raw: dict[str, Any]) -> OpenOrder:
    """Map one ccxt order dict to our frozen OpenOrder."""
    order_id = raw.get("id")
    if not order_id:
        raise ReconcileError(f"open order payload had no id: keys={sorted(raw.keys())}")

    symbol = raw.get("symbol")
    if not symbol:
        raise ReconcileError(f"open order {order_id} had no symbol")

    # ccxt sets reduceOnly to None when the venue did not report it. For an
    # order we did not place, absent means "we cannot prove it is reduce-only",
    # which we record as False rather than optimistically True.
    reduce_only = bool(raw.get("reduceOnly") or False)

    return OpenOrder(
        order_id=str(order_id),
        client_order_id=(str(raw["clientOrderId"]) if raw.get("clientOrderId") else None),
        symbol=str(symbol),
        side=str(raw.get("side") or "").strip().lower(),
        order_type=(str(raw["type"]) if raw.get("type") else None),
        price=to_decimal(raw.get("price")),
        amount=to_decimal(raw.get("amount")),
        filled=to_decimal(raw.get("filled")),
        remaining=to_decimal(raw.get("remaining")),
        reduce_only=reduce_only,
        status=(str(raw["status"]) if raw.get("status") else None),
        timestamp_ms=(int(raw["timestamp"]) if raw.get("timestamp") else None),
    )


def _fill_client_order_id(raw: dict[str, Any]) -> Optional[str]:
    """
    Extract the client order id from a ccxt trade.

    ccxt does not surface clientOrderId on trades for every venue, so we fall
    back to Bitget's raw `info.clientOid`. This id is what the daily entry
    counter keys on, so losing it would silently under-count entries.
    """
    direct = raw.get("clientOrderId")
    if direct:
        return str(direct)
    info = raw.get("info") or {}
    for key in ("clientOid", "clientOrderId", "cOid"):
        value = info.get(key)
        if value:
            return str(value)
    return None


def normalise_fill(raw: dict[str, Any]) -> Fill:
    """Map one ccxt trade dict to our frozen Fill."""
    trade_id = raw.get("id")
    if not trade_id:
        raise ReconcileError(f"fill payload had no id: keys={sorted(raw.keys())}")

    fee = raw.get("fee") or {}

    return Fill(
        trade_id=str(trade_id),
        order_id=(str(raw["order"]) if raw.get("order") else None),
        client_order_id=_fill_client_order_id(raw),
        symbol=str(raw.get("symbol") or ""),
        side=str(raw.get("side") or "").strip().lower(),
        price=to_decimal(raw.get("price")),
        amount=to_decimal(raw.get("amount")),
        cost=to_decimal(raw.get("cost")),
        fee_cost=to_decimal(fee.get("cost")),
        fee_currency=(str(fee["currency"]) if fee.get("currency") else None),
        timestamp_ms=(int(raw["timestamp"]) if raw.get("timestamp") else None),
    )


def normalise_closed_position(raw: dict[str, Any]) -> ClosedPosition:
    """
    Map one ccxt position-history entry to our frozen ClosedPosition.

    Realised PnL drives the daily loss limit, so an unparseable value must not
    quietly become zero -- that would read as a break-even day and re-enable
    trading after a loss.
    """
    info = raw.get("info") or {}

    pnl = raw.get("realizedPnl")
    if pnl is None:
        for key in ("netProfit", "pnl", "achievedProfits"):
            if info.get(key) is not None:
                pnl = info[key]
                break

    realised = to_decimal(pnl)
    if realised is None:
        raise ReconcileError(
            f"closed position for {raw.get('symbol')!r} reported no parseable realised "
            f"PnL; refusing to treat it as break-even. info keys={sorted(info.keys())}"
        )

    closed_at = raw.get("lastUpdateTimestamp") or raw.get("timestamp")
    if closed_at is None and info.get("utime"):
        closed_at = info["utime"]

    return ClosedPosition(
        symbol=str(raw.get("symbol") or ""),
        side=(str(raw["side"]) if raw.get("side") else None),
        realised_pnl=realised,
        closed_at_ms=(int(closed_at) if closed_at else None),
    )


def extract_equity(balance: dict[str, Any], margin_coin: str) -> tuple[Optional[object], Optional[object]]:
    """
    Pull (total, free) for the margin coin out of a ccxt balance dict.

    Returns (None, None) rather than raising if the coin is absent -- a brand-new
    account with a zero balance is a legitimate Phase 1 state, and Phase 1 never
    sizes anything, so a missing balance is informational here. Phase 4 must
    treat a missing equity figure as fatal before it sizes an order.
    """
    entry = balance.get(margin_coin)
    if not isinstance(entry, dict):
        return None, None
    return to_decimal(entry.get("total")), to_decimal(entry.get("free"))


async def reconcile(client: BitgetClient, mode: str) -> AccountState:
    """
    Build a complete AccountState from live exchange responses.

    The exchange is the authority. Nothing in this function consults a local
    cache, a previous snapshot, or anything persisted on disk.
    """
    try:
        position_mode = await client.fetch_position_mode()
        balance = await client.fetch_balance()
        raw_positions = await client.fetch_positions()
        raw_orders = await client.fetch_open_orders()
        raw_fills = await client.fetch_todays_fills()
        raw_closed = await client.fetch_todays_closed_positions()
    except ExchangeError as exc:
        raise ReconcileError(f"could not build account snapshot: {exc}") from exc

    equity, available = extract_equity(balance, client.margin_coin)

    positions = tuple(
        p for p in (normalise_position(raw) for raw in raw_positions) if p is not None
    )
    open_orders = tuple(normalise_order(raw) for raw in raw_orders)
    todays_fills = tuple(normalise_fill(raw) for raw in raw_fills)
    todays_closed = tuple(normalise_closed_position(raw) for raw in raw_closed)

    return AccountState(
        fetched_at_ms=int(time.time() * 1000),
        mode=mode,
        position_mode=position_mode,
        equity=equity,
        available=available,
        positions=positions,
        open_orders=open_orders,
        todays_fills=todays_fills,
        todays_closed_positions=todays_closed,
    )
