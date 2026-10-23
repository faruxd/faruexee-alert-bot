"""
Scan orchestration.

Two timeframes with different rules:

  1D  reported on its own merits.
  4H  reported either way, but SPLIT by whether it agrees with the daily
      bias. A 4H bullish reset inside a bullish daily is a dip being bought
      in an uptrend; the same reset inside a bearish daily is a bounce in a
      downtrend, and acting on it is how you end up long into a falling
      market. Both are shown -- the agreeing ones prominently, the rest
      compactly -- so the distinction stays visible instead of being decided
      for the reader.

      Volume is the cost: measured over 80 days across this universe, 4H
      resets run ~17.6/day unfiltered against ~2.1/day for the agreeing
      subset. Set ALERT_4H_UNCONFIRMED=false to go back to agreeing-only.

The daily series is fetched on EVERY run regardless of timeframe, because the
4H filter needs it. That is 2 requests per symbol per run, not 1.

Every reset that gets this far then has to pass the trade filters in
filters.py (200-day EMA trend, BTC 50-day EMA for altcoins, funding
crowding). A reset the filters block is not reported at all -- only counted.
One the filters cannot check is reported and marked unverified. Data for
the filters is fetched only for symbols that actually reset.

A symbol that fails to fetch is recorded and skipped -- one delisted or flaky
name must never cost you the other thirty-three.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import requests

from .bitget import FetchError, bar_age_minutes, bars_for
from .config import Config
from .filters import SOURCE_BITGET, FilterContext
from .rsi import bias, detect_reset, wilder_rsi
from .symbols import NON_CRYPTO


@dataclass
class Signal:
    symbol: str
    direction: str            # "bullish" | "bearish"
    timeframe: str            # "1D" | "4H"
    rsi_prev: float
    rsi_now: float
    close: float
    prev_close: float
    bar_ts_ms: int
    is_crypto: bool
    daily_rsi: Optional[float] = None   # context; set on 4H signals
    # 4H only. True when the reset agrees with the daily bias, False when it
    # fights it. 1D signals are always True -- there is no higher timeframe
    # for them to agree with.
    daily_confirmed: bool = True
    # Set when a trade filter could not run (no data for this symbol). The
    # alert is still sent, marked, rather than dropped silently.
    unverified: Optional[str] = None

    @property
    def pct_change(self) -> float:
        if self.prev_close == 0:
            return 0.0
        return (self.close - self.prev_close) / self.prev_close * 100.0


@dataclass
class ScanResult:
    signals: List[Signal] = field(default_factory=list)
    scanned: int = 0
    failures: List[Tuple[str, str]] = field(default_factory=list)
    bars_available: Optional[int] = None
    last_bar_ts_ms: Optional[int] = None
    # 4H resets rejected because they fought the daily. Surfaced so a silent
    # filter cannot quietly swallow everything without anyone noticing.
    suppressed: int = 0
    # Resets blocked by the trade filters. Hidden from the digest, but counted
    # in its footer so a filter that blocks everything is still noticeable.
    filtered: int = 0
    # True if any funding check fell back to Bitget's 90 days.
    funding_fallback: bool = False
    # Which timeframes actually had a freshly closed bar this run.
    reported: List[str] = field(default_factory=list)
    stale: Dict[str, float] = field(default_factory=dict)
    # Close time of the newest bar actually reported this run. The digest
    # dates itself from this, not from the daily -- on a 04:00 run only the
    # 4H bar is news, and stamping it with yesterday's date reads as stale.
    reported_bar_ts_ms: Optional[int] = None

    @property
    def bullish(self) -> List[Signal]:
        return [s for s in self.signals if s.direction == "bullish"]

    @property
    def bearish(self) -> List[Signal]:
        return [s for s in self.signals if s.direction == "bearish"]

    def for_tf(self, timeframe: str) -> List[Signal]:
        return [s for s in self.signals if s.timeframe == timeframe]

    def confirmed_4h(self) -> List[Signal]:
        return [s for s in self.signals
                if s.timeframe == "4H" and s.daily_confirmed]

    def unconfirmed_4h(self) -> List[Signal]:
        return [s for s in self.signals
                if s.timeframe == "4H" and not s.daily_confirmed]


def scan(config: Config, now_ms: Optional[int] = None, log=print,
         filters: Optional[FilterContext] = None) -> ScanResult:
    result = ScanResult()
    session = requests.Session()
    if filters is None and config.filters_enabled:
        filters = FilterContext(session=session, now_ms=now_ms, log=log)

    try:
        for index, symbol in enumerate(config.symbols):
            if index:
                time.sleep(config.request_delay_seconds)
            try:
                _scan_symbol(symbol, config, result, session, now_ms, log, filters)
            except FetchError as exc:
                result.failures.append((symbol, str(exc)))
                log(f"  [WARN] {symbol}: {exc}")
    finally:
        session.close()

    return result


def _scan_symbol(symbol, config, result, session, now_ms, log, filters=None) -> None:
    is_crypto = symbol not in NON_CRYPTO

    # --- daily: needed every run, both as a signal and as the 4H filter -----
    daily_bars = bars_for(symbol, "1D", config.day_boundary, session, now_ms)
    if len(daily_bars) < config.period + 2:
        result.failures.append((symbol, f"only {len(daily_bars)} daily bars"))
        log(f"  [WARN] {symbol}: only {len(daily_bars)} daily bars")
        return

    result.scanned += 1
    if result.bars_available is None:
        result.bars_available = len(daily_bars)
        result.last_bar_ts_ms = int(daily_bars[-1][0])

    daily_closes_ = [b[4] for b in daily_bars]
    daily_series = wilder_rsi(daily_closes_, config.period)
    daily_rsi = daily_series[-1]
    daily_bias = bias(daily_rsi, config.bias_midline)

    line = f"  {symbol:<12} 1D {daily_series[-2]:6.2f} -> {daily_rsi:6.2f}"

    # --- 1D signal, only if the daily bar is genuinely fresh ----------------
    if _is_fresh(daily_bars[-1][0], "1D", config, result, now_ms):
        direction = detect_reset(daily_series, config.oversold, config.overbought)
        if direction:
            sig = _mk(symbol, direction, "1D", daily_series, daily_closes_,
                      daily_bars, is_crypto)
            keep, note = _apply_filters(sig, filters, result)
            line += f"   *** 1D {direction.upper()} ({note}) ***"
            if keep:
                result.signals.append(sig)

    # --- 4H signal, gated on the daily -------------------------------------
    if "4H" in config.timeframes:
        h4_bars = bars_for(symbol, "4H", config.day_boundary, session, now_ms)
        if len(h4_bars) >= config.period + 2:
            h4_closes = [b[4] for b in h4_bars]
            h4_series = wilder_rsi(h4_closes, config.period)
            line += f"  |  4H {h4_series[-2]:6.2f} -> {h4_series[-1]:6.2f}"

            if _is_fresh(h4_bars[-1][0], "4H", config, result, now_ms):
                direction = detect_reset(h4_series, config.oversold, config.overbought)
                if direction:
                    confirmed = direction == daily_bias
                    if confirmed or config.alert_4h_unconfirmed:
                        tag = "1D agrees" if confirmed else f"against 1D {daily_bias}"
                        sig = _mk(symbol, direction, "4H", h4_series, h4_closes,
                                  h4_bars, is_crypto)
                        sig.daily_rsi = float(daily_rsi) if daily_rsi is not None else None
                        sig.daily_confirmed = confirmed
                        keep, note = _apply_filters(sig, filters, result)
                        line += f"   *** 4H {direction.upper()} ({tag}; {note}) ***"
                        if keep:
                            result.signals.append(sig)
                    else:
                        result.suppressed += 1
                        line += f"   (4H {direction} suppressed: 1D is {daily_bias})"

    log(line)


def _apply_filters(sig: Signal, filters: Optional[FilterContext],
                   result: ScanResult) -> Tuple[bool, str]:
    """(keep the alert?, note for the console log)."""
    if filters is None:
        return True, "filters off"
    verdict = filters.check(sig.symbol, sig.direction, sig.is_crypto)
    if verdict.funding_source == SOURCE_BITGET:
        result.funding_fallback = True
    if verdict.passed is False:
        result.filtered += 1
        return False, "FILTERED: " + ", ".join(verdict.blocked_by)
    if verdict.passed is None:
        sig.unverified = ", ".join(verdict.unavailable)
        return True, f"unverified: no {sig.unverified} data"
    return True, "filters pass"


def _mk(symbol, direction, timeframe, series, closes, bars, is_crypto) -> Signal:
    return Signal(
        symbol=symbol,
        direction=direction,
        timeframe=timeframe,
        rsi_prev=float(series[-2]),
        rsi_now=float(series[-1]),
        close=closes[-1],
        prev_close=closes[-2],
        bar_ts_ms=int(bars[-1][0]),
        is_crypto=is_crypto,
    )


def _is_fresh(bar_ts, timeframe, config, result, now_ms) -> bool:
    """
    Has this bar closed recently enough to be worth reporting?

    THIS IS THE DUPLICATE GUARD. Without it, a scheduler firing every ten
    minutes re-evaluates the same closed bar and re-posts the same digest all
    day -- which is exactly what happened in production. The scan is stateless
    by design, so it cannot remember what it already sent; instead it refuses
    to report a bar that closed long ago, because a fresh signal and a
    stale re-read are distinguishable from the clock alone.

    It BOUNDS the damage rather than eliminating it: a ten-minute cron still
    gets several posts inside the window. The actual fix is the schedule. Set
    MAX_BAR_AGE_MINUTES tighter on a reliable scheduler like Render Cron;
    keep it loose on GitHub Actions, whose runs are routinely 5-30 minutes
    late and would otherwise be dropped silently.
    """
    age = bar_age_minutes(int(bar_ts), timeframe, now_ms)
    if age <= config.max_bar_age_minutes:
        if timeframe not in result.reported:
            result.reported.append(timeframe)
        ts = int(bar_ts)
        if result.reported_bar_ts_ms is None or ts > result.reported_bar_ts_ms:
            result.reported_bar_ts_ms = ts
        return True
    result.stale[timeframe] = age
    return False
