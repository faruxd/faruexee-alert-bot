"""
Configuration loading and validation.

Three separate concerns, kept separate so each is independently testable:

  load_settings()    -- config.yaml -> validated pydantic model (no secrets)
  resolve_mode()     -- the demo/live gate (environment only)
  load_credentials() -- API keys (environment only, never logged)

Credentials are deliberately NOT part of the settings model. That means the
settings object can be dumped wholesale into a log line and it is structurally
impossible for a key to ride along.

Everything here raises on bad input. Nothing here has a fallback that makes the
bot more permissive than the operator asked for.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping, Optional

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from cf_bot.constants import (
    LIVE_CONFIRMATION_ENV,
    LIVE_CONFIRMATION_VALUE,
    MAX_RISK_PCT,
    MODE_DEMO,
    MODE_LIVE,
    VALID_MODES,
)


class ConfigError(Exception):
    """Any configuration problem. Always fatal at startup."""


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Credentials:
    """
    Bitget requires all three of key, secret and passphrase.

    __repr__ and __str__ are overridden so that an accidental f-string, a
    traceback frame dump, or a structlog fallback repr can never print the
    real values.
    """

    api_key: str
    api_secret: str
    passphrase: str

    def __repr__(self) -> str:
        return "Credentials(api_key='<redacted>', api_secret='<redacted>', passphrase='<redacted>')"

    __str__ = __repr__


def load_credentials(env: Optional[Mapping[str, str]] = None) -> Credentials:
    """Read the three Bitget credentials from the environment. Never from config.yaml."""
    env = os.environ if env is None else env

    required = {
        "BITGET_API_KEY": "api_key",
        "BITGET_API_SECRET": "api_secret",
        "BITGET_API_PASSPHRASE": "passphrase",
    }

    values = {}
    missing = []
    for env_name, field in required.items():
        raw = env.get(env_name)
        if raw is None or raw.strip() == "":
            missing.append(env_name)
        else:
            values[field] = raw.strip()

    if missing:
        raise ConfigError(
            "missing required credential environment variable(s): "
            + ", ".join(sorted(missing))
            + ". Credentials are read from the environment only -- never put them "
            "in config.yaml or in source."
        )

    return Credentials(**values)


# ---------------------------------------------------------------------------
# Mode gate
# ---------------------------------------------------------------------------


def resolve_mode(env: Optional[Mapping[str, str]] = None) -> str:
    """
    Decide whether we are in demo or live.

    Rules, in order:
      - MODE unset               -> demo
      - MODE not in {demo,live}  -> raise (never silently fall back)
      - MODE=live                -> requires I_UNDERSTAND_THIS_TRADES_REAL_MONEY=yes

    `live` is never a fallback and never the result of an error path.
    """
    env = os.environ if env is None else env

    raw = env.get("MODE")
    if raw is None or raw.strip() == "":
        return MODE_DEMO

    mode = raw.strip().lower()
    if mode not in VALID_MODES:
        raise ConfigError(
            f"MODE={raw!r} is not valid. MODE must be exactly one of {list(VALID_MODES)}. "
            "Refusing to guess."
        )

    if mode == MODE_LIVE:
        confirmation = env.get(LIVE_CONFIRMATION_ENV)
        if confirmation != LIVE_CONFIRMATION_VALUE:
            raise ConfigError(
                f"MODE=live requires {LIVE_CONFIRMATION_ENV}={LIVE_CONFIRMATION_VALUE!r} "
                f"(exact, lowercase). Got {confirmation!r}. Refusing to trade real money."
            )

    return mode


# ---------------------------------------------------------------------------
# config.yaml
# ---------------------------------------------------------------------------

# Timeframes the bot will run on, in milliseconds. The strategy is bar-count
# based and so is timeframe-agnostic; this list exists to reject typos and to
# validate the signal/trend relationship.
SUPPORTED_TIMEFRAMES = {
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
}

_STRICT = ConfigDict(extra="forbid", frozen=True)
# extra="forbid" matters more than it looks: it turns a typo'd config key into a
# startup crash instead of a silently ignored setting the operator believes is
# in effect.


def _coerce_decimal(value: object) -> Decimal:
    """Convert via str() so a YAML float never carries binary-float error into Decimal."""
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, ValueError) as exc:
        raise ValueError(f"{value!r} is not a valid decimal number") from exc


class ExchangeSettings(BaseModel):
    model_config = _STRICT

    symbols: list[str] = Field(
        description="ccxt unified symbols to scan, e.g. ['BTC/USDT:USDT', ...]"
    )
    # Signal timeframe: the EMA cross is evaluated on closed bars of this size.
    timeframe: str = "15m"
    # Trend timeframe: the higher-timeframe EMA that sets direction. Must be a
    # whole multiple of the signal timeframe, or the two series do not line up
    # on bar boundaries and the filter reads a candle that is still forming.
    trend_timeframe: str = "30m"
    product_type: str = "USDT-FUTURES"
    margin_coin: str = "USDT"

    @field_validator("symbols")
    @classmethod
    def _symbols_are_usdt_perps(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("at least one symbol is required")

        cleaned = [s.strip() for s in v]
        for symbol in cleaned:
            if ":" not in symbol or "/" not in symbol:
                raise ValueError(
                    f"symbol {symbol!r} does not look like a ccxt unified swap symbol. "
                    "Expected the form BASE/QUOTE:SETTLE, e.g. 'BTC/USDT:USDT'."
                )

        duplicates = {s for s in cleaned if cleaned.count(s) > 1}
        if duplicates:
            raise ValueError(f"duplicate symbols in config: {sorted(duplicates)}")

        return cleaned

    @field_validator("timeframe", "trend_timeframe")
    @classmethod
    def _known_timeframe(cls, v: str) -> str:
        v = v.strip()
        if v not in SUPPORTED_TIMEFRAMES:
            raise ValueError(
                f"timeframe {v!r} is not supported. Choose from "
                f"{sorted(SUPPORTED_TIMEFRAMES, key=lambda t: SUPPORTED_TIMEFRAMES[t])}."
            )
        return v

    @model_validator(mode="after")
    def _trend_is_a_whole_multiple_of_signal(self) -> "ExchangeSettings":
        signal = SUPPORTED_TIMEFRAMES[self.timeframe]
        trend = SUPPORTED_TIMEFRAMES[self.trend_timeframe]
        if trend <= signal:
            raise ValueError(
                f"trend_timeframe ({self.trend_timeframe}) must be LONGER than "
                f"timeframe ({self.timeframe}); it is the higher-timeframe filter."
            )
        if trend % signal != 0:
            raise ValueError(
                f"trend_timeframe ({self.trend_timeframe}) must be a whole multiple "
                f"of timeframe ({self.timeframe}), or their bars do not share "
                "boundaries and the trend filter reads a partly formed candle."
            )
        return self


class RiskSettings(BaseModel):
    model_config = _STRICT

    # PERCENT of equity per trade. 1.0 means 1%. Same unit as MAX_RISK_PCT.
    risk_pct: Decimal

    @field_validator("risk_pct", mode="before")
    @classmethod
    def _to_decimal(cls, v: object) -> Decimal:
        return _coerce_decimal(v)

    @field_validator("risk_pct")
    @classmethod
    def _within_hard_ceiling(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError(f"risk_pct must be greater than zero, got {v}")
        if v > MAX_RISK_PCT:
            raise ValueError(
                f"risk_pct={v}% exceeds the hard ceiling MAX_RISK_PCT={MAX_RISK_PCT}%. "
                "This ceiling is a source-code constant in cf_bot/constants.py and is "
                "deliberately not configurable. Refusing to start."
            )
        return v


class RuntimeSettings(BaseModel):
    model_config = _STRICT

    loop_interval_seconds: float = 15.0
    heartbeat_seconds: float = 300.0
    kill_file: str = "KILL"
    # How long a scalper entry rests passively before falling back to market.
    # Longer = more maker fills = cheaper, but more missed moves.
    entry_limit_timeout_seconds: float = 25.0

    @field_validator("entry_limit_timeout_seconds")
    @classmethod
    def _sane_entry_timeout(cls, v: float) -> float:
        if not (0.0 <= v <= 120.0):
            raise ValueError(
                f"entry_limit_timeout_seconds={v} is outside the sane range [0, 120]. "
                "The loop is blocked for this long during an entry."
            )
        return v

    @field_validator("loop_interval_seconds")
    @classmethod
    def _sane_interval(cls, v: float) -> float:
        if not (1.0 <= v <= 300.0):
            raise ValueError(
                f"loop_interval_seconds={v} is outside the sane range [1, 300]"
            )
        return v


class LoggingSettings(BaseModel):
    model_config = _STRICT

    level: str = "INFO"
    file: str = "logs/cf_bot.jsonl"
    max_bytes: int = 50_000_000
    backup_count: int = 5

    @field_validator("level")
    @classmethod
    def _known_level(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ValueError(f"unknown log level {v!r}")
        return v


STRATEGY_FORCED_FLOW = "forced_flow"
STRATEGY_EMA_SCALPER = "ema_scalper"
VALID_STRATEGIES = (STRATEGY_FORCED_FLOW, STRATEGY_EMA_SCALPER)


class StrategySettings(BaseModel):
    """
    Which strategy runs, and its parameters.

    Both strategies' parameters live here with defaults, so a config only needs
    to specify the ones for the strategy it selects. `extra="forbid"` still
    catches typos in either set.
    """

    model_config = _STRICT

    name: str = STRATEGY_FORCED_FLOW

    # --- forced_flow: displacement threshold, stop distance, percentile floor
    k: Decimal = Decimal("2.5")
    s: Decimal = Decimal("1.25")
    p: Decimal = Decimal("30")

    # --- ema_scalper: 15m trend filter, 5m cross trigger, ATR stop, R target
    ema_fast: int = 9
    ema_slow: int = 21
    ema_trend: int = 50
    atr_mult: Decimal = Decimal("1.5")
    target_r: Decimal = Decimal("2.0")

    @field_validator("name")
    @classmethod
    def _known_strategy(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in VALID_STRATEGIES:
            raise ValueError(
                f"strategy.name={v!r} is not valid. Must be one of {list(VALID_STRATEGIES)}."
            )
        return v

    @field_validator("k", "s", "p", "atr_mult", "target_r", mode="before")
    @classmethod
    def _to_decimal(cls, v: object) -> Decimal:
        return _coerce_decimal(v)

    @field_validator("k", "s", "atr_mult", "target_r")
    @classmethod
    def _positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError(f"must be greater than zero, got {v}")
        return v

    @field_validator("p")
    @classmethod
    def _is_a_percentile(cls, v: Decimal) -> Decimal:
        if not (0 <= v <= 100):
            raise ValueError(f"p must be a percentile in [0, 100], got {v}")
        return v

    @field_validator("ema_fast", "ema_slow", "ema_trend")
    @classmethod
    def _sane_ema_period(cls, v: int) -> int:
        if not (2 <= v <= 400):
            raise ValueError(f"EMA period {v} is outside the sane range [2, 400]")
        return v

    @model_validator(mode="after")
    def _fast_is_faster_than_slow(self) -> "StrategySettings":
        if self.ema_slow <= self.ema_fast:
            raise ValueError(
                f"ema_slow ({self.ema_slow}) must be greater than ema_fast "
                f"({self.ema_fast}), or the cross has no meaning"
            )
        return self

    @property
    def is_scalper(self) -> bool:
        return self.name == STRATEGY_EMA_SCALPER


class Settings(BaseModel):
    """The whole of config.yaml. Contains no secrets, so it is safe to log."""

    model_config = _STRICT

    exchange: ExchangeSettings
    risk: RiskSettings
    strategy: StrategySettings = StrategySettings()
    runtime: RuntimeSettings = RuntimeSettings()
    logging: LoggingSettings = LoggingSettings()


def load_settings(path: Path) -> Settings:
    """Parse and validate config.yaml. Raises ConfigError on any problem."""
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"could not read config file {path}: {exc}") from exc

    try:
        parsed = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"config file {path} is not valid YAML: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ConfigError(
            f"config file {path} must contain a YAML mapping at the top level, "
            f"got {type(parsed).__name__}"
        )

    try:
        return Settings(**parsed)
    except ValidationError as exc:
        raise ConfigError(f"config file {path} failed validation:\n{exc}") from exc


# ---------------------------------------------------------------------------
# Composite
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AppConfig:
    """Everything the bot needs to run, except credentials."""

    settings: Settings
    mode: str
    working_dir: Path

    @property
    def kill_file(self) -> Path:
        return self.working_dir / self.settings.runtime.kill_file

    @property
    def log_file(self) -> Path:
        return self.working_dir / self.settings.logging.file

    @property
    def is_live(self) -> bool:
        return self.mode == MODE_LIVE

    def log_payload(self) -> dict:
        """Safe-to-log rendering. Cannot contain credentials -- they are not in here."""
        return {
            "mode": self.mode,
            "symbols": list(self.settings.exchange.symbols),
            "timeframe": self.settings.exchange.timeframe,
            "trend_timeframe": self.settings.exchange.trend_timeframe,
            "product_type": self.settings.exchange.product_type,
            "margin_coin": self.settings.exchange.margin_coin,
            "risk_pct": f"{self.settings.risk.risk_pct}%",
            "max_risk_pct_ceiling": f"{MAX_RISK_PCT}%",
            "strategy": self.settings.strategy.name,
            **(
                {
                    "ema_fast": self.settings.strategy.ema_fast,
                    "ema_slow": self.settings.strategy.ema_slow,
                    "ema_trend": self.settings.strategy.ema_trend,
                    "atr_mult": str(self.settings.strategy.atr_mult),
                    "target_r": str(self.settings.strategy.target_r),
                }
                if self.settings.strategy.is_scalper
                else {
                    "strategy_k": str(self.settings.strategy.k),
                    "strategy_s": str(self.settings.strategy.s),
                    "strategy_p": str(self.settings.strategy.p),
                }
            ),
            "loop_interval_seconds": self.settings.runtime.loop_interval_seconds,
            "kill_file": str(self.kill_file),
            "log_file": str(self.log_file),
            "working_dir": str(self.working_dir),
        }


def load_app_config(
    config_path: Path,
    working_dir: Path,
    env: Optional[Mapping[str, str]] = None,
) -> AppConfig:
    """Load settings and resolve the mode gate. Does not touch credentials."""
    settings = load_settings(config_path)
    mode = resolve_mode(env)
    return AppConfig(settings=settings, mode=mode, working_dir=working_dir)
