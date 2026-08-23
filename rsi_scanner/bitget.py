"""
Bitget public market data.

No API key, no signing, read-only. Two things in here are not obvious and
were both verified against the live API before this code was written:

1. BITGET'S NATIVE DAILY BAR CLOSES AT 16:00 UTC, NOT MIDNIGHT.
   Daily candles come back stamped 16:00 -- they run 16:00 UTC to 16:00 UTC,
   i.e. midnight in UTC+8. An RSI computed on those bars will NOT match a
   TradingView chart set to UTC, because the bars are cut in different
   places. So the default here resamples 4H bars into true UTC-midnight days.
   Set DAY_BOUNDARY=exchange to use Bitget's native 1D bars instead.

2. HISTORY IS CAPPED AT 90 DAYS, ON EVERY GRANULARITY.
   limit=1000 on 1D returns 90 rows. 4H returns 540 rows, which is the same
   90 days. history-candles does not help. This is a data-retention limit,
   not a paging limit. 90 days is comfortably enough for RSI(14) to converge
   -- roughly 75 smoothing steps past the seed -- but it rules out anything
   needing a 200-day lookback, which is why the trade bot's HTF filter is
   pinned to an EMA rather than structure.
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional, Sequence

import requests

BASE_URL = "https://api.bitget.com"
PRODUCT_TYPE = "USDT-FUTURES"

MS_PER_DAY = 86_400_000
MS_PER_4H = 14_400_000
BARS_PER_UTC_DAY_4H = 6

REQUEST_TIMEOUT = 15.0


class FetchError(Exception):
    """A symbol's candles could not be retrieved. Never fatal to a scan."""


def fetch_candles(
    symbol: str,
    granularity: str,
    limit: int = 1000,
    session: Optional[requests.Session] = None,
) -> List[List[float]]:
    """
    Raw candles, oldest -> newest, as [ts_ms, open, high, low, close, volume].

    The final row is the CURRENTLY FORMING bar. Callers must drop it -- see
    drop_forming_bar(). Nothing here does that for you, because whether a bar
    is closed depends on the granularity being requested.
    """
    url = (
        f"{BASE_URL}/api/v2/mix/market/candles"
        f"?symbol={symbol}&productType={PRODUCT_TYPE}"
        f"&granularity={granularity}&limit={limit}"
    )
    getter = session.get if session is not None else requests.get
    try:
        response = getter(url, timeout=REQUEST_TIMEOUT)
        payload = response.json()
    except Exception as exc:
        raise FetchError(f"{symbol} {granularity}: request failed: {exc}") from exc

    if payload.get("code") not in ("00000", 0, None):
        raise FetchError(f"{symbol} {granularity}: API code {payload.get('code')} {payload.get('msg')}")

    rows = payload.get("data") or []
    if not rows:
        raise FetchError(f"{symbol} {granularity}: empty candle set (delisted or bad symbol?)")

    out: List[List[float]] = []
    for row in rows:
        try:
            out.append([float(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]),
                        float(row[5]) if len(row) > 5 else 0.0])
        except (TypeError, ValueError, IndexError):
            continue
    out.sort(key=lambda r: r[0])
    return out


def drop_forming_bar(candles: Sequence[Sequence[float]], bar_ms: int, now_ms: Optional[int] = None) -> List[List[float]]:
    """
    Remove any trailing bar whose period has not elapsed.

    Checked against the clock rather than assumed, so the result is correct
    whatever time the scan happens to run. Getting this wrong is the classic
    daily-scanner bug: you evaluate a bar that is four hours old, alert on
    it, and the signal has vanished by the real close.
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    return [list(c) for c in candles if c[0] + bar_ms <= now_ms]


def resample_4h_to_utc_days(candles: Sequence[Sequence[float]]) -> List[List[float]]:
    """
    Fold 4H bars into UTC-midnight daily bars.

    Only days with all six 4H bars present are emitted. A partial day -- the
    one at the start of the 90-day window, and the one still forming at the
    end -- would produce a close that is not a real daily close, and RSI would
    be computed off a phantom bar.
    """
    buckets: Dict[int, List[List[float]]] = {}
    for c in candles:
        day = int(c[0]) // MS_PER_DAY
        buckets.setdefault(day, []).append(list(c))

    days: List[List[float]] = []
    for day in sorted(buckets):
        bars = sorted(buckets[day], key=lambda r: r[0])
        if len(bars) != BARS_PER_UTC_DAY_4H:
            continue
        days.append([
            float(day * MS_PER_DAY),
            bars[0][1],
            max(b[2] for b in bars),
            min(b[3] for b in bars),
            bars[-1][4],
            sum(b[5] for b in bars),
        ])
    return days


def daily_closes(
    symbol: str,
    boundary: str = "utc",
    session: Optional[requests.Session] = None,
    now_ms: Optional[int] = None,
) -> List[List[float]]:
    """
    Closed daily bars for a symbol, oldest -> newest.

    boundary="utc"      true UTC-midnight days, resampled from 4H. Matches a
                        TradingView chart set to UTC. Default.
    boundary="exchange" Bitget's native 1D bars, which close at 16:00 UTC.
    """
    if boundary == "exchange":
        raw = fetch_candles(symbol, "1D", limit=1000, session=session)
        return drop_forming_bar(raw, MS_PER_DAY, now_ms=now_ms)

    if boundary != "utc":
        raise ValueError(f"unknown day boundary {boundary!r}; expected 'utc' or 'exchange'")

    raw = fetch_candles(symbol, "4H", limit=1000, session=session)
    closed_4h = drop_forming_bar(raw, MS_PER_4H, now_ms=now_ms)
    return resample_4h_to_utc_days(closed_4h)
