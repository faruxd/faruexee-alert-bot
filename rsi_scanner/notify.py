"""
Discord digest.

ONE message per run, not one per symbol. Every daily bar in the universe
closes at the same instant and crypto is heavily correlated, so a genuine
market-wide reset day produces fifteen-plus hits simultaneously. Fifteen
separate webhook posts would trip Discord's rate limit, arrive out of order,
and be unreadable on a phone. A digest is both kinder and more robust.

The webhook URL is a credential -- anyone holding it can post to the channel.
It is never logged, not even partially, and never included in an error
message.
"""

from __future__ import annotations

import datetime as dt
from typing import List, Optional

import requests

from .scan import ScanResult, Signal
from .symbols import display_name

DISCORD_CONTENT_LIMIT = 2000
SAFE_CONTENT_LIMIT = 1900
HTTP_TIMEOUT = 15.0


def _fmt_price(value: float) -> str:
    """
    Fixed-point across six orders of magnitude.

    SHIB trades near 0.00001 and gold near 4000. A single format spec makes
    one of them unreadable, and %g renders the small one in scientific
    notation, which is useless in a notification.
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
    return (
        f"`{name:<7}`{tag} RSI `{sig.rsi_prev:.1f}` → `{sig.rsi_now:.1f}`  "
        f"@ `{_fmt_price(sig.close)}`  ({sig.pct_change:+.2f}%)"
    )


def build_digest(
    result: ScanResult,
    boundary: str = "utc",
    bar_ts_ms: Optional[int] = None,
    oversold: float = 30.0,
    overbought: float = 70.0,
) -> str:
    """Render the whole run as one Discord message."""
    if bar_ts_ms is None:
        bar_ts_ms = result.last_bar_ts_ms
    if bar_ts_ms is None and result.signals:
        bar_ts_ms = result.signals[0].bar_ts_ms

    if bar_ts_ms is not None:
        bar_date = dt.datetime.fromtimestamp(bar_ts_ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d")
    else:
        bar_date = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    boundary_note = "UTC day" if boundary == "utc" else "Bitget day (16:00 UTC close)"
    lines = [f"**📊 Daily RSI Reset — {bar_date}**  ·  `1D` · {boundary_note}"]

    bulls, bears = result.bullish, result.bearish

    if not bulls and not bears:
        lines.append("\nNo resets — every symbol closed between the levels.")
    else:
        if bulls:
            lines.append(f"\n🟢 **Bullish reset** — crossed back above `{oversold:g}`")
            lines += [_signal_line(s) for s in _ordered(bulls)]
        if bears:
            lines.append(f"\n🔴 **Bearish reset** — crossed back below `{overbought:g}`")
            lines += [_signal_line(s) for s in _ordered(bears)]

    footer = f"\n_Scanned {result.scanned} symbols_"
    if result.failures:
        names = ", ".join(display_name(s) for s, _ in result.failures[:6])
        more = f" +{len(result.failures) - 6}" if len(result.failures) > 6 else ""
        footer += f"  ·  ⚠️ {len(result.failures)} failed: {names}{more}"
    if any(not s.is_crypto for s in result.signals):
        footer += "\n_⧉ = non-crypto (session gaps differ)_"
    lines.append(footer)

    message = "\n".join(lines)
    if len(message) > SAFE_CONTENT_LIMIT:
        message = message[: SAFE_CONTENT_LIMIT - 20].rstrip() + "\n_…truncated_"
    return message


def _ordered(signals: List[Signal]) -> List[Signal]:
    """Crypto first, then by how deep the extreme was -- most stretched on top."""
    return sorted(signals, key=lambda s: (not s.is_crypto, abs(s.rsi_prev - 50.0) * -1))




def post(webhook_url: Optional[str], message: str, log=print) -> bool:
    """
    Best-effort webhook post. Returns True on success.

    Never raises: a notification failure is not worth a non-zero exit that
    would show up as a red X on a cron that actually did its job. It is
    logged loudly instead.
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
