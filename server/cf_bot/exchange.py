"""
Bitget client wrapper (async ccxt).

This is the ONLY module in the package that touches the network. Everything else
consumes its output. That boundary is what makes the rest of the bot testable
without a socket.

It returns raw-but-parsed exchange responses and makes no trading decisions.
Order placement policy -- retries, rate limiting, protection attachment -- lives
in orders.py, one layer up.

MODE ENFORCEMENT
----------------
Only `live` may transmit an order. Every write method checks `_trading_enabled`
and raises DemoModeRefusal otherwise, before a request is even constructed. The
gate is structural, not a convention someone has to remember.

Endpoint and field names below were verified against ccxt 4.5.70's generated
Bitget method table and current Bitget v2 docs, not recalled from memory:
    privateMixGetV2MixAccountAccount  -> GET /api/v2/mix/account/account (posMode)
    privateSpotGetV2SpotAccountInfo   -> GET /api/v2/spot/account/info  (authorities)
    createOrder params stopLoss/takeProfit -> presetStopLossPrice / presetStopSurplusPrice
    postOnly -> force='post_only'
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional, Sequence

import ccxt.async_support as ccxt

from cf_bot.config import Credentials, Settings


class ExchangeError(Exception):
    """Any failure talking to Bitget."""


class OrderRejected(ExchangeError):
    """
    The venue refused the order and retrying it unchanged will not help.

    Distinct from ExchangeError because the retry policy treats them
    differently: a rejection is final, a transport failure is worth another go.
    """


class DemoModeRefusal(ExchangeError):
    """An order was requested while MODE=demo. Demo cannot transmit orders."""


# ccxt exception classes that mean "the venue said no", as opposed to "the
# network failed". Retrying these unchanged is pointless and, for a duplicate
# clientOid, actively wrong.
_FINAL_REJECTION_TYPES = (
    ccxt.InvalidOrder,
    ccxt.InsufficientFunds,
    ccxt.BadRequest,
    ccxt.PermissionDenied,
    ccxt.AuthenticationError,
    ccxt.DuplicateOrderId,
)


# Bitget's hard cap on candles per request. Verified empirically against the
# live endpoint: asking for 500 or 1000 both return 200. Do NOT raise this
# because a doc page says otherwise -- the venue is the authority.
MAX_BARS_PER_REQUEST = 200

_TIMEFRAME_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def timeframe_to_ms(timeframe: str) -> int:
    try:
        return _TIMEFRAME_MS[timeframe]
    except KeyError:
        raise ExchangeError(
            f"unknown timeframe {timeframe!r}; known: {sorted(_TIMEFRAME_MS)}"
        ) from None


def utc_day_start_ms(now: Optional[datetime] = None) -> int:
    """Milliseconds since epoch at 00:00:00 UTC of the current day."""
    now = datetime.now(timezone.utc) if now is None else now.astimezone(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp() * 1000)


class BitgetClient:
    """
    Thin async wrapper around ccxt.bitget.

    Lifecycle:
        client = BitgetClient(creds, settings, mode)
        await client.connect()
        ...
        await client.close()      # ALWAYS, or aiohttp leaks the session
    """

    def __init__(
        self, credentials: Credentials, settings: Settings, mode: str = "demo"
    ) -> None:
        self._settings = settings
        self._mode = mode
        self._trading_enabled = mode == "live"
        self._symbols = tuple(settings.exchange.symbols)

        # ccxt names the Bitget passphrase `password`. Verified against
        # ccxt.bitget().requiredCredentials -> {'apiKey', 'secret', 'password'}.
        self._exchange = ccxt.bitget(
            {
                "apiKey": credentials.api_key,
                "secret": credentials.api_secret,
                "password": credentials.passphrase,
                "enableRateLimit": True,
                "options": {"defaultType": "swap", "defaultSubType": "linear"},
            }
        )
        self._connected = False

    # -- lifecycle ---------------------------------------------------------

    async def connect(self) -> None:
        """Load markets and confirm every configured symbol exists."""
        try:
            await self._exchange.load_markets()
        except Exception as exc:
            raise ExchangeError(f"load_markets failed: {exc}") from exc

        missing = [s for s in self._symbols if s not in self._exchange.markets]
        if missing:
            raise ExchangeError(
                f"configured symbol(s) not present in Bitget's market list: {missing}. "
                "Check config.yaml exchange.symbols."
            )
        self._connected = True

    async def close(self) -> None:
        try:
            await self._exchange.close()
        except Exception:
            # Closing must never mask the real reason we are shutting down.
            pass
        self._connected = False

    def _require_connected(self) -> None:
        if not self._connected:
            raise ExchangeError("client used before connect() -- this is a programming error")

    def _require_trading_enabled(self, what: str) -> None:
        if not self._trading_enabled:
            raise DemoModeRefusal(
                f"refusing to {what}: MODE={self._mode}. Only MODE=live may transmit orders."
            )

    @property
    def symbols(self) -> tuple[str, ...]:
        return self._symbols

    @property
    def margin_coin(self) -> str:
        return self._settings.exchange.margin_coin

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def trading_enabled(self) -> bool:
        return self._trading_enabled

    def market_id(self, symbol: str) -> str:
        """Bitget's own symbol id, e.g. 'BTCUSDT', for the raw v2 endpoints."""
        self._require_connected()
        return self._exchange.market(symbol)["id"]

    def amount_to_precision(self, symbol: str, amount: Decimal) -> Decimal:
        self._require_connected()
        return Decimal(self._exchange.amount_to_precision(symbol, float(amount)))

    def price_to_precision(self, symbol: str, price: Decimal) -> Decimal:
        self._require_connected()
        return Decimal(self._exchange.price_to_precision(symbol, float(price)))

    def min_amount(self, symbol: str) -> Optional[Decimal]:
        """Venue minimum order size, or None if the venue does not publish one."""
        self._require_connected()
        limits = self._exchange.market(symbol).get("limits") or {}
        minimum = (limits.get("amount") or {}).get("min")
        return Decimal(str(minimum)) if minimum is not None else None

    # -- raw v2 reads ------------------------------------------------------

    async def fetch_position_mode(self) -> str:
        """
        Return the account's position mode: 'one_way_mode' or 'hedge_mode'.
        GET /api/v2/mix/account/account -> data.posMode
        """
        self._require_connected()
        params = {
            "symbol": self.market_id(self._symbols[0]),
            "productType": self._settings.exchange.product_type,
            "marginCoin": self._settings.exchange.margin_coin,
        }
        try:
            response = await self._exchange.privateMixGetV2MixAccountAccount(params)
        except Exception as exc:
            raise ExchangeError(f"could not read account position mode: {exc}") from exc

        data = (response or {}).get("data") or {}
        pos_mode = data.get("posMode")
        if not pos_mode:
            raise ExchangeError(
                "account response contained no 'posMode' field; refusing to assume a "
                f"position mode. Raw data keys: {sorted(data.keys())}"
            )
        return str(pos_mode).strip()

    async def fetch_authorities(self) -> tuple[str, ...]:
        """
        Permissions attached to this API key, e.g. ('trade', 'readonly').
        GET /api/v2/spot/account/info -> data.authorities
        """
        self._require_connected()
        try:
            response = await self._exchange.privateSpotGetV2SpotAccountInfo({})
        except Exception as exc:
            raise ExchangeError(f"could not read API key permissions: {exc}") from exc

        data = (response or {}).get("data") or {}
        authorities = data.get("authorities")
        if authorities is None:
            raise ExchangeError(
                "account info response contained no 'authorities' field; cannot prove "
                "this key lacks withdrawal permission. Refusing to start. "
                f"Raw data keys: {sorted(data.keys())}"
            )
        if not isinstance(authorities, (list, tuple)):
            raise ExchangeError(
                f"'authorities' was {type(authorities).__name__}, expected a list."
            )
        return tuple(str(a).strip() for a in authorities)

    # -- unified ccxt reads ------------------------------------------------

    async def fetch_balance(self) -> dict[str, Any]:
        self._require_connected()
        try:
            return await self._exchange.fetch_balance()
        except Exception as exc:
            raise ExchangeError(f"fetch_balance failed: {exc}") from exc

    async def fetch_positions(self) -> list[dict[str, Any]]:
        self._require_connected()
        try:
            return await self._exchange.fetch_positions(list(self._symbols))
        except Exception as exc:
            raise ExchangeError(f"fetch_positions failed: {exc}") from exc

    async def fetch_open_orders(self) -> list[dict[str, Any]]:
        """Open orders across every configured symbol."""
        self._require_connected()
        collected: list[dict[str, Any]] = []
        for symbol in self._symbols:
            try:
                collected.extend(await self._exchange.fetch_open_orders(symbol))
            except Exception as exc:
                raise ExchangeError(f"fetch_open_orders failed for {symbol}: {exc}") from exc
        return collected

    async def fetch_todays_fills(self, now: Optional[datetime] = None) -> list[dict[str, Any]]:
        """
        Every fill since 00:00 UTC today, across every configured symbol.

        'Today' is the UTC day, matching the daily entry counter, which must be
        derived from exchange fills rather than a local variable.
        """
        self._require_connected()
        since = utc_day_start_ms(now)
        collected: list[dict[str, Any]] = []
        for symbol in self._symbols:
            try:
                collected.extend(
                    await self._exchange.fetch_my_trades(symbol, since=since)
                )
            except Exception as exc:
                raise ExchangeError(f"fetch_my_trades failed for {symbol}: {exc}") from exc
        return collected

    async def fetch_todays_closed_positions(
        self, now: Optional[datetime] = None
    ) -> list[dict[str, Any]]:
        """
        Positions closed since 00:00 UTC today, for the daily loss limit and the
        consecutive-loss counter. Both must survive a restart, so both read from
        here rather than from anything this process remembers.
        """
        self._require_connected()
        since = utc_day_start_ms(now)
        try:
            return await self._exchange.fetch_positions_history(
                list(self._symbols), since=since
            )
        except Exception as exc:
            raise ExchangeError(f"fetch_positions_history failed: {exc}") from exc

    async def fetch_funding_rate(self, symbol: str) -> Optional[Decimal]:
        """Last settled funding rate as a fraction. None if the venue omits it."""
        self._require_connected()
        try:
            info = await self._exchange.fetch_funding_rate(symbol)
        except Exception as exc:
            raise ExchangeError(f"fetch_funding_rate failed for {symbol}: {exc}") from exc

        rate = info.get("fundingRate")
        return Decimal(str(rate)) if rate is not None else None

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, limit: int, since: Optional[int] = None
    ) -> list[list]:
        """
        Closed candles, oldest first.

        Bitget returns AT MOST MAX_BARS_PER_REQUEST rows however many you ask
        for -- verified empirically: requesting 500 or 1000 both return 200. So
        anything larger has to be paged, and the loop must never treat a short
        batch as "no more data", because every full batch is short relative to
        what we asked for.

        When no `since` is given we derive one from the bar count requested, so
        a caller can simply ask for N bars ending now.
        """
        self._require_connected()

        if limit <= 0:
            return []

        bar_ms = timeframe_to_ms(timeframe)
        cursor = since
        if cursor is None:
            # Ask for a window a little wider than needed; the venue trims to
            # whatever history it actually has.
            span = int(limit * bar_ms * 1.2)
            cursor = int(datetime.now(timezone.utc).timestamp() * 1000) - span

        collected: dict[int, list] = {}
        try:
            while len(collected) < limit:
                batch = await self._exchange.fetch_ohlcv(
                    symbol, timeframe, since=cursor, limit=MAX_BARS_PER_REQUEST
                )
                if not batch:
                    break

                for row in batch:
                    collected[int(row[0])] = row

                next_cursor = int(batch[-1][0]) + 1
                if next_cursor <= cursor:
                    break  # no forward progress; stop rather than spin
                cursor = next_cursor
        except Exception as exc:
            raise ExchangeError(f"fetch_ohlcv failed for {symbol}: {exc}") from exc

        collected_rows = [collected[ts] for ts in sorted(collected)]
        return collected_rows[-limit:]

        # De-duplicate by timestamp and keep chronological order.
        seen: dict[int, list] = {}
        for row in collected:
            seen[int(row[0])] = row
        ordered = [seen[ts] for ts in sorted(seen)]
        return ordered[-limit:]

    async def fetch_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        self._require_connected()
        try:
            return await self._exchange.fetch_order(order_id, symbol)
        except Exception as exc:
            raise ExchangeError(f"fetch_order {order_id} failed: {exc}") from exc

    # -- writes ------------------------------------------------------------

    async def create_entry_with_protection(
        self,
        symbol: str,
        side: str,
        amount: Decimal,
        price: Decimal,
        stop_price: Decimal,
        take_profit_price: Decimal,
        client_order_id: str,
        post_only: bool = True,
    ) -> dict[str, Any]:
        """
        One call: post-only limit entry with a stop-loss and take-profit PRESET
        onto the order itself.

        ccxt maps the stopLoss/takeProfit params to Bitget's presetStopLossPrice
        and presetStopSurplusPrice, so the protection is attached by the venue at
        fill time. There is no window in which the position exists unprotected
        because of a second round trip that has not happened yet.
        """
        self._require_connected()
        self._require_trading_enabled(f"place an entry on {symbol}")

        params: dict[str, Any] = {
            "clientOid": client_order_id,
            "stopLoss": {"triggerPrice": float(self.price_to_precision(symbol, stop_price))},
            "takeProfit": {
                "triggerPrice": float(self.price_to_precision(symbol, take_profit_price))
            },
        }
        if post_only:
            params["postOnly"] = True

        ccxt_side = "buy" if side == "long" else "sell"

        try:
            return await self._exchange.create_order(
                symbol,
                "limit",
                ccxt_side,
                float(self.amount_to_precision(symbol, amount)),
                float(self.price_to_precision(symbol, price)),
                params,
            )
        except _FINAL_REJECTION_TYPES as exc:
            raise OrderRejected(f"entry rejected for {symbol}: {exc}") from exc
        except Exception as exc:
            raise ExchangeError(f"entry submission failed for {symbol}: {exc}") from exc

    async def create_market_entry_with_protection(
        self,
        symbol: str,
        side: str,
        amount: Decimal,
        stop_price: Decimal,
        take_profit_price: Decimal,
        client_order_id: str,
    ) -> dict[str, Any]:
        """
        Market entry with the stop and target PRESET onto the order.

        The fallback leg for the scalper: used only when a passive limit failed
        to fill inside its window. Pays taker, so the caller should treat every
        use of this as a real cost, not a free convenience.

        Protection is still attached by the venue at fill time -- there is no
        variant of entry in this codebase that leaves a position bare.
        """
        self._require_connected()
        self._require_trading_enabled(f"place a market entry on {symbol}")

        params: dict[str, Any] = {
            "clientOid": client_order_id,
            "stopLoss": {"triggerPrice": float(self.price_to_precision(symbol, stop_price))},
            "takeProfit": {
                "triggerPrice": float(self.price_to_precision(symbol, take_profit_price))
            },
        }
        ccxt_side = "buy" if side == "long" else "sell"

        try:
            return await self._exchange.create_order(
                symbol,
                "market",
                ccxt_side,
                float(self.amount_to_precision(symbol, amount)),
                None,
                params,
            )
        except _FINAL_REJECTION_TYPES as exc:
            raise OrderRejected(f"market entry rejected for {symbol}: {exc}") from exc
        except Exception as exc:
            raise ExchangeError(f"market entry failed for {symbol}: {exc}") from exc

    async def create_reduce_only_market(
        self, symbol: str, side: str, amount: Decimal, client_order_id: str
    ) -> dict[str, Any]:
        """Market close of an existing position. `side` is the position's direction."""
        self._require_connected()
        self._require_trading_enabled(f"close {symbol} at market")

        ccxt_side = "sell" if side == "long" else "buy"
        params = {"clientOid": client_order_id, "reduceOnly": True}

        try:
            return await self._exchange.create_order(
                symbol,
                "market",
                ccxt_side,
                float(self.amount_to_precision(symbol, amount)),
                None,
                params,
            )
        except _FINAL_REJECTION_TYPES as exc:
            raise OrderRejected(f"reduce-only market rejected for {symbol}: {exc}") from exc
        except Exception as exc:
            raise ExchangeError(f"reduce-only market failed for {symbol}: {exc}") from exc

    async def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        self._require_connected()
        self._require_trading_enabled(f"cancel {order_id}")
        try:
            return await self._exchange.cancel_order(order_id, symbol)
        except ccxt.OrderNotFound:
            # Already gone. That is the state we wanted, so it is not an error.
            return {"id": order_id, "status": "canceled", "note": "already absent"}
        except Exception as exc:
            raise ExchangeError(f"cancel_order {order_id} failed: {exc}") from exc

    async def cancel_all_orders(self, symbol: str) -> Any:
        self._require_connected()
        self._require_trading_enabled(f"cancel all orders on {symbol}")
        try:
            return await self._exchange.cancel_all_orders(symbol)
        except Exception as exc:
            raise ExchangeError(f"cancel_all_orders failed for {symbol}: {exc}") from exc
