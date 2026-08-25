"""
Configuration, entirely from the environment.

Nothing here has a secret default and nothing is read from a file. The one
credential -- the Discord webhook -- is never logged, not even truncated.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

from .rsi import (
    DEFAULT_BIAS_MIDLINE,
    DEFAULT_OVERBOUGHT,
    DEFAULT_OVERSOLD,
    DEFAULT_PERIOD,
)
from .symbols import default_universe

WEBHOOK_ENV_VAR = "DISCORD_RSI_WEBHOOK"


@dataclass
class Config:
    webhook_url: Optional[str] = None
    symbols: List[str] = field(default_factory=default_universe)
    period: int = DEFAULT_PERIOD
    oversold: float = DEFAULT_OVERSOLD
    overbought: float = DEFAULT_OVERBOUGHT
    day_boundary: str = "utc"
    timeframes: List[str] = field(default_factory=lambda: ["1D", "4H"])
    bias_midline: float = DEFAULT_BIAS_MIDLINE
    # Report 4H resets that FIGHT the daily bias, in their own compact
    # section. On by default. Turning it off roughly cuts total volume from
    # ~20 alerts/day to ~5 -- see the README table before changing it.
    alert_4h_unconfirmed: bool = True
    # A bar older than this is a re-read, not news. See scan._is_fresh.
    max_bar_age_minutes: float = 90.0
    post_when_empty: bool = False
    request_delay_seconds: float = 0.12
    dry_run: bool = False

    @classmethod
    def from_env(cls, env=None) -> "Config":
        env = os.environ if env is None else env

        raw_webhook = (env.get(WEBHOOK_ENV_VAR) or "").strip()
        webhook = raw_webhook or None
        if webhook and not webhook.startswith("https://"):
            # Rejected rather than used. The URL is not echoed back.
            raise ValueError(f"{WEBHOOK_ENV_VAR} must be an https URL")

        raw_symbols = (env.get("RSI_SYMBOLS") or "").strip()
        symbols = (
            [s.strip().upper() for s in raw_symbols.split(",") if s.strip()]
            if raw_symbols
            else default_universe()
        )

        boundary = (env.get("DAY_BOUNDARY") or "utc").strip().lower()
        if boundary not in ("utc", "exchange"):
            raise ValueError("DAY_BOUNDARY must be 'utc' or 'exchange'")

        oversold = float(env.get("RSI_OVERSOLD") or DEFAULT_OVERSOLD)
        overbought = float(env.get("RSI_OVERBOUGHT") or DEFAULT_OVERBOUGHT)
        if not 0 < oversold < overbought < 100:
            raise ValueError(
                f"need 0 < RSI_OVERSOLD ({oversold}) < RSI_OVERBOUGHT ({overbought}) < 100"
            )

        period = int(env.get("RSI_PERIOD") or DEFAULT_PERIOD)
        if period < 2:
            raise ValueError("RSI_PERIOD must be >= 2")

        raw_tfs = (env.get("RSI_TIMEFRAMES") or "1D,4H").strip()
        timeframes = [t.strip().upper() for t in raw_tfs.split(",") if t.strip()]
        for tf in timeframes:
            if tf not in ("1D", "4H"):
                raise ValueError(f"unsupported timeframe {tf!r}; expected 1D or 4H")
        if "1D" not in timeframes:
            # The 4H filter reads the daily bias, so the daily series is
            # fetched either way. Excluding 1D would only hide its signals.
            raise ValueError("1D cannot be removed; the 4H filter depends on it")

        max_age = float(env.get("MAX_BAR_AGE_MINUTES") or 90.0)
        if max_age <= 0:
            raise ValueError("MAX_BAR_AGE_MINUTES must be positive")

        midline = float(env.get("BIAS_MIDLINE") or DEFAULT_BIAS_MIDLINE)
        if not 0 < midline < 100:
            raise ValueError("BIAS_MIDLINE must be between 0 and 100")

        return cls(
            webhook_url=webhook,
            symbols=symbols,
            period=period,
            oversold=oversold,
            overbought=overbought,
            day_boundary=boundary,
            timeframes=timeframes,
            bias_midline=midline,
            alert_4h_unconfirmed=_flag(env.get("ALERT_4H_UNCONFIRMED"), default=True),
            max_bar_age_minutes=max_age,
            post_when_empty=_flag(env.get("POST_WHEN_EMPTY")),
            dry_run=_flag(env.get("RSI_DRY_RUN")),
        )


def _flag(value: Optional[str], default: bool = False) -> bool:
    """
    Unset falls back to `default`; anything set is parsed strictly.

    A flag that defaults ON cannot treat "unset" and "false" alike, or it
    would be impossible to turn off.
    """
    if value is None or not value.strip():
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")
