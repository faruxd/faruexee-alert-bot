"""
Immutable snapshot types.

This module is PURE. It performs no I/O, imports no exchange library, and holds
no mutable state. Every type here is a frozen dataclass built from tuples, which
gives us structural equality for free -- that is what makes the Phase 1
acceptance test ("kill it, restart it, it reports identical state") a one-line
assertion.

All monetary and quantity values are Decimal. ccxt hands back floats; we convert
at the boundary in reconcile.py via str() so we never inherit binary-float
rounding into anything that will later size an order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional


def to_decimal(value: object) -> Optional[Decimal]:
    """
    Convert an exchange-supplied number to Decimal without going through binary
    float. Returns None for None/empty, so callers must handle absent fields
    explicitly rather than silently treating a missing price as zero.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    text = str(value).strip()
    if text == "" or text.lower() in ("none", "null", "nan"):
        return None
    return Decimal(text)


def require_decimal(value: object, field_name: str) -> Decimal:
    """Same as to_decimal but refuses to produce None. Fail closed on missing data."""
    result = to_decimal(value)
    if result is None:
        raise ValueError(f"required numeric field {field_name!r} was missing or unparseable")
    return result


@dataclass(frozen=True)
class Position:
    """One open position as the exchange reports it."""

    symbol: str
    side: str  # normalised to "long" or "short"
    contracts: Decimal  # always positive; direction lives in `side`
    entry_price: Optional[Decimal]
    mark_price: Optional[Decimal]
    liquidation_price: Optional[Decimal]
    unrealized_pnl: Optional[Decimal]
    margin_mode: Optional[str]
    leverage: Optional[Decimal]
    opened_at_ms: Optional[int] = None

    def describe(self) -> str:
        return f"{self.side} {self.contracts} {self.symbol} @ {self.entry_price}"


@dataclass(frozen=True)
class OpenOrder:
    """One resting order as the exchange reports it."""

    order_id: str
    client_order_id: Optional[str]
    symbol: str
    side: str  # "buy" | "sell"
    order_type: Optional[str]
    price: Optional[Decimal]
    amount: Optional[Decimal]
    filled: Optional[Decimal]
    remaining: Optional[Decimal]
    reduce_only: bool
    status: Optional[str]
    timestamp_ms: Optional[int]

    def describe(self) -> str:
        tag = " reduce-only" if self.reduce_only else ""
        return (
            f"{self.side} {self.amount} {self.symbol} @ {self.price} "
            f"[{self.order_type}{tag}] id={self.order_id}"
        )


@dataclass(frozen=True)
class Fill:
    """One execution as the exchange reports it."""

    trade_id: str
    order_id: Optional[str]
    client_order_id: Optional[str]
    symbol: str
    side: str
    price: Optional[Decimal]
    amount: Optional[Decimal]
    cost: Optional[Decimal]
    fee_cost: Optional[Decimal]
    fee_currency: Optional[str]
    timestamp_ms: Optional[int]


@dataclass(frozen=True)
class ClosedPosition:
    """
    One position that has already been closed, as the exchange reports it.

    Source of truth for the daily loss limit and the consecutive-loss counter.
    Both must survive a process restart, so both are derived from here rather
    than from anything this process remembers.
    """

    symbol: str
    side: Optional[str]
    realised_pnl: Decimal
    closed_at_ms: Optional[int]

    @property
    def is_loss(self) -> bool:
        return self.realised_pnl < 0


@dataclass(frozen=True)
class AccountState:
    """
    A complete, self-consistent picture of the account at one instant, built
    entirely from exchange responses.

    There is no constructor path that produces a partially populated
    AccountState: reconcile() either returns a whole one or raises. A
    half-filled snapshot is worse than no snapshot, because code downstream
    would treat "no positions found" and "positions call failed" identically.
    """

    fetched_at_ms: int
    mode: str
    position_mode: str
    equity: Optional[Decimal]
    available: Optional[Decimal]
    positions: tuple[Position, ...]
    open_orders: tuple[OpenOrder, ...]
    todays_fills: tuple[Fill, ...]
    todays_closed_positions: tuple[ClosedPosition, ...] = ()

    @property
    def live_positions(self) -> tuple[Position, ...]:
        return tuple(p for p in self.positions if p.contracts > 0)

    @property
    def is_flat(self) -> bool:
        """True when no position carries a non-zero size."""
        return all(p.contracts == 0 for p in self.positions)

    @property
    def fetched_at_iso(self) -> str:
        return datetime.fromtimestamp(
            self.fetched_at_ms / 1000, tz=timezone.utc
        ).isoformat()

    def comparable(self) -> tuple:
        """
        The parts of the snapshot that must match across a restart.

        Deliberately excludes fetched_at_ms and the mark-price/PnL fields, which
        move continuously with the market and would make any restart comparison
        fail for reasons that have nothing to do with correctness.
        """
        return (
            self.position_mode,
            tuple(
                (p.symbol, p.side, p.contracts, p.entry_price)
                for p in sorted(self.positions, key=lambda x: (x.symbol, x.side))
            ),
            tuple(
                (o.order_id, o.symbol, o.side, o.price, o.amount, o.reduce_only)
                for o in sorted(self.open_orders, key=lambda x: x.order_id)
            ),
            tuple(sorted(f.trade_id for f in self.todays_fills)),
        )

    def log_payload(self) -> dict:
        """A flat, JSON-safe rendering for structlog. Contains no credentials."""
        return {
            "fetched_at": self.fetched_at_iso,
            "mode": self.mode,
            "position_mode": self.position_mode,
            "equity": str(self.equity) if self.equity is not None else None,
            "available": str(self.available) if self.available is not None else None,
            "is_flat": self.is_flat,
            "position_count": len(self.positions),
            "open_order_count": len(self.open_orders),
            "todays_fill_count": len(self.todays_fills),
            "positions": [p.describe() for p in self.positions],
            "open_orders": [o.describe() for o in self.open_orders],
        }
