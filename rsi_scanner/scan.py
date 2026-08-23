"""
Scan orchestration.

One pass over the universe: fetch, compute, detect. A symbol that fails to
fetch is recorded and skipped -- one delisted or flaky name must never cost
you the other twenty-eight alerts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

import requests

from .bitget import FetchError, daily_closes
from .config import Config
from .rsi import detect_reset, wilder_rsi
from .symbols import NON_CRYPTO


@dataclass
class Signal:
    symbol: str
    direction: str          # "bullish" | "bearish"
    rsi_prev: float
    rsi_now: float
    close: float
    prev_close: float
    bar_ts_ms: int
    is_crypto: bool

    @property
    def pct_change(self) -> float:
        if self.prev_close == 0:
            return 0.0
        return (self.close - self.prev_close) / self.prev_close * 100.0


@dataclass
class ScanResult:
    signals: List[Signal]
    scanned: int
    failures: List[Tuple[str, str]]
    bars_available: Optional[int] = None
    # Timestamp of the last CLOSED daily bar. Carried explicitly so the
    # digest can date itself correctly on a day with no signals -- falling
    # back to 'now' would label the message with tomorrow's date.
    last_bar_ts_ms: Optional[int] = None

    @property
    def bullish(self) -> List[Signal]:
        return [s for s in self.signals if s.direction == "bullish"]

    @property
    def bearish(self) -> List[Signal]:
        return [s for s in self.signals if s.direction == "bearish"]


def scan(config: Config, now_ms: Optional[int] = None, log=print) -> ScanResult:
    signals: List[Signal] = []
    failures: List[Tuple[str, str]] = []
    scanned = 0
    bars_seen: Optional[int] = None
    last_bar_ts: Optional[int] = None

    # One session for the whole run: connection reuse turns ~29 TLS handshakes
    # into one.
    session = requests.Session()
    try:
        for index, symbol in enumerate(config.symbols):
            if index:
                time.sleep(config.request_delay_seconds)
            try:
                bars = daily_closes(
                    symbol, boundary=config.day_boundary, session=session, now_ms=now_ms
                )
            except FetchError as exc:
                failures.append((symbol, str(exc)))
                log(f"  [WARN] {symbol}: {exc}")
                continue

            if len(bars) < config.period + 2:
                failures.append((symbol, f"only {len(bars)} daily bars"))
                log(f"  [WARN] {symbol}: only {len(bars)} daily bars, need {config.period + 2}")
                continue

            scanned += 1
            bars_seen = len(bars) if bars_seen is None else bars_seen
            last_bar_ts = int(bars[-1][0]) if last_bar_ts is None else last_bar_ts

            closes = [b[4] for b in bars]
            series = wilder_rsi(closes, period=config.period)
            direction = detect_reset(series, config.oversold, config.overbought)

            rsi_now, rsi_prev = series[-1], series[-2]
            log(
                f"  {symbol:<12} RSI {rsi_prev:6.2f} -> {rsi_now:6.2f}"
                + (f"   *** {direction.upper()} ***" if direction else "")
            )

            if direction is None:
                continue

            signals.append(
                Signal(
                    symbol=symbol,
                    direction=direction,
                    rsi_prev=float(rsi_prev),
                    rsi_now=float(rsi_now),
                    close=closes[-1],
                    prev_close=closes[-2],
                    bar_ts_ms=int(bars[-1][0]),
                    is_crypto=symbol not in NON_CRYPTO,
                )
            )
    finally:
        session.close()

    return ScanResult(
        signals=signals,
        scanned=scanned,
        failures=failures,
        bars_available=bars_seen,
        last_bar_ts_ms=last_bar_ts,
    )
