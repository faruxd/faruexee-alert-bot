"""
Historical bar loading for the backtester.

Public endpoints only -- no API key is needed or used to run a backtest.

A note on Bitget's data limits, because it constrains what a backtest can say:
the recent-candles endpoint serves roughly 30 days of 5m bars, and the strategy
needs a full 30 days of trailing ATR before the regime filter will pass a single
bar. Fetching only "recent" data therefore yields ZERO tradeable bars. This
module pages the history-candles endpoint via `since` so there is real history
in front of the warm-up window.
"""

from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path
from typing import Optional

import ccxt.async_support as ccxt

from cf_bot.strategy import Bar

BAR_MS_5M = 5 * 60 * 1000


async def fetch_history(
    symbol: str,
    timeframe: str,
    since_ms: int,
    until_ms: Optional[int] = None,
    per_request: int = 1000,
) -> list[Bar]:
    """Page forward from since_ms. Public data; no credentials involved."""
    exchange = ccxt.bitget({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    collected: dict[int, list] = {}

    try:
        await exchange.load_markets()
        cursor = since_ms
        while True:
            batch = await exchange.fetch_ohlcv(
                symbol, timeframe, since=cursor, limit=per_request
            )
            if not batch:
                break

            for row in batch:
                collected[int(row[0])] = row

            last_ts = int(batch[-1][0])
            if until_ms is not None and last_ts >= until_ms:
                break
            next_cursor = last_ts + 1
            if next_cursor <= cursor:
                break
            cursor = next_cursor

            if len(batch) < per_request:
                break
    finally:
        await exchange.close()

    rows = [collected[ts] for ts in sorted(collected)]
    if until_ms is not None:
        rows = [r for r in rows if int(r[0]) <= until_ms]
    return [Bar.from_ccxt(r) for r in rows]


def load_csv(path: Path) -> list[Bar]:
    """
    Load bars from CSV: timestamp_ms,open,high,low,close,volume

    A header row is detected and skipped.
    """
    bars: list[Bar] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.reader(handle):
            if not row or len(row) < 6:
                continue
            try:
                timestamp = int(row[0])
            except ValueError:
                continue  # header
            bars.append(
                Bar(
                    timestamp_ms=timestamp,
                    open=Decimal(row[1]),
                    high=Decimal(row[2]),
                    low=Decimal(row[3]),
                    close=Decimal(row[4]),
                    volume=Decimal(row[5]),
                )
            )
    bars.sort(key=lambda b: b.timestamp_ms)
    return bars


def save_csv(bars: list[Bar], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp_ms", "open", "high", "low", "close", "volume"])
        for bar in bars:
            writer.writerow(
                [bar.timestamp_ms, bar.open, bar.high, bar.low, bar.close, bar.volume]
            )


def check_continuity(bars: list[Bar], expected_gap_ms: int = BAR_MS_5M) -> list[str]:
    """
    Report gaps in the series.

    Gaps matter: the ATR percentile window is counted in BARS, so a series with
    holes silently uses a longer wall-clock window than 30 days.
    """
    warnings: list[str] = []
    for index in range(1, len(bars)):
        gap = bars[index].timestamp_ms - bars[index - 1].timestamp_ms
        if gap != expected_gap_ms:
            missing = gap // expected_gap_ms - 1
            if missing > 0:
                warnings.append(
                    f"gap of {missing} bar(s) before {bars[index].timestamp_ms}"
                )
    if len(warnings) > 10:
        head = warnings[:10]
        head.append(f"... and {len(warnings) - 10} more gaps")
        return head
    return warnings
