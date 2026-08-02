"""
Backtest CLI -- a separate binary that shares the live strategy module.

    python -m backtest --symbol BTC/USDT:USDT --days 180
    python -m backtest --csv data/btc_5m.csv
    python -m backtest --symbol BTC/USDT:USDT --days 180 --save data/btc_5m.csv

This does NOT optimise parameters. Walk-forward is a separate step run by a
human. The --k/--s/--p flags exist to evaluate ONE candidate set at a time; if
you find yourself looping this in a shell script over a grid, you are curve
fitting and the result will not survive live.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from decimal import Decimal
from pathlib import Path

from backtest.data import check_continuity, fetch_history, load_csv, save_csv
from backtest.engine import BacktestConfig, run_backtest
from backtest.metrics import compute, render
from cf_bot.strategy import PERCENTILE_LOOKBACK_BARS, StrategyParams

MS_PER_DAY = 86_400_000


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backtest", description="CF Forced Flow backtester")
    # --symbol is both the fetch target AND the label on a CSV run, so these are
    # not mutually exclusive. At least one is required; validated in main().
    parser.add_argument("--symbol", help="ccxt unified symbol, e.g. BTC/USDT:USDT")
    parser.add_argument("--csv", type=Path, help="load bars from a CSV instead of the network")

    parser.add_argument("--days", type=int, default=180, help="days of history to fetch")
    parser.add_argument("--save", type=Path, help="write fetched bars to this CSV")
    parser.add_argument("--equity", type=Decimal, default=Decimal("1000"))
    parser.add_argument("--risk-pct", type=Decimal, default=Decimal("1.0"), help="percent")

    parser.add_argument("--k", type=Decimal, default=Decimal("2.5"))
    parser.add_argument("--s", type=Decimal, default=Decimal("1.25"))
    parser.add_argument("--p", type=Decimal, default=Decimal("30"))

    parser.add_argument(
        "--funding",
        type=Decimal,
        default=Decimal("0"),
        help=(
            "funding rate assumed at every bar, as a fraction per 8h. "
            "0 assumes funding was always benign, which is OPTIMISTIC."
        ),
    )
    parser.add_argument(
        "--no-guards",
        action="store_true",
        help="disable live guards. Produces a result the bot will never achieve.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.csv and not args.symbol:
        print("ERROR: pass --symbol to fetch, or --csv to load from disk.", file=sys.stderr)
        return 2

    if args.csv:
        bars = load_csv(args.csv)
        symbol = args.symbol or args.csv.stem
        print(f"Loaded {len(bars)} bars from {args.csv}")
    else:
        symbol = args.symbol
        since = int(time.time() * 1000) - args.days * MS_PER_DAY
        print(f"Fetching {args.days}d of 5m bars for {symbol} ...")
        bars = asyncio.run(fetch_history(symbol, "5m", since_ms=since))
        print(f"Fetched {len(bars)} bars")
        if args.save:
            save_csv(bars, args.save)
            print(f"Saved to {args.save}")

    if not bars:
        print("No bars. Nothing to test.", file=sys.stderr)
        return 1

    warnings = check_continuity(bars)

    tradeable = len(bars) - PERCENTILE_LOOKBACK_BARS
    if tradeable <= 0:
        print(
            f"\nERROR: {len(bars)} bars is not enough. The regime filter needs a full "
            f"{PERCENTILE_LOOKBACK_BARS}-bar (30 day) trailing ATR window before it will "
            f"pass a single bar, so this run would evaluate zero tradeable bars.\n"
            f"Fetch more history: --days {int((PERCENTILE_LOOKBACK_BARS * 2) / 288) + 1} "
            f"or more.",
            file=sys.stderr,
        )
        return 1

    print(f"Warm-up consumes {PERCENTILE_LOOKBACK_BARS} bars; {tradeable} bars are tradeable.")

    config = BacktestConfig(
        symbol=symbol,
        params=StrategyParams(k=args.k, s=args.s, p=args.p),
        risk_pct=args.risk_pct,
        starting_equity=args.equity,
        assumed_funding=args.funding,
        apply_guards=not args.no_guards,
    )

    trades, engine_warnings = run_backtest(bars, config)
    warnings.extend(engine_warnings)

    if args.no_guards:
        warnings.append(
            "GUARDS DISABLED: these results are not achievable by the live bot."
        )

    funding_note = (
        "Funding assumed 0 at every bar (optimistic). Pass --funding to model a cost."
        if args.funding == 0
        else f"Funding modelled at {args.funding} per 8h on every settlement crossed."
    )

    print()
    print(render(compute(trades, args.equity), warnings, funding_note))
    return 0


if __name__ == "__main__":
    sys.exit(main())
