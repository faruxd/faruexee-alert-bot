"""
Phase 3 -- Guards.

Every guard is a PURE function over a GuardContext. No network, no clock reads,
no local counters. That matters for one specific reason: the daily entry count
and the loss limit must not reset when the process restarts, so both are derived
from exchange data carried in the context rather than from anything this process
remembers.

Every guard fails closed. If a value needed to evaluate a guard is missing or
unparseable, the guard BLOCKS. "I could not determine whether this is safe" and
"this is safe" are never the same answer.

Guards are checked before every entry. Any single block stops the entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Sequence

from cf_bot.state import ClosedPosition, Fill, Position

# ---------------------------------------------------------------------------
# Fixed limits. Not configurable -- these are the risk model, not tuning knobs.
# ---------------------------------------------------------------------------

# One position at a time, ACROSS ALL SYMBOLS.
#
# Not one per symbol. With 8 symbols at 1% risk each, per-symbol would put 8% at
# risk simultaneously and would breach the -2% daily loss limit on the first two
# losers. Global max-1 is what keeps the risk model coherent.
MAX_CONCURRENT_POSITIONS = 1

MAX_ENTRIES_PER_UTC_DAY = 3
MAX_ORDERS_PER_HOUR = 12
DAILY_LOSS_LIMIT_PCT = Decimal("-2.0")
MAX_CONSECUTIVE_LOSSES = 3

# Settlement / funding times, UTC hours.
SETTLEMENT_HOURS_UTC = (0, 8, 16)
BLACKOUT_MINUTES = 15  # no entries within this many minutes of settlement
FLATTEN_BEFORE_MINUTES = 2  # flatten open positions this long before settlement

MS_PER_MINUTE = 60_000
MS_PER_HOUR = 3_600_000


@dataclass(frozen=True)
class GuardVerdict:
    allowed: bool
    reason: str

    @staticmethod
    def allow(reason: str = "ok") -> "GuardVerdict":
        return GuardVerdict(True, reason)

    @staticmethod
    def block(reason: str) -> "GuardVerdict":
        return GuardVerdict(False, reason)


@dataclass(frozen=True)
class GuardContext:
    """
    Everything the guards need, all of it derived from the exchange.

    Built by the caller from an AccountState plus position history. Nothing in
    here comes from a variable this process incremented.
    """

    now_ms: int
    positions: tuple[Position, ...]
    todays_fills: tuple[Fill, ...]
    todays_closed_positions: tuple[ClosedPosition, ...]
    recent_order_timestamps_ms: tuple[int, ...]
    current_equity: Optional[Decimal]


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _utc(now_ms: int) -> datetime:
    return datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)


def minutes_to_nearest_settlement(now_ms: int) -> Decimal:
    """
    Signed-magnitude distance in minutes to the closest settlement boundary.

    Always non-negative -- it is the absolute distance, whether the boundary is
    ahead or behind.
    """
    now = _utc(now_ms)
    minutes_into_day = Decimal(now.hour * 60 + now.minute) + Decimal(now.second) / 60

    best: Optional[Decimal] = None
    # Include the next day's 00:00 so late-evening times measure correctly.
    for hour in list(SETTLEMENT_HOURS_UTC) + [24]:
        distance = abs(minutes_into_day - Decimal(hour * 60))
        if best is None or distance < best:
            best = distance
    assert best is not None
    return best


def minutes_until_next_settlement(now_ms: int) -> Decimal:
    """Forward-looking distance in minutes to the next settlement boundary."""
    now = _utc(now_ms)
    minutes_into_day = Decimal(now.hour * 60 + now.minute) + Decimal(now.second) / 60

    for hour in list(SETTLEMENT_HOURS_UTC) + [24]:
        boundary = Decimal(hour * 60)
        if boundary >= minutes_into_day:
            return boundary - minutes_into_day
    return Decimal(0)


def in_settlement_blackout(now_ms: int) -> bool:
    """True within BLACKOUT_MINUTES either side of 00:00 / 08:00 / 16:00 UTC."""
    return minutes_to_nearest_settlement(now_ms) <= Decimal(BLACKOUT_MINUTES)


def should_flatten_for_settlement(now_ms: int) -> bool:
    """
    True in the window where open positions must be flattened ahead of settlement.

    Fires from FLATTEN_BEFORE_MINUTES before the boundary up to the boundary
    itself, so a loop running every 15s cannot step over it.
    """
    until = minutes_until_next_settlement(now_ms)
    return Decimal(0) <= until <= Decimal(FLATTEN_BEFORE_MINUTES)


# ---------------------------------------------------------------------------
# Derivations from exchange data
# ---------------------------------------------------------------------------


def count_entries_today(
    closed_positions: Sequence[ClosedPosition], live_positions: Sequence[Position]
) -> int:
    """
    How many positions we OPENED today: closed today + still open.

    Derived from the exchange's own position history, so it survives a restart
    and needs no interpretation of order ids.

    WHY NOT FILLS. The first version counted distinct entry client order ids and
    treated any unrecognised id as another entry. But an exit through a preset
    take-profit or stop carries BITGET's order id, not ours -- so every exit was
    counted as a fresh entry and each round trip consumed two of the three daily
    slots. In production this reported "4/3 entries" after a single completed
    trade and locked out the rest of the day.

    Counting positions instead is unambiguous: one position opened is one entry,
    however many partial fills it took to build or unwind.

    A position opened yesterday and closed today is counted here. That
    over-counts by one on that day, which blocks earlier -- the safe direction.
    """
    live = [p for p in live_positions if p.contracts > 0]
    return len(closed_positions) + len(live)


def realised_pnl_today(closed: Sequence[ClosedPosition]) -> Decimal:
    return sum((c.realised_pnl for c in closed), Decimal(0))


def consecutive_losses(closed: Sequence[ClosedPosition]) -> int:
    """
    Length of the current losing streak, counting backwards from the most
    recent close. A single winner resets it to zero.
    """
    ordered = sorted(closed, key=lambda c: c.closed_at_ms or 0, reverse=True)
    streak = 0
    for position in ordered:
        if position.is_loss:
            streak += 1
        else:
            break
    return streak


def day_start_equity(current_equity: Decimal, todays_realised: Decimal) -> Decimal:
    """
    Reconstruct equity as at 00:00 UTC.

    current - realised_today. This is restart-safe and needs no stored value.
    It assumes no deposits or withdrawals mid-day, which holds here because the
    API key is asserted at startup to have no withdrawal or transfer rights.
    """
    return current_equity - todays_realised


# ---------------------------------------------------------------------------
# The guards
# ---------------------------------------------------------------------------


def guard_position_history_sane(ctx: GuardContext) -> GuardVerdict:
    """
    Refuse to trade when the exchange's own answers contradict each other.

    The daily entry cap AND the daily loss limit are both derived entirely from
    todays_closed_positions. If that list comes back empty when it should not
    be, both guards silently pass -- they fail OPEN, which is the opposite of
    this module's whole contract.

    That happened: a ccxt quirk made the history query return only the first
    configured symbol, so a day of LTC, PEPE and UNI closes read as zero. The
    bot traded past both its entry cap and its loss limit with no complaint.

    Fills today with nothing closed and nothing open is impossible -- a fill
    either opened a position or closed one. Seeing it means the history feed is
    lying, and the correct response is to stop rather than to trade on numbers
    known to be wrong.
    """
    live = [p for p in ctx.positions if p.contracts > 0]
    if ctx.todays_fills and not ctx.todays_closed_positions and not live:
        return GuardVerdict.block(
            f"{len(ctx.todays_fills)} fills today but position history reports nothing "
            "closed and nothing open -- the daily entry cap and loss limit are both "
            "derived from that history, so they cannot be trusted right now"
        )
    return GuardVerdict.allow()


def guard_max_concurrent_positions(ctx: GuardContext) -> GuardVerdict:
    live = [p for p in ctx.positions if p.contracts > 0]
    if len(live) >= MAX_CONCURRENT_POSITIONS:
        held = ", ".join(p.describe() for p in live)
        return GuardVerdict.block(
            f"already holding {len(live)}/{MAX_CONCURRENT_POSITIONS} position(s): {held}"
        )
    return GuardVerdict.allow()


def guard_max_entries_per_day(ctx: GuardContext) -> GuardVerdict:
    entries = count_entries_today(ctx.todays_closed_positions, ctx.positions)
    if entries >= MAX_ENTRIES_PER_UTC_DAY:
        return GuardVerdict.block(
            f"{entries}/{MAX_ENTRIES_PER_UTC_DAY} entries already made this UTC day"
        )
    return GuardVerdict.allow(f"{entries}/{MAX_ENTRIES_PER_UTC_DAY} entries today")


def guard_max_orders_per_hour(ctx: GuardContext) -> GuardVerdict:
    """Runaway-loop protection. Counts every order we sent, not just entries."""
    cutoff = ctx.now_ms - MS_PER_HOUR
    recent = [ts for ts in ctx.recent_order_timestamps_ms if ts >= cutoff]
    if len(recent) >= MAX_ORDERS_PER_HOUR:
        return GuardVerdict.block(
            f"{len(recent)}/{MAX_ORDERS_PER_HOUR} orders sent in the last hour"
        )
    return GuardVerdict.allow()


def guard_daily_loss_limit(ctx: GuardContext) -> GuardVerdict:
    """
    Disable until the next UTC day on either:
      - realised PnL <= -2% of the day's starting equity, or
      - 3 consecutive losing positions.
    """
    streak = consecutive_losses(ctx.todays_closed_positions)
    if streak >= MAX_CONSECUTIVE_LOSSES:
        return GuardVerdict.block(
            f"{streak} consecutive losses (limit {MAX_CONSECUTIVE_LOSSES}); "
            "disabled until the next UTC day"
        )

    if ctx.current_equity is None:
        return GuardVerdict.block(
            "equity unknown, cannot evaluate the daily loss limit; failing closed"
        )

    realised = realised_pnl_today(ctx.todays_closed_positions)
    opening = day_start_equity(ctx.current_equity, realised)

    if opening <= 0:
        return GuardVerdict.block(
            f"reconstructed day-start equity is {opening}, which is not usable; failing closed"
        )

    loss_pct = realised / opening * Decimal(100)
    if loss_pct <= DAILY_LOSS_LIMIT_PCT:
        return GuardVerdict.block(
            f"daily PnL {loss_pct:.2f}% has reached the {DAILY_LOSS_LIMIT_PCT}% limit; "
            "disabled until the next UTC day"
        )

    return GuardVerdict.allow(f"daily PnL {loss_pct:.2f}%")


def guard_settlement_blackout(ctx: GuardContext) -> GuardVerdict:
    if in_settlement_blackout(ctx.now_ms):
        distance = minutes_to_nearest_settlement(ctx.now_ms)
        return GuardVerdict.block(
            f"within {distance:.1f} min of a settlement boundary "
            f"(blackout is +/-{BLACKOUT_MINUTES} min)"
        )
    return GuardVerdict.allow()


ALL_GUARDS = (
    # Data-integrity first: if the exchange's answers contradict each other,
    # every guard below is evaluating numbers we know are wrong.
    guard_position_history_sane,
    guard_max_concurrent_positions,
    guard_max_entries_per_day,
    guard_max_orders_per_hour,
    guard_daily_loss_limit,
    guard_settlement_blackout,
)


def check_all(ctx: GuardContext) -> GuardVerdict:
    """
    Run every guard. The first block wins and no entry is permitted.

    Order matters only for which reason gets reported; any single block is fatal
    to the entry attempt.
    """
    for guard in ALL_GUARDS:
        verdict = guard(ctx)
        if not verdict.allowed:
            return GuardVerdict.block(f"{guard.__name__}: {verdict.reason}")
    return GuardVerdict.allow("all guards passed")
