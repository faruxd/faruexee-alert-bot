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
from cf_bot.scalper import MIN_SIGNAL_BARS as SCALPER_MIN_SIGNAL_BARS, ScalperParams
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

    parser.add_argument(
        "--strategy",
        choices=["ema_scalper", "forced_flow"],
        default="ema_scalper",
        help="which strategy to drive (default: ema_scalper)",
    )

    # forced_flow
    parser.add_argument("--k", type=Decimal, default=Decimal("2.5"))
    parser.add_argument("--s", type=Decimal, default=Decimal("1.25"))
    parser.add_argument("--p", type=Decimal, default=Decimal("30"))

    # ema_scalper
    parser.add_argument("--ema-fast", type=int, default=9)
    parser.add_argument("--ema-slow", type=int, default=21)
    parser.add_argument("--ema-trend", type=int, default=50)
    parser.add_argument("--atr-mult", type=Decimal, default=Decimal("1.5"))
    parser.add_argument("--target-r", type=Decimal, default=Decimal("2.0"))

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
    is_scalper = args.strategy == "ema_scalper"

    warmup = SCALPER_MIN_SIGNAL_BARS if is_scalper else PERCENTILE_LOOKBACK_BARS
    tradeable = len(bars) - warmup
    if tradeable <= 0:
        print(
            f"\nERROR: {len(bars)} bars is not enough. {args.strategy} needs a "
            f"{warmup}-bar warm-up before it will evaluate anything, so this run would "
            f"test zero bars.\nFetch more history with --days.",
            file=sys.stderr,
        )
        return 1

    print(
        f"Strategy {args.strategy}: warm-up consumes {warmup} bars; "
        f"{tradeable} bars are tradeable."
    )

    config = BacktestConfig(
        symbol=symbol,
        params=StrategyParams(k=args.k, s=args.s, p=args.p),
        risk_pct=args.risk_pct,
        starting_equity=args.equity,
        assumed_funding=args.funding,
        apply_guards=not args.no_guards,
        scalper_params=(
            ScalperParams(
                ema_fast=args.ema_fast,
                ema_slow=args.ema_slow,
                ema_trend=args.ema_trend,
                atr_mult=args.atr_mult,
                target_r=args.target_r,
            )
            if is_scalper
            else None
        ),
    )

    trades, engine_warnings = run_backtest(bars, config)
    warnings.extend(engine_warnings)

    if args.no_guards:
        warnings.append(
            "GUARDS DISABLED: these results are not achievable by the live bot."
        )

    if is_scalper:
        warnings.append(
            "Scalper fills are modelled as ALWAYS market at the next bar's open, "
            "always taker (0.060%). Live, the passive leg fills some of the time at "
            "maker (0.020%), so real fees should land BELOW this. Treat the result "
            "as a floor, not a forecast."
        )

    funding_note = (
        "Funding assumed 0 at every bar (optimistic). Pass --funding to model a cost."
        if args.funding == 0
        else f"Funding modelled at {args.funding} per 8h on every settlement crossed."
    )

    title = (
        f"EMA SCALPER  {args.ema_fast}/{args.ema_slow} on 5m, {args.ema_trend} on 15m  "
        f"| stop {args.atr_mult}xATR | target {args.target_r}R"
        if is_scalper
        else f"FORCED FLOW  k={args.k} s={args.s} p={args.p}"
    )

    print()
    print(render(compute(trades, args.equity), warnings, funding_note, title))
    return 0


if __name__ == "__main__":
    sys.exit(main())
