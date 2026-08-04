"""
Structured JSON logging to stdout AND to a rotating file.

Two independent sinks on purpose: stdout is what systemd/journald captures and
what you watch live; the file is what survives a journald rotation and what you
reconcile against the Bitget UI weeks later.

This module also owns secret redaction. Redaction is implemented as a structlog
processor that runs on EVERY event before rendering, so there is a single choke
point rather than a discipline that each call site has to remember.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any, MutableMapping

import structlog

# Keys whose values are replaced with "<redacted>" wherever they appear in an
# event dict, at any nesting depth. Matching is case-insensitive and on
# substrings, because exchange payloads are inconsistent about naming
# (apiKey / api_key / ACCESS-KEY / passphrase / sign ...).
_SECRET_KEY_SUBSTRINGS = (
    "apikey",
    "api_key",
    "secret",
    "passphrase",
    "password",
    "signature",
    "sign",
    "token",
    "authorization",
    "access-key",
    "access_key",
    # A Discord webhook URL is a credential: anyone holding it can post to the
    # channel. It must never reach a log line.
    "webhook",
)

_REDACTED = "<redacted>"

# Keys that must NEVER be redacted despite matching a substring above.
#
# "signal" contains "sign", so the OS signal number in shutdown logs was being
# scrubbed -- you could not tell SIGTERM from SIGINT. Over-redaction is much
# safer than under-redaction, but it still destroys diagnostics, so known-safe
# keys are listed explicitly rather than by loosening the substring rules.
_NEVER_REDACT = frozenset(
    {
        "signal",
        "signal_bar_ts",
        "signals",
        "design",
        "assigned",
    }
)


def _looks_secret(key: str) -> bool:
    lowered = key.lower()
    if lowered in _NEVER_REDACT:
        return False
    return any(needle in lowered for needle in _SECRET_KEY_SUBSTRINGS)


def _redact(value: Any, depth: int = 0) -> Any:
    """Recursively redact secret-looking keys. Depth-capped to avoid pathological nesting."""
    if depth > 6:
        return value
    if isinstance(value, MutableMapping):
        return {
            k: (_REDACTED if _looks_secret(str(k)) else _redact(v, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(_redact(v, depth + 1) for v in value)
    return value


def redact_secrets(_logger, _method_name, event_dict):
    """structlog processor: scrub credentials from every event before it is rendered."""
    return _redact(event_dict)


def configure_logging(
    level: str,
    log_file: Path,
    max_bytes: int = 50_000_000,
    backup_count: int = 5,
) -> None:
    """
    Wire structlog + stdlib logging. Idempotent enough to call once at startup.

    Both handlers emit the same JSON lines. If the log directory cannot be
    created we do NOT silently continue with stdout only -- a trading bot whose
    audit trail is missing is a trading bot you cannot reconcile, so this raises.
    """
    log_file.parent.mkdir(parents=True, exist_ok=True)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact_secrets,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=shared_processors
        + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(sort_keys=True),
        ],
        foreign_pre_chain=shared_processors,
    )

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)

    file_handler = logging.handlers.RotatingFileHandler(
        filename=str(log_file),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    # Replace any handlers a library may have installed, so we cannot end up
    # double-logging or emitting unredacted plain-text lines.
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(stdout_handler)
    root.addHandler(file_handler)
    root.setLevel(getattr(logging, level.upper()))

    # ccxt is chatty at DEBUG and its request logs can contain signed headers.
    # Keep it at WARNING regardless of our level.
    logging.getLogger("ccxt").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def get_logger(name: str = "cf_bot"):
    return structlog.get_logger(name)
