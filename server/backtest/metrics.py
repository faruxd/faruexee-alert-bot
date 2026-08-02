"""
Backtest metrics.

Deliberately plain arithmetic on Decimals rather than pandas: the fill model and
the strategy both work in Decimal, and routing results through float just to get
a summary table would let rounding differences appear between the backtest and
the live path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Sequence

from backtest.engine import Trade


@dataclass(frozen=True)
class Metrics:
    trade_count: int
    wins: int
    losses: int
    win_rate: Decimal
    gross_profit: Decimal
    gross_loss: Decimal
    profit_factor: Decimal
    net_pnl: Decimal
    total_fees: Decimal
    total_funding: Decimal
    avg_r: Decimal
    max_drawdown: Decimal
    max_drawdown_pct: Decimal
    longest_losing_streak: int
    starting_equity: Decimal
    ending_equity: Decimal
    monthly_returns: dict[str, Decimal]
    exit_reasons: dict[str, int]


def compute(trades: Sequence[Trade], starting_equity: Decimal) -> Metrics:
    if not trades:
        return Metrics(
            trade_count=0,
            wins=0,
            losses=0,
            win_rate=Decimal(0),
            gross_profit=Decimal(0),
            gross_loss=Decimal(0),
            profit_factor=Decimal(0),
            net_pnl=Decimal(0),
            total_fees=Decimal(0),
            total_funding=Decimal(0),
            avg_r=Decimal(0),
            max_drawdown=Decimal(0),
            max_drawdown_pct=Decimal(0),
            longest_losing_streak=0,
            starting_equity=starting_equity,
            ending_equity=starting_equity,
            monthly_returns={},
            exit_reasons={},
        )

    ordered = sorted(trades, key=lambda t: t.exit_ts)

    wins = [t for t in ordered if t.is_win]
    losses = [t for t in ordered if not t.is_win]

    gross_profit = sum((t.net for t in wins), Decimal(0))
    gross_loss = abs(sum((t.net for t in losses), Decimal(0)))

    # Profit factor is NET of fees and funding. A gross-of-costs PF is the
    # single most common way a backtest overstates a strategy.
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else Decimal("Infinity")

    equity = starting_equity
    peak = starting_equity
    max_dd = Decimal(0)
    max_dd_pct = Decimal(0)
    streak = 0
    longest_streak = 0
    monthly: dict[str, Decimal] = {}

    for trade in ordered:
        equity += trade.net
        peak = max(peak, equity)
        drawdown = peak - equity
        if drawdown > max_dd:
            max_dd = drawdown
            max_dd_pct = (drawdown / peak * Decimal(100)) if peak > 0 else Decimal(0)

        if trade.is_win:
            streak = 0
        else:
            streak += 1
            longest_streak = max(longest_streak, streak)

        month = datetime.fromtimestamp(trade.exit_ts / 1000, tz=timezone.utc).strftime("%Y-%m")
        monthly[month] = monthly.get(month, Decimal(0)) + trade.net

    exit_reasons: dict[str, int] = {}
    for trade in ordered:
        exit_reasons[trade.exit_reason] = exit_reasons.get(trade.exit_reason, 0) + 1

    return Metrics(
        trade_count=len(ordered),
        wins=len(wins),
        losses=len(losses),
        win_rate=Decimal(len(wins)) / Decimal(len(ordered)) * Decimal(100),
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=profit_factor,
        net_pnl=sum((t.net for t in ordered), Decimal(0)),
        total_fees=sum((t.fees for t in ordered), Decimal(0)),
        total_funding=sum((t.funding for t in ordered), Decimal(0)),
        avg_r=sum((t.r_multiple for t in ordered), Decimal(0)) / Decimal(len(ordered)),
        max_drawdown=max_dd,
        max_drawdown_pct=max_dd_pct,
        longest_losing_streak=longest_streak,
        starting_equity=starting_equity,
        ending_equity=equity,
        monthly_returns=monthly,
        exit_reasons=exit_reasons,
    )


def render(metrics: Metrics, warnings: Sequence[str], funding_note: str) -> str:
    """Human-readable report."""
    lines: list[str] = []
    add = lines.append

    add("=" * 66)
    add("  CF 'FORCED FLOW' BACKTEST")
    add("=" * 66)

    if metrics.trade_count == 0:
        add("")
        add("  NO TRADES.")
        add("")
        for warning in warnings:
            add(f"  ! {warning}")
        add("")
        add("  Zero trades is a result, not a bug. The regime filter requires a")
        add("  full 30 days of trailing ATR before it will pass, and a post-only")
        add("  entry only fills if a later bar trades through it.")
        add("=" * 66)
        return "\n".join(lines)

    pf = (
        "inf"
        if metrics.profit_factor == Decimal("Infinity")
        else f"{metrics.profit_factor:.2f}"
    )

    add("")
    add(f"  Trades              {metrics.trade_count}")
    add(f"  Win rate            {metrics.win_rate:.1f}%  ({metrics.wins}W / {metrics.losses}L)")
    add(f"  Profit factor (net) {pf}")
    add(f"  Avg R               {metrics.avg_r:.3f}")
    add(f"  Longest losing run  {metrics.longest_losing_streak}")
    add("")
    add(f"  Starting equity     {metrics.starting_equity:.2f}")
    add(f"  Ending equity       {metrics.ending_equity:.2f}")
    add(f"  Net PnL             {metrics.net_pnl:.2f}")
    add(f"  Fees paid           {metrics.total_fees:.2f}")
    add(f"  Funding paid        {metrics.total_funding:.2f}")
    add(f"  Max drawdown        {metrics.max_drawdown:.2f}  ({metrics.max_drawdown_pct:.1f}%)")
    add("")
    add("  Exits")
    for reason, count in sorted(metrics.exit_reasons.items(), key=lambda kv: -kv[1]):
        add(f"    {reason:20} {count}")
    add("")
    add("  Monthly net")
    for month in sorted(metrics.monthly_returns):
        value = metrics.monthly_returns[month]
        add(f"    {month}  {value:>12.2f}")
    add("")
    add(f"  ! {funding_note}")
    for warning in warnings:
        add(f"  ! {warning}")
    add("=" * 66)

    return "\n".join(lines)
