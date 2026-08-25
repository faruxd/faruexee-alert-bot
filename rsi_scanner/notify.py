"""
Discord digest.

One digest per run, not one message per symbol. Every bar on a timeframe
closes at the same instant and crypto is heavily correlated, so a market-wide
reset produces a dozen-plus hits simultaneously. Separate posts per symbol
would trip Discord's rate limit, arrive out of order, and be unreadable on a
phone.

A digest that does not fit in one Discord message is SPLIT across several,
never truncated. Dropping the tail would silently discard alerts, and the
tail is where the counter-trend 4H section lives -- the longest and most
easily lost part.

Three tiers, deliberately unequal:

  Daily              full lines
  4H, daily agrees   full lines
  4H, against daily  compact, several per row

The third runs ~15 signals a day. Rendering it at full width would push the
signals that passed the daily filter off the bottom of the message, which
inverts the point of separating them in the first place.

Sections for a timeframe with no freshly closed bar are omitted entirely
rather than rendered empty -- on a 04:00 run only 4H has new information, and
showing a stale "1D" header would imply otherwise.

The webhook URL is a credential -- anyone holding it can post to the channel.
It is never logged, not even partially, and never included in an error message.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import List, Optional

import requests

from .scan import ScanResult, Signal
from .symbols import display_name

SAFE_CONTENT_LIMIT = 1900
HTTP_TIMEOUT = 15.0

# Discord allows bursts on a webhook but throttles hard past them. Two-message
# digests are the norm and three the worst case, so a short pause is enough.
INTER_MESSAGE_DELAY = 1.0

TF_LABEL = {"1D": "Daily", "4H": "4-Hour"}

GREEN = "\U0001F7E2"
RED = "\U0001F534"
UP = "↑"
DOWN = "↓"
BAR = "━"


def _fmt_price(value: float) -> str:
    """
    Fixed-point across six orders of magnitude.

    SHIB trades near 0.00001 and gold near 4000. A single format spec makes one
    of them unreadable, and %g renders the small one in scientific notation,
    which is useless in a notification.
    """
    if value >= 1000:
        return f"{value:,.1f}"
    if value >= 1:
        return f"{value:,.3f}".rstrip("0").rstrip(".")
    if value >= 0.001:
        return f"{value:.5f}"
    return f"{value:.8f}".rstrip("0")


def _signal_line(sig: Signal) -> str:
    name = display_name(sig.symbol)
    tag = "" if sig.is_crypto else " ⧉"
    line = (
        f"`{name:<7}`{tag} RSI `{sig.rsi_prev:.1f}` → `{sig.rsi_now:.1f}`  "
        f"@ `{_fmt_price(sig.close)}`  ({sig.pct_change:+.2f}%)"
    )
    if sig.timeframe == "4H" and sig.daily_rsi is not None:
        line += f"  · 1D `{sig.daily_rsi:.0f}`"
    return line


def _ordered(signals: List[Signal]) -> List[Signal]:
    """Crypto first, then most stretched on top."""
    return sorted(signals, key=lambda s: (not s.is_crypto, -abs(s.rsi_prev - 50.0)))


def _compact_lines(signals: List[Signal], emoji: str, arrow: str,
                   width: int = 78) -> List[str]:
    """
    Counter-trend 4H resets packed several to a row: `BTC 68` `ETH 66` ...

    Symbol and current RSI only. If you want the full picture for one of these
    you have the console log; the digest's job is to tell you which names to
    go and look at.
    """
    prefix = f"{emoji} {arrow}"
    lines: List[str] = []
    current = prefix
    for sig in _ordered(signals):
        cell = f"`{display_name(sig.symbol)} {sig.rsi_now:.0f}`"
        if current != prefix and len(current) + 1 + len(cell) > width:
            lines.append(current)
            current = prefix
        current += " " + cell
    lines.append(current)
    return lines


def _render(signals: List[Signal], compact: bool,
            oversold: float, overbought: float) -> List[str]:
    bulls = [s for s in signals if s.direction == "bullish"]
    bears = [s for s in signals if s.direction == "bearish"]
    out: List[str] = []
    for group, emoji, arrow, word, level, label in (
        (bulls, GREEN, UP, "above", oversold, "Bullish"),
        (bears, RED, DOWN, "below", overbought, "Bearish"),
    ):
        if not group:
            continue
        if compact:
            out.extend(_compact_lines(group, emoji, arrow))
        else:
            out.append(f"{emoji} **{label}** — back {word} `{level:g}`")
            out.extend(_signal_line(s) for s in _ordered(group))
    return out


def _build_lines(result: ScanResult, boundary: str, bar_ts_ms: Optional[int],
                 oversold: float, overbought: float) -> List[str]:
    if bar_ts_ms is None:
        # Prefer the newest bar actually reported; fall back to the daily.
        bar_ts_ms = result.reported_bar_ts_ms or result.last_bar_ts_ms

    stamp = (
        dt.datetime.fromtimestamp(bar_ts_ms / 1000, dt.timezone.utc)
        if bar_ts_ms is not None
        else dt.datetime.now(dt.timezone.utc)
    ).strftime("%Y-%m-%d")

    boundary_note = "UTC day" if boundary == "utc" else "Bitget day (16:00 UTC close)"
    lines = [f"**\U0001F4CA RSI Reset — {stamp}**  ·  {boundary_note}"]

    any_signal = False
    for timeframe in ("1D", "4H"):
        if timeframe not in result.reported:
            continue

        if timeframe == "1D":
            groups = [(f"**{BAR} {TF_LABEL['1D']}**", result.for_tf("1D"), False)]
        else:
            groups = [
                (f"**{BAR} 4-Hour**  _· daily agrees_", result.confirmed_4h(), False),
                (f"**{BAR} 4-Hour**  _· against the daily_", result.unconfirmed_4h(), True),
            ]

        for header, signals, compact in groups:
            # An empty counter-trend section is pure noise. The agreeing one
            # still prints "nothing", so a quiet market stays distinguishable
            # from a filter that has broken and is dropping everything.
            if compact and not signals:
                continue
            lines.append("")
            lines.append(header)
            if not signals:
                lines.append("_nothing_")
                continue
            any_signal = True
            lines.extend(_render(signals, compact, oversold, overbought))

    if not result.reported:
        # Nothing had a freshly closed bar. The run was a duplicate.
        lines.append("")
        lines.append("_No newly closed bar — nothing to report._")
    elif not any_signal:
        lines.append("")
        lines.append("_No resets._")

    footer = f"_Scanned {result.scanned} symbols_"
    if result.suppressed:
        footer += f"  ·  _{result.suppressed} 4H suppressed_"
    if result.failures:
        names = ", ".join(display_name(s) for s, _ in result.failures[:5])
        more = f" +{len(result.failures) - 5}" if len(result.failures) > 5 else ""
        footer += f"  ·  ⚠️ {len(result.failures)} failed: {names}{more}"
    if any(not s.is_crypto for s in result.signals):
        footer += "\n_⧉ = non-crypto (session gaps differ)_"
    lines.append("")
    lines.append(footer)
    return lines


def build_digest(
    result: ScanResult,
    boundary: str = "utc",
    bar_ts_ms: Optional[int] = None,
    oversold: float = 30.0,
    overbought: float = 70.0,
) -> str:
    """The whole digest as one string. Used for the console log and by tests."""
    return "\n".join(_build_lines(result, boundary, bar_ts_ms, oversold, overbought))


def build_messages(
    result: ScanResult,
    boundary: str = "utc",
    bar_ts_ms: Optional[int] = None,
    oversold: float = 30.0,
    overbought: float = 70.0,
    limit: int = SAFE_CONTENT_LIMIT,
) -> List[str]:
    """
    The digest split into Discord-sized messages, at line boundaries.

    Splitting rather than truncating: a full house across three tiers can run
    past 2000 characters, and cutting the tail would silently drop the
    counter-trend section entirely.

    A single line longer than the limit is hard-cut -- it cannot be split
    safely and in practice cannot occur, since the longest line is a compact
    row bounded well under the limit.
    """
    lines = _build_lines(result, boundary, bar_ts_ms, oversold, overbought)

    messages: List[str] = []
    current: List[str] = []
    size = 0
    for line in lines:
        if len(line) > limit:
            line = line[: limit - 1] + "…"
        # +1 for the newline that will join this line to the previous one.
        cost = len(line) + (1 if current else 0)
        if current and size + cost > limit:
            messages.append("\n".join(current))
            current, size = [line], len(line)
        else:
            current.append(line)
            size += cost
    if current:
        messages.append("\n".join(current))

    if len(messages) > 1:
        total = len(messages)
        messages = [f"{m}\n_({i + 1}/{total})_" for i, m in enumerate(messages)]
    return messages


def post(webhook_url: Optional[str], message: str, log=print) -> bool:
    """
    Best-effort webhook post. Returns True on success.

    Never raises: a notification failure is not worth a non-zero exit that
    would show up as a red X on a cron that actually did its job. It is logged
    loudly instead.
    """
    if not webhook_url:
        log("  [INFO] no webhook configured; message not sent")
        return False
    try:
        response = requests.post(
            webhook_url, json={"content": message[:SAFE_CONTENT_LIMIT]}, timeout=HTTP_TIMEOUT
        )
        if response.status_code >= 400:
            # Status only. The URL is the secret and must not appear here.
            log(f"  [ERROR] Discord rejected the message: HTTP {response.status_code}")
            return False
    except Exception as exc:
        log(f"  [ERROR] Discord post failed: {type(exc).__name__}: {str(exc)[:120]}")
        return False
    return True


def post_all(webhook_url: Optional[str], messages: List[str], log=print,
             delay: float = INTER_MESSAGE_DELAY) -> bool:
    """
    Post every part of a split digest, in order. True only if all succeeded.

    Stops at the first failure rather than hammering a webhook that is already
    rejecting us -- the remaining parts are logged by the caller's console
    output either way, so the information is not lost.
    """
    ok = True
    for index, message in enumerate(messages):
        if index:
            time.sleep(delay)
        if not post(webhook_url, message, log=log):
            ok = False
            break
    return ok
