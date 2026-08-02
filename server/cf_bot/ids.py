"""
Deterministic client order IDs.

This is the only defence against a network timeout creating a duplicate entry.
If we send an order, time out without a response, and retry, the exchange must
reject the second one as a duplicate rather than open a second position.

The id is derived purely from (symbol, signal_bar_ts, side, purpose). The same
signal is therefore incapable of producing two distinct order ids, no matter how
many times the process restarts or how many times a call is retried.

Nothing random, nothing time-based, nothing from a counter. If you are tempted
to add a nonce here, the entire duplicate-protection story collapses.
"""

from __future__ import annotations

import hashlib

# Bitget clientOid accepts letters, digits, hyphen and underscore, up to 64
# chars. We stay far inside that.
PREFIX = "cf"

PURPOSE_ENTRY = "e"
PURPOSE_STOP = "s"
PURPOSE_TARGET = "t"
PURPOSE_FLATTEN = "f"

_ALL_PURPOSES = (PURPOSE_ENTRY, PURPOSE_STOP, PURPOSE_TARGET, PURPOSE_FLATTEN)


def client_order_id(symbol: str, signal_bar_ts: int, side: str, purpose: str) -> str:
    """
    Build the deterministic id for one leg of one signal.

    Deliberately hashed rather than concatenated: symbols contain '/' and ':'
    which are not safe in a clientOid, and a raw concatenation would overflow
    the length limit on longer symbols.
    """
    if purpose not in _ALL_PURPOSES:
        raise ValueError(f"unknown order purpose {purpose!r}, expected one of {_ALL_PURPOSES}")
    if side not in ("long", "short"):
        raise ValueError(f"side must be 'long' or 'short', got {side!r}")
    if not symbol:
        raise ValueError("symbol is required")

    payload = f"{symbol}|{int(signal_bar_ts)}|{side}|{purpose}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"{PREFIX}{purpose}{digest}"


def is_ours(client_oid: str | None) -> bool:
    """True if this id was minted by us."""
    if not client_oid:
        return False
    return client_oid.startswith(PREFIX) and len(client_oid) == len(PREFIX) + 1 + 20


def purpose_of(client_oid: str | None) -> str | None:
    """Extract the purpose marker, or None if the id is not ours."""
    if not is_ours(client_oid):
        return None
    marker = client_oid[len(PREFIX)]
    return marker if marker in _ALL_PURPOSES else None


def is_entry(client_oid: str | None) -> bool:
    return purpose_of(client_oid) == PURPOSE_ENTRY
