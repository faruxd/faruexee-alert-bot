"""
The configured logging sinks, exercised for real.

test_state.py covers the redaction processor in isolation. This file drives the
whole configure_logging() path, because a redaction function that works in a
unit test but is not actually wired into the emitted stream would be worthless.

Note on capture: pytest replaces sys.stdout at session level before our
StreamHandler binds to it, so neither capsys nor capfd can observe what that
handler writes. These tests therefore reach for the real configured handler
object and redirect its stream, which tests the actual production wiring rather
than a stand-in.
"""

from __future__ import annotations

import io
import json
import logging
import logging.handlers
from pathlib import Path

import pytest

from cf_bot.logging_setup import configure_logging, get_logger


@pytest.fixture
def configured(tmp_path: Path):
    log_file = tmp_path / "logs" / "cf_bot.jsonl"
    configure_logging(level="INFO", log_file=log_file, max_bytes=10_000_000, backup_count=2)
    yield log_file
    # Detach handlers so a later test does not keep writing into a deleted tmp dir.
    root = logging.getLogger()
    for handler in list(root.handlers):
        handler.close()
        root.removeHandler(handler)


def _our_handlers() -> list[logging.Handler]:
    """
    Root handlers excluding pytest's own.

    pytest re-attaches LogCaptureHandler after configure_logging runs, and that
    class subclasses StreamHandler, so it must be filtered out by name or it
    would shadow the real stdout handler in the lookups below.
    """
    return [
        h
        for h in logging.getLogger().handlers
        if type(h).__name__ not in ("LogCaptureHandler", "_LiveLoggingNullHandler")
    ]


def _stdout_handler() -> logging.StreamHandler:
    return next(
        h
        for h in _our_handlers()
        if isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.handlers.RotatingFileHandler)
    )


def _file_handler() -> logging.handlers.RotatingFileHandler:
    return next(
        h for h in _our_handlers() if isinstance(h, logging.handlers.RotatingFileHandler)
    )


def _read_lines(log_file: Path) -> list[dict]:
    for handler in logging.getLogger().handlers:
        handler.flush()
    text = log_file.read_text(encoding="utf-8").strip()
    return [json.loads(line) for line in text.splitlines() if line]


# --- wiring ----------------------------------------------------------------


def test_log_directory_is_created(tmp_path: Path, configured):
    assert configured.parent.is_dir()


def test_exactly_two_sinks_are_installed(configured):
    """One stdout, one rotating file. No stray handlers from a library."""
    assert len(_our_handlers()) == 2
    assert _stdout_handler() is not None
    assert _file_handler() is not None


def test_both_sinks_share_one_formatter(configured):
    """
    The two sinks must render identically. If they could drift, the file you
    reconcile against Bitget later would not match what you watched live.
    """
    assert _stdout_handler().formatter is _file_handler().formatter


def test_rotation_is_configured(configured):
    handler = _file_handler()
    assert handler.maxBytes == 10_000_000
    assert handler.backupCount == 2


# --- content ---------------------------------------------------------------


def test_file_sink_receives_json_events(configured):
    get_logger().info("test.event", detail="hello")
    records = _read_lines(configured)
    assert any(r["event"] == "test.event" and r["detail"] == "hello" for r in records)


def test_stdout_sink_receives_json_events(configured):
    buffer = io.StringIO()
    _stdout_handler().setStream(buffer)

    get_logger().info("test.event", detail="hello")

    lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
    assert any(json.loads(line)["event"] == "test.event" for line in lines)


def test_output_has_level_and_utc_timestamp(configured):
    get_logger().warning("test.warned")
    record = next(r for r in _read_lines(configured) if r["event"] == "test.warned")
    assert record["level"] == "warning"
    assert record["timestamp"].endswith("Z")


# --- the guarantee that matters --------------------------------------------


def test_secrets_are_redacted_in_both_real_sinks(configured):
    """End-to-end: a secret handed to a log call reaches neither disk nor stdout."""
    buffer = io.StringIO()
    _stdout_handler().setStream(buffer)

    get_logger().info(
        "request.sent",
        headers={"ACCESS-KEY": "SUPERSECRETKEY", "ACCESS-PASSPHRASE": "SUPERSECRETPASS"},
        api_key="SUPERSECRETKEY",
        symbol="BTC/USDT:USDT",
    )

    for handler in logging.getLogger().handlers:
        handler.flush()

    on_disk = configured.read_text(encoding="utf-8")
    on_stdout = buffer.getvalue()

    for sink_name, sink in (("file", on_disk), ("stdout", on_stdout)):
        assert "SUPERSECRETKEY" not in sink, f"secret leaked to {sink_name}"
        assert "SUPERSECRETPASS" not in sink, f"secret leaked to {sink_name}"
        assert "<redacted>" in sink
        assert "BTC/USDT:USDT" in sink  # non-secret context survives


def test_exceptions_are_logged_with_a_traceback_not_swallowed(configured):
    try:
        raise ValueError("simulated failure")
    except ValueError:
        get_logger().exception("fatal.unhandled")

    record = next(r for r in _read_lines(configured) if r["event"] == "fatal.unhandled")
    assert "ValueError: simulated failure" in record["exception"]
    assert "Traceback" in record["exception"]


def test_ccxt_logger_is_pinned_to_warning(configured):
    """ccxt's DEBUG stream can contain signed request headers. It stays quiet."""
    assert logging.getLogger("ccxt").level == logging.WARNING
