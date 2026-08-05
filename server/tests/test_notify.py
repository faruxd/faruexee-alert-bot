"""
Discord notifications.

The formatting tests are the least important thing here. What matters is that a
notification failure cannot disturb trading, that it cannot flood the channel,
and that the webhook URL never reaches a log line.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from cf_bot.logging_setup import redact_secrets
from cf_bot.notify import (
    MAX_MESSAGES_PER_HOUR,
    WEBHOOK_ENV_VAR,
    DiscordNotifier,
    critical_message,
    position_closed_message,
    position_opened_message,
)
from cf_bot.state import Position


class FakeLog:
    def __init__(self):
        self.events = []

    def info(self, event, **kw):
        self.events.append((event, kw))

    def warning(self, event, **kw):
        self.events.append((event, kw))

    def error(self, event, **kw):
        self.events.append((event, kw))

    def names(self):
        return [e for e, _ in self.events]


@pytest.fixture
def log():
    return FakeLog()


def make_position(side="long", symbol="BTC/USDT:USDT") -> Position:
    return Position(
        symbol=symbol,
        side=side,
        contracts=Decimal("0.001"),
        entry_price=Decimal("64000.5"),
        mark_price=Decimal("64100"),
        liquidation_price=Decimal("58000"),
        unrealized_pnl=Decimal("0.1"),
        margin_mode="isolated",
        leverage=Decimal("10"),
    )


# --- configuration ---------------------------------------------------------


def test_absent_webhook_disables_notifications(log):
    notifier = DiscordNotifier.from_env(log, env={})
    assert notifier.enabled is False


def test_blank_webhook_disables_notifications(log):
    notifier = DiscordNotifier.from_env(log, env={WEBHOOK_ENV_VAR: "   "})
    assert notifier.enabled is False


def test_non_https_webhook_is_rejected(log):
    """A plaintext webhook would leak the credential over the wire."""
    notifier = DiscordNotifier.from_env(log, env={WEBHOOK_ENV_VAR: "http://example.com/hook"})
    assert notifier.enabled is False
    assert "notify.webhook_rejected" in log.names()


def test_valid_webhook_enables_notifications(log):
    notifier = DiscordNotifier.from_env(
        log, env={WEBHOOK_ENV_VAR: "https://discord.com/api/webhooks/1/abc"}
    )
    assert notifier.enabled is True


def test_configuration_never_logs_the_url(log):
    """The URL is a credential -- anyone holding it can post to the channel."""
    secret = "https://discord.com/api/webhooks/123456/SUPERSECRETTOKEN"
    DiscordNotifier.from_env(log, env={WEBHOOK_ENV_VAR: secret})
    assert "SUPERSECRETTOKEN" not in json.dumps(log.events)


def test_webhook_keys_are_redacted_from_logs():
    out = redact_secrets(None, None, {"webhook_url": "https://discord.com/api/webhooks/x/SECRET"})
    assert out["webhook_url"] == "<redacted>"


# --- disabled is a total no-op ---------------------------------------------


async def test_disabled_notifier_sends_nothing(log):
    notifier = DiscordNotifier.from_env(log, env={})
    await notifier.send("hello")  # must not raise, must not touch the network


# --- failures never reach the caller ---------------------------------------


async def test_a_network_failure_does_not_raise(log, monkeypatch):
    """
    Rule one. Discord being down must never propagate into the trading loop.
    """
    notifier = DiscordNotifier.from_env(
        log, env={WEBHOOK_ENV_VAR: "https://discord.com/api/webhooks/1/abc"}
    )

    import cf_bot.notify as notify_module

    class Boom:
        def __init__(self, *a, **kw):
            raise OSError("network unreachable")

    monkeypatch.setattr(notify_module.asyncio, "get_running_loop", __import__("asyncio").get_running_loop)
    monkeypatch.setitem(
        __import__("sys").modules, "aiohttp", type("m", (), {"ClientTimeout": Boom, "ClientSession": Boom})
    )

    await notifier.send("this will fail")  # must not raise
    assert "notify.send_error" in log.names()


async def test_a_failed_send_does_not_count_against_the_rate_limit(log, monkeypatch):
    notifier = DiscordNotifier.from_env(
        log, env={WEBHOOK_ENV_VAR: "https://discord.com/api/webhooks/1/abc"}
    )

    class Boom:
        def __init__(self, *a, **kw):
            raise OSError("down")

    monkeypatch.setitem(
        __import__("sys").modules, "aiohttp", type("m", (), {"ClientTimeout": Boom, "ClientSession": Boom})
    )

    for i in range(5):
        await notifier.send(f"msg {i}")

    assert notifier._sent_timestamps == []


# --- flood protection ------------------------------------------------------


@pytest.fixture
def captured_posts(monkeypatch):
    """
    Replace ONLY the HTTP layer, so the real send() runs.

    Mocking send() itself would test the mock rather than the dedup and
    rate-limit logic, which is the whole point of this file.
    """
    posts: list[dict] = []

    class FakeResponse:
        status = 204

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class FakeSession:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def post(self, url, json=None):
            posts.append({"url": url, "json": json})
            return FakeResponse()

    fake_aiohttp = type(
        "aiohttp",
        (),
        {"ClientTimeout": lambda **kw: None, "ClientSession": FakeSession},
    )
    monkeypatch.setitem(__import__("sys").modules, "aiohttp", fake_aiohttp)
    return posts


async def test_identical_consecutive_messages_are_suppressed(log, captured_posts):
    """
    There is prior art in this repo of a retry loop hammering Discord. An
    identical repeat is almost always a loop, not news.
    """
    notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/1/abc", log=log)

    await notifier.send("same")
    await notifier.send("same")
    await notifier.send("same")

    assert len(captured_posts) == 1


async def test_different_messages_all_go_through(log, captured_posts):
    notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/1/abc", log=log)

    await notifier.send("first")
    await notifier.send("second")

    assert len(captured_posts) == 2


async def test_a_duplicate_can_be_forced_for_critical_alerts(log, captured_posts):
    """A repeated 'unprotected position' alert must still get through."""
    notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/1/abc", log=log)

    await notifier.send("critical", allow_duplicate=True)
    await notifier.send("critical", allow_duplicate=True)

    assert len(captured_posts) == 2


async def test_the_hourly_cap_actually_stops_sending(log, captured_posts):
    notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/1/abc", log=log)

    for i in range(MAX_MESSAGES_PER_HOUR + 5):
        await notifier.send(f"message {i}")

    assert len(captured_posts) == MAX_MESSAGES_PER_HOUR
    assert "notify.rate_limited" in log.names()


async def test_a_dropped_message_is_logged_so_it_is_not_lost(log, captured_posts):
    notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/1/abc", log=log)
    for i in range(MAX_MESSAGES_PER_HOUR):
        await notifier.send(f"message {i}")

    await notifier.send("THE DROPPED ONE")
    assert "THE DROPPED ONE" in json.dumps(log.events)


async def test_the_message_is_posted_to_the_configured_url(log, captured_posts):
    notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/1/abc", log=log)
    await notifier.send("hello world")

    assert captured_posts[0]["url"] == "https://discord.com/api/webhooks/1/abc"
    assert captured_posts[0]["json"]["content"] == "hello world"


async def test_long_messages_are_truncated_below_discords_limit(log, captured_posts):
    notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/1/abc", log=log)
    await notifier.send("x" * 5000)

    assert len(captured_posts[0]["json"]["content"]) <= 1900


def test_hourly_cap_is_enforced(log):
    notifier = DiscordNotifier(
        webhook_url="https://discord.com/api/webhooks/1/abc", log=log
    )
    notifier._sent_timestamps = [1000.0] * MAX_MESSAGES_PER_HOUR
    assert notifier._within_rate_limit(1001.0) is False


def test_stale_sends_fall_out_of_the_hourly_window(log):
    notifier = DiscordNotifier(
        webhook_url="https://discord.com/api/webhooks/1/abc", log=log
    )
    notifier._sent_timestamps = [0.0] * MAX_MESSAGES_PER_HOUR
    # An hour later, the old sends no longer count.
    assert notifier._within_rate_limit(4000.0) is True


# --- message content -------------------------------------------------------


def test_opened_message_names_the_essentials():
    message = position_opened_message(make_position(), "live", Decimal("20.50"))
    assert "BTC/USDT:USDT" in message
    assert "LONG" in message
    assert "64000" in message
    assert "live" in message


def test_short_is_distinguishable_from_long():
    long_msg = position_opened_message(make_position("long"), "live", Decimal("20.5"))
    short_msg = position_opened_message(make_position("short"), "live", Decimal("20.5"))
    assert long_msg != short_msg
    assert "SHORT" in short_msg


def test_closed_message_shows_a_win_and_a_loss_differently():
    win = position_closed_message("BTC/USDT:USDT", "long", Decimal("1.25"), Decimal("21.75"))
    loss = position_closed_message("BTC/USDT:USDT", "long", Decimal("-0.5"), Decimal("20.0"))
    assert "✅" in win
    assert "❌" in loss


def test_closed_message_handles_unknown_pnl():
    """Absent PnL must not render as zero -- that would read as break-even."""
    message = position_closed_message("BTC/USDT:USDT", "long", None, Decimal("20.5"))
    assert "0.00" not in message.split("Realised")[1].split("|")[0]


def test_critical_message_is_visually_distinct():
    assert "🚨" in critical_message("HALTED", "detail")


def test_prices_never_render_in_scientific_notation():
    """
    Decimal.normalize() turns 58000.00 into 5.8E+4. Unreadable on a phone.
    """
    message = position_opened_message(
        make_position(), "live", Decimal("20.5008")
    )
    assert "E+" not in message
    assert "20.50" in message


def test_size_keeps_significant_digits_without_scientific_notation():
    position = make_position()
    object.__setattr__(position, "contracts", Decimal("0.00090000"))
    message = position_opened_message(position, "live", Decimal("20.50"))
    assert "0.0009" in message
    assert "E-" not in message


# --- SL / TP on the open alert ---------------------------------------------


def test_opened_message_shows_stop_and_target():
    message = position_opened_message(
        make_position("short"), "live", Decimal("38.03"),
        stop_price=Decimal("63831.44"), target_price=Decimal("63309.32"),
    )
    assert "63831.44" in message
    assert "63309.32" in message
    assert "SL" in message and "TP" in message


def test_a_missing_stop_is_shown_loudly_not_omitted():
    """
    If the stop is genuinely absent that is the most important thing on the
    alert. A silently missing line would read as though all were well.
    """
    message = position_opened_message(make_position(), "live", Decimal("38.03"))
    assert "NOT FOUND" in message


def test_risk_in_usdt_is_shown():
    position = make_position("long")           # 0.001 @ 64000.5
    message = position_opened_message(
        position, "live", Decimal("38.03"),
        stop_price=Decimal("63000"), target_price=Decimal("66000"),
    )
    # |64000.5 - 63000| * 0.001 = 1.0005
    assert "1.0005" in message


def test_reward_to_risk_is_shown():
    position = make_position("long")           # entry 64000.5
    message = position_opened_message(
        position, "live", Decimal("38.03"),
        stop_price=Decimal("63000.5"), target_price=Decimal("66000.5"),
    )
    assert "R:R" in message and "2.00" in message


def test_distance_percentages_are_shown():
    message = position_opened_message(
        make_position("long"), "live", Decimal("38.03"),
        stop_price=Decimal("62720.49"), target_price=Decimal("66560.52"),
    )
    assert "%" in message
