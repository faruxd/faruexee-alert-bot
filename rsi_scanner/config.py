"""
Configuration, entirely from the environment.

Nothing here has a secret default and nothing is read from a file. The one
credential -- the Discord webhook -- is never logged, not even truncated.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

from .rsi import DEFAULT_OVERBOUGHT, DEFAULT_OVERSOLD, DEFAULT_PERIOD
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

        return cls(
            webhook_url=webhook,
            symbols=symbols,
            period=period,
            oversold=oversold,
            overbought=overbought,
            day_boundary=boundary,
            post_when_empty=_flag(env.get("POST_WHEN_EMPTY")),
            dry_run=_flag(env.get("RSI_DRY_RUN")),
        )


def _flag(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")
