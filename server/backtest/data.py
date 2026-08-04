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

import asyncio
import csv
from decimal import Decimal
from pathlib import Path
from typing import Optional

import ccxt.async_support as ccxt

from cf_bot.exchange import MAX_BARS_PER_REQUEST
from cf_bot.strategy import Bar

BAR_MS_5M = 5 * 60 * 1000


async def _fetch_batch_with_backoff(
    exchange,
    symbol: str,
    timeframe: str,
    cursor: int,
    limit: int,
    max_attempts: int = 6,
) -> list[list]:
    """
    One page, retrying on rate limiting.

    A long backfill issues hundreds of requests back to back and WILL trip
    Bitget's 429. That is not an error worth aborting a 20-minute download for,
    so we wait and continue. This is the backtester only -- the live bot runs
    nowhere near these rates.
    """
    delay = 2.0
    for attempt in range(1, max_attempts + 1):
        try:
            return await exchange.fetch_ohlcv(symbol, timeframe, since=cursor, limit=limit)
        except (ccxt.DDoSProtection, ccxt.RateLimitExceeded, ccxt.NetworkError) as exc:
            if attempt == max_attempts:
                raise
            print(
                f"  rate limited ({type(exc).__name__}), waiting {delay:.0f}s "
                f"[{attempt}/{max_attempts}]",
                flush=True,
            )
            await asyncio.sleep(delay)
            delay *= 2
    return []


async def fetch_history(
    symbol: str,
    timeframe: str,
    since_ms: int,
    until_ms: Optional[int] = None,
    per_request: int = MAX_BARS_PER_REQUEST,
    progress: bool = True,
) -> list[Bar]:
    """
    Page forward from since_ms. Public data; no credentials involved.

    Bitget returns at most 200 rows however many you request, so a batch being
    "smaller than asked for" says nothing about whether more history exists.
    An earlier version broke out of the loop on that condition and silently
    fetched only the first 200 bars. Termination is now on an empty batch, a
    cursor that fails to advance, or reaching until_ms.
    """
    exchange = ccxt.bitget({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    collected: dict[int, list] = {}

    try:
        await exchange.load_markets()
        cursor = since_ms
        while True:
            batch = await _fetch_batch_with_backoff(
                exchange, symbol, timeframe, cursor, per_request
            )
            if not batch:
                break

            for row in batch:
                collected[int(row[0])] = row

            if progress and len(collected) % 5000 < per_request:
                print(f"  ... {len(collected)} bars", flush=True)

            last_ts = int(batch[-1][0])
            if until_ms is not None and last_ts >= until_ms:
                break
            next_cursor = last_ts + 1
            if next_cursor <= cursor:
                break
            cursor = next_cursor
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


def resample(bars: list[Bar], factor: int) -> list[Bar]:
    """
    Aggregate `factor` consecutive bars into one (5m -> 15m at factor 3).

    Buckets are aligned to wall-clock boundaries of the TARGET timeframe, not to
    the start of the array. Aligning to the array would produce 15m bars at
    07:05, 07:20, ... which is not what the exchange's own 15m series looks
    like, and the trend filter would be reading different candles live than in
    the backtest.

    Only COMPLETE buckets are emitted. A partial trailing bucket is the
    equivalent of a forming candle and must not be evaluated.
    """
    if factor < 1:
        raise ValueError(f"resample factor must be >= 1, got {factor}")
    if factor == 1:
        return list(bars)
    if not bars:
        return []

    target_ms = BAR_MS_5M * factor
    buckets: dict[int, list[Bar]] = {}
    for candle in bars:
        bucket_ts = (candle.timestamp_ms // target_ms) * target_ms
        buckets.setdefault(bucket_ts, []).append(candle)

    out: list[Bar] = []
    for bucket_ts in sorted(buckets):
        group = sorted(buckets[bucket_ts], key=lambda b: b.timestamp_ms)
        if len(group) != factor:
            continue  # incomplete bucket
        out.append(
            Bar(
                timestamp_ms=bucket_ts,
                open=group[0].open,
                high=max(b.high for b in group),
                low=min(b.low for b in group),
                close=group[-1].close,
                volume=sum((b.volume for b in group), Decimal(0)),
            )
        )
    return out


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
