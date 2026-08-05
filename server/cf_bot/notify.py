"""
Discord notifications.

THREE RULES, in priority order:

1. A notification failure must NEVER affect trading. Discord being down, slow,
   rate-limiting us, or returning garbage cannot be allowed to raise into the
   trading loop. This is the one place in the codebase where swallowing an
   exception is correct -- but it is still logged, never silent.

2. It must never flood. There is prior art in this repo of a retry loop
   hammering a Discord channel. So: identical consecutive messages are
   suppressed, and there is a hard hourly cap after which messages are logged
   instead of sent.

3. It must never leak the webhook URL. That URL is a credential -- anyone
   holding it can post to the channel. It comes from the environment only and
   is never logged, not even partially.

Absent DISCORD_WEBHOOK_URL, the notifier is disabled and every call is a no-op.
The bot runs identically with or without it.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Optional

WEBHOOK_ENV_VAR = "DISCORD_WEBHOOK_URL"

# Hard ceiling. Well above any legitimate rate -- the bot caps at 3 entries a
# day -- so hitting this means something is wrong, and the right response is to
# stop posting rather than to keep hammering the channel.
MAX_MESSAGES_PER_HOUR = 20

HTTP_TIMEOUT_SECONDS = 10.0


@dataclass
class DiscordNotifier:
    """
    Fire-and-forget Discord webhook client.

    Construct via from_env(). Every send is best-effort and returns None.
    """

    webhook_url: Optional[str]
    log: object = None

    _sent_timestamps: list[float] = None  # type: ignore[assignment]
    _last_message: Optional[str] = None

    def __post_init__(self) -> None:
        if self._sent_timestamps is None:
            self._sent_timestamps = []

    @classmethod
    def from_env(cls, log, env: Optional[Mapping[str, str]] = None) -> "DiscordNotifier":
        env = os.environ if env is None else env
        raw = env.get(WEBHOOK_ENV_VAR)
        url = raw.strip() if raw and raw.strip() else None

        if url and not url.startswith("https://"):
            log.warning(
                "notify.webhook_rejected",
                note=(
                    f"{WEBHOOK_ENV_VAR} must be an https URL. Notifications are "
                    "disabled. The URL itself is not logged."
                ),
            )
            url = None

        log.info("notify.configured", discord_enabled=bool(url))
        return cls(webhook_url=url, log=log)

    @property
    def enabled(self) -> bool:
        return self.webhook_url is not None

    def _within_rate_limit(self, now: float) -> bool:
        cutoff = now - 3600.0
        self._sent_timestamps = [t for t in self._sent_timestamps if t >= cutoff]
        return len(self._sent_timestamps) < MAX_MESSAGES_PER_HOUR

    async def send(self, message: str, *, allow_duplicate: bool = False) -> None:
        """
        Post to Discord. Never raises, never blocks the caller meaningfully.

        Returns as soon as the request completes or times out. Callers treat
        this as best-effort and do not check the result.
        """
        if not self.enabled:
            return

        if not allow_duplicate and message == self._last_message:
            return  # identical to the previous message; almost certainly a loop

        loop = asyncio.get_running_loop()
        now = loop.time()
        if not self._within_rate_limit(now):
            self.log.warning(
                "notify.rate_limited",
                cap_per_hour=MAX_MESSAGES_PER_HOUR,
                dropped_message=message,
                note="not sending; logging instead so the information is not lost",
            )
            return

        try:
            import aiohttp  # ccxt already depends on it

            timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    self.webhook_url, json={"content": message[:1900]}
                ) as response:
                    if response.status >= 400:
                        # Deliberately does NOT log the URL.
                        self.log.warning(
                            "notify.send_failed",
                            status=response.status,
                            note="Discord rejected the message",
                        )
                        return
        except Exception as exc:
            # Rule 1: a notification failure must never reach the trading loop.
            self.log.warning(
                "notify.send_error",
                error=str(exc)[:200],
                note="notification dropped; trading is unaffected",
            )
            return

        self._sent_timestamps.append(now)
        self._last_message = message


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------


def _fmt(value: Optional[Decimal], places: str = "0.01") -> str:
    """
    Fixed-point, never scientific notation.

    Decimal.normalize() turns 58000.00 into 5.8E+4, which is unreadable in a
    phone notification at 3am. The 'f' format spec forces plain digits.
    """
    if value is None:
        return "?"
    try:
        return f"{value.quantize(Decimal(places)):f}"
    except Exception:
        return f"{value:f}"


def _fmt_size(value: Optional[Decimal]) -> str:
    """Position size: trailing zeros stripped, but still never scientific."""
    if value is None:
        return "?"
    text = f"{value:f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _pct_away(price: Optional[Decimal], entry: Optional[Decimal]) -> str:
    if price is None or entry is None or entry == 0:
        return ""
    pct = abs(price - entry) / entry * Decimal(100)
    return f" ({_fmt(pct, '0.01')}%)"


def position_opened_message(
    position,
    mode: str,
    equity: Optional[Decimal],
    stop_price: Optional[Decimal] = None,
    target_price: Optional[Decimal] = None,
) -> str:
    arrow = "🟢 LONG" if position.side == "long" else "🔴 SHORT"
    entry = position.entry_price

    lines = [
        f"**{arrow} opened** — `{position.symbol}`",
        f"Size: `{_fmt_size(position.contracts)}`  |  Entry: `{_fmt(entry)}`",
    ]

    # A missing level is shown as "not found" rather than omitted. If the stop
    # is genuinely absent that is the single most important thing on the alert,
    # and a silently missing line would read as though everything were fine.
    stop_text = f"`{_fmt(stop_price)}`{_pct_away(stop_price, entry)}" if stop_price else "`NOT FOUND`"
    target_text = (
        f"`{_fmt(target_price)}`{_pct_away(target_price, entry)}" if target_price else "`NOT FOUND`"
    )
    lines.append(f"🛑 SL: {stop_text}  |  🎯 TP: {target_text}")

    if stop_price is not None and entry is not None:
        risk = abs(entry - stop_price) * position.contracts
        detail = f"Risk: `{_fmt(risk, '0.0001')}` USDT"
        if target_price is not None:
            reward = abs(target_price - entry) * position.contracts
            denominator = abs(entry - stop_price)
            if denominator > 0:
                rr = abs(target_price - entry) / denominator
                detail += f"  |  R:R `{_fmt(rr, '0.01')}`"
            detail += f"  |  Target: `{_fmt(reward, '0.0001')}` USDT"
        lines.append(detail)

    lines.append(
        f"Liq: `{_fmt(position.liquidation_price)}`  |  "
        f"Equity: `{_fmt(equity)}` USDT  |  mode: `{mode}`"
    )
    return "\n".join(lines)


def position_closed_message(
    symbol: str, side: str, realised_pnl: Optional[Decimal], equity: Optional[Decimal]
) -> str:
    if realised_pnl is None:
        outcome = "closed"
        emoji = "⚪"
    elif realised_pnl >= 0:
        outcome = f"+{_fmt(realised_pnl, '0.0001')} USDT"
        emoji = "✅"
    else:
        outcome = f"{_fmt(realised_pnl, '0.0001')} USDT"
        emoji = "❌"

    return (
        f"**{emoji} {side.upper()} closed** — `{symbol}`\n"
        f"Realised: `{outcome}`  |  Equity: `{_fmt(equity)}` USDT"
    )


def critical_message(headline: str, detail: str) -> str:
    return f"🚨 **{headline}**\n{detail}"
