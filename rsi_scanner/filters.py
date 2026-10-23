"""
Trade filters: a reset is only alerted when trend, market and positioning agree.

These are the three filters that survived the backtests
(server/research/rsi_reset_filter_backtest.py), applied to every alert, 1D and 4H:

  trend    longs only while the coin's last completed daily close is above its
           200-day EMA; shorts only below it.
  btc      altcoins only: longs only while BTC's last completed daily close is
           above its 50-day EMA; shorts only below it. BTC itself and non-crypto
           symbols are not gated by this.
  funding  skip a long when the coin's last-7-day funding is above the 90th
           percentile of its own trailing year (the crowd is already long); skip
           a short below the 10th percentile.

WHY BINANCE, NOT BITGET. Bitget's API keeps only ~90 days of candles and ~90
days of funding. A 200-day EMA and a one-year funding comparison need more, and
Binance is also what the backtests ran on. Daily candles come from Binance's
public market-data mirror (data-api.binance.vision), which is meant for exactly
this. Funding comes from Binance futures; if that is unreachable (its main API
refuses connections from some regions, US-hosted CI runners among them), the
check falls back to Bitget's 90 days and the digest says so. A fallback that
compared against a shorter window silently would be a different filter
pretending to be the tested one.

A check that cannot run -- the symbol is not on Binance (CRO, the metals,
crude), or the data request fails -- does NOT drop the alert. It is sent marked
unverified. Silently dropping it would make a broken data source look like a
quiet market.

Pure Python on purpose: the scanner's only dependency is requests.
"""

from __future__ import annotations

import bisect
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import requests

BINANCE_KLINES = "https://data-api.binance.vision/api/v3/klines"
BINANCE_FUNDING = "https://fapi.binance.com/fapi/v1/fundingRate"
BITGET_FUNDING = "https://api.bitget.com/api/v2/mix/market/history-fund-rate"

MS_PER_DAY = 86_400_000
REQUEST_TIMEOUT = 15.0

TREND_EMA = 200
BTC_EMA = 50
BTC_SYMBOL = "BTCUSDT"
FUND_WINDOW_DAYS = 7
FUND_LOOKBACK_DAYS = 365
FUND_PCT_HIGH = 0.90
FUND_PCT_LOW = 0.10
# Fewer daily comparison points than this and the percentile is noise. Low
# enough that Bitget's ~83 usable days still qualify as a fallback.
FUND_MIN_POINTS = 60

SOURCE_BINANCE = "binance"
SOURCE_BITGET = "bitget-90d"


@dataclass
class Verdict:
    # True = every check passed. False = at least one check blocked it.
    # None = nothing blocked it, but some check could not run.
    passed: Optional[bool]
    blocked_by: List[str] = field(default_factory=list)
    unavailable: List[str] = field(default_factory=list)
    funding_source: Optional[str] = None


# ---------------------------------------------------------------------------
# Maths
# ---------------------------------------------------------------------------

def ema_tv(values: Sequence[float], length: int) -> Optional[float]:
    """
    Last value of an EMA seeded with the SMA of the first `length` values.

    The same seeding as TradingView's ta.ema and the backtest's tv_ema, so the
    number here matches the orange line on the chart. None if too short.
    """
    if len(values) < length:
        return None
    ema = sum(values[:length]) / length
    alpha = 2.0 / (length + 1)
    for value in values[length:]:
        ema = alpha * value + (1 - alpha) * ema
    return ema


def percentile(sorted_values: Sequence[float], q: float) -> float:
    """Linear interpolation between closest ranks (numpy/pandas default)."""
    if not sorted_values:
        raise ValueError("percentile of an empty sequence")
    pos = (len(sorted_values) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def funding_crowding(
    settlements: Sequence[Tuple[int, float]], now_ms: int
) -> Optional[Tuple[float, float, float]]:
    """
    (current 7-day funding, 10th pct, 90th pct) or None if history is too short
    or stale.

    Sums of rates rather than means, so a coin whose funding interval changed
    from 8h to 4h is still compared like for like.

    Every window -- the current one and the year of comparison windows -- ends
    on the LATEST SETTLEMENT that exists, stepped back a day at a time. Anchoring
    to the clock instead would make the current window hold one settlement
    fewer or more than the history depending on whether the newest one had been
    published yet, which shifts the answer for no market reason. This is also
    how the backtest measured it (a rolling 7-day window ending at each event).
    """
    if not settlements:
        return None
    pts = sorted(settlements)
    times = [t for t, _ in pts]
    if now_ms - times[-1] > 2 * MS_PER_DAY:
        return None  # no fresh funding data: don't judge crowding on old news
    prefix = [0.0]
    for _, rate in pts:
        prefix.append(prefix[-1] + rate)
    window = FUND_WINDOW_DAYS * MS_PER_DAY

    def window_sum(end_ms: int) -> float:
        hi = bisect.bisect_right(times, end_ms)
        lo = bisect.bisect_right(times, end_ms - window)
        return prefix[hi] - prefix[lo]

    anchor = times[-1]
    history = []
    for i in range(FUND_LOOKBACK_DAYS):
        end = anchor - i * MS_PER_DAY
        if end - window < times[0]:
            break  # window would start before the data does
        history.append(window_sum(end))
    if len(history) < FUND_MIN_POINTS:
        return None
    history.sort()
    return window_sum(anchor), percentile(history, FUND_PCT_LOW), percentile(history, FUND_PCT_HIGH)


# ---------------------------------------------------------------------------
# Data + the check itself
# ---------------------------------------------------------------------------

class FilterContext:
    """
    One per scan. Caches per symbol, so BTC's candles are fetched once however
    many altcoins fire, and nothing is fetched for coins that did not reset.
    """

    def __init__(self, session: Optional[requests.Session] = None,
                 now_ms: Optional[int] = None, log=print):
        self.session = session or requests.Session()
        self.now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        self.log = log
        self._daily: Dict[str, Optional[List[Tuple[int, float]]]] = {}
        self._funding: Dict[str, Tuple[Optional[List[Tuple[int, float]]], Optional[str]]] = {}

    # --- fetchers (overridden in tests) -------------------------------------
    def _get(self, url: str, params: dict):
        response = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}")
        return response.json()

    def daily_closes(self, symbol: str) -> Optional[List[Tuple[int, float]]]:
        """Closed UTC daily bars from Binance spot as (open_ts, close). None if unavailable."""
        if symbol not in self._daily:
            try:
                rows = self._get(BINANCE_KLINES, {"symbol": symbol, "interval": "1d", "limit": 1000})
                closed = [(int(r[0]), float(r[4])) for r in rows
                          if int(r[0]) + MS_PER_DAY <= self.now_ms]
                self._daily[symbol] = closed or None
            except Exception as exc:
                self.log(f"  [filters] {symbol}: no Binance daily data ({type(exc).__name__}: {str(exc)[:80]})")
                self._daily[symbol] = None
        return self._daily[symbol]

    def funding(self, symbol: str) -> Tuple[Optional[List[Tuple[int, float]]], Optional[str]]:
        if symbol not in self._funding:
            self._funding[symbol] = self._fetch_funding(symbol)
        return self._funding[symbol]

    def _fetch_funding(self, symbol):
        try:
            start = self.now_ms - (FUND_LOOKBACK_DAYS + FUND_WINDOW_DAYS + 30) * MS_PER_DAY
            rows: List[Tuple[int, float]] = []
            for _ in range(6):  # 1000 per page; 4h-interval coins need ~3 pages
                batch = self._get(BINANCE_FUNDING, {"symbol": symbol, "startTime": start, "limit": 1000})
                rows += [(int(x["fundingTime"]), float(x["fundingRate"])) for x in batch]
                if len(batch) < 1000:
                    break
                start = int(batch[-1]["fundingTime"]) + 1
            if rows:
                return rows, SOURCE_BINANCE
        except Exception as exc:
            self.log(f"  [filters] {symbol}: Binance funding unavailable ({type(exc).__name__}: "
                     f"{str(exc)[:80]}); trying Bitget")
        try:
            rows = []
            for page in range(1, 11):
                payload = self._get(BITGET_FUNDING, {"symbol": symbol, "productType": "USDT-FUTURES",
                                                     "pageSize": 100, "pageNo": page})
                data = payload.get("data") or []
                if not data:
                    break
                rows += [(int(x["fundingTime"]), float(x["fundingRate"])) for x in data]
            if rows:
                return rows, SOURCE_BITGET
        except Exception as exc:
            self.log(f"  [filters] {symbol}: Bitget funding unavailable ({type(exc).__name__}: {str(exc)[:80]})")
        return None, None

    # --- the check -----------------------------------------------------------
    def check(self, symbol: str, direction: str, is_crypto: bool) -> Verdict:
        long = direction == "bullish"
        verdict = Verdict(passed=True)

        closes = self.daily_closes(symbol)
        ema = ema_tv([c for _, c in closes], TREND_EMA) if closes else None
        if ema is None:
            verdict.unavailable.append("trend")
        elif (closes[-1][1] > ema) != long or closes[-1][1] == ema:
            verdict.blocked_by.append(f"{'below' if long else 'above'} EMA{TREND_EMA}")

        if is_crypto and symbol != BTC_SYMBOL:
            btc = self.daily_closes(BTC_SYMBOL)
            btc_ema = ema_tv([c for _, c in btc], BTC_EMA) if btc else None
            if btc_ema is None:
                verdict.unavailable.append("BTC")
            elif (btc[-1][1] > btc_ema) != long or btc[-1][1] == btc_ema:
                verdict.blocked_by.append(f"BTC {'below' if long else 'above'} EMA{BTC_EMA}")

        settlements, source = self.funding(symbol)
        verdict.funding_source = source
        crowd = funding_crowding(settlements, self.now_ms) if settlements else None
        if crowd is None:
            verdict.unavailable.append("funding")
        else:
            current, low, high = crowd
            if long and current > high:
                verdict.blocked_by.append("funding crowded long")
            elif not long and current < low:
                verdict.blocked_by.append("funding crowded short")

        if verdict.blocked_by:
            verdict.passed = False
        elif verdict.unavailable:
            verdict.passed = None
        return verdict
