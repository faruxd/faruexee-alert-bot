"""
Shared fixtures.

No test in this suite opens a socket. The fake client below implements exactly
the surface reconcile, preflight, orders and trader use, so the whole system is
exercised against handcrafted exchange payloads.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from cf_bot.exchange import DemoModeRefusal, ExchangeError


class FakeBitgetClient:
    """
    Stand-in for BitgetClient with the same surface.

    Any attribute set to an Exception instance is raised instead of returned,
    which is how the tests exercise the fail-closed paths.

    Order methods record what they were asked to do in `sent_orders` and honour
    the mode gate, so a test can prove demo never transmits.
    """

    def __init__(
        self,
        position_mode="one_way_mode",
        authorities=("readonly", "trade"),
        balance=None,
        positions=None,
        open_orders=None,
        fills=None,
        closed_positions=None,
        margin_coin="USDT",
        symbols=("BTC/USDT:USDT",),
        mode="live",
        ohlcv=None,
        funding=Decimal("0"),
    ):
        self.position_mode = position_mode
        self.authorities = authorities
        self.balance = (
            balance
            if balance is not None
            else {"USDT": {"total": "1000.5", "free": "980.25"}}
        )
        self.positions = positions if positions is not None else []
        self.open_orders = open_orders if open_orders is not None else []
        self.fills = fills if fills is not None else []
        self.closed_positions = closed_positions if closed_positions is not None else []
        self.margin_coin = margin_coin
        self.symbols = tuple(symbols)
        self.mode = mode
        self.trading_enabled = mode == "live"
        self.ohlcv = ohlcv if ohlcv is not None else []
        self.funding = funding

        self.connect_calls = 0
        self.close_calls = 0
        self.reconcile_calls = 0
        self.sent_orders: list[dict] = []
        self.cancelled: list[str] = []
        self.cancel_all_calls: list[str] = []

    # -- lifecycle ---------------------------------------------------------

    async def connect(self):
        self.connect_calls += 1

    async def close(self):
        self.close_calls += 1

    def market_id(self, symbol):
        return symbol.split("/")[0] + "USDT"

    def amount_to_precision(self, symbol, amount):
        return Decimal(amount).quantize(Decimal("0.0001"))

    def price_to_precision(self, symbol, price):
        return Decimal(price).quantize(Decimal("0.01"))

    def min_amount(self, symbol):
        return Decimal("0.0001")

    @staticmethod
    def _resolve(value):
        if isinstance(value, Exception):
            raise value
        return value

    def _require_trading(self, what):
        if not self.trading_enabled:
            raise DemoModeRefusal(f"refusing to {what}: MODE={self.mode}")

    # -- reads -------------------------------------------------------------

    async def fetch_position_mode(self):
        self.reconcile_calls += 1
        return self._resolve(self.position_mode)

    async def fetch_authorities(self):
        return self._resolve(self.authorities)

    async def fetch_balance(self):
        return self._resolve(self.balance)

    async def fetch_positions(self):
        return self._resolve(self.positions)

    async def fetch_open_orders(self):
        return self._resolve(self.open_orders)

    async def fetch_todays_fills(self):
        return self._resolve(self.fills)

    async def fetch_todays_closed_positions(self):
        return self._resolve(self.closed_positions)

    async def fetch_ohlcv(self, symbol, timeframe, limit, since=None):
        return self._resolve(self.ohlcv)

    async def fetch_funding_rate(self, symbol):
        return self._resolve(self.funding)

    # -- writes ------------------------------------------------------------

    async def create_entry_with_protection(self, **kwargs):
        self._require_trading(f"place an entry on {kwargs.get('symbol')}")
        self.sent_orders.append({"kind": "entry", **kwargs})
        return {"id": "order-1", "filled": 0}

    async def create_reduce_only_market(self, symbol, side, amount, client_order_id):
        self._require_trading(f"close {symbol} at market")
        self.sent_orders.append(
            {"kind": "close", "symbol": symbol, "side": side, "amount": amount}
        )
        self.positions = []  # the close worked
        return {"id": "close-1"}

    async def cancel_order(self, order_id, symbol):
        self._require_trading(f"cancel {order_id}")
        self.cancelled.append(order_id)
        return {"id": order_id, "status": "canceled"}

    async def cancel_all_orders(self, symbol):
        self._require_trading(f"cancel all orders on {symbol}")
        self.cancel_all_calls.append(symbol)
        self.open_orders = []
        return []


@pytest.fixture
def fake_client():
    return FakeBitgetClient()


@pytest.fixture
def exchange_error():
    return ExchangeError("simulated venue failure")


# --- Sample ccxt payloads --------------------------------------------------


@pytest.fixture
def ccxt_position_long():
    return {
        "symbol": "BTC/USDT:USDT",
        "side": "long",
        "contracts": 0.01,
        "entryPrice": 64000.5,
        "markPrice": 64250.0,
        "liquidationPrice": 58000.0,
        "unrealizedPnl": 2.495,
        "marginMode": "isolated",
        "leverage": 10,
        "timestamp": 1717000000000,
    }


@pytest.fixture
def ccxt_position_zero():
    """ccxt reports these for symbols you merely have leverage configured on."""
    return {
        "symbol": "BTC/USDT:USDT",
        "side": None,
        "contracts": 0,
        "entryPrice": None,
        "markPrice": 64250.0,
        "liquidationPrice": None,
        "unrealizedPnl": 0,
        "marginMode": "isolated",
        "leverage": 10,
    }


@pytest.fixture
def ccxt_open_order():
    return {
        "id": "1234567890",
        "clientOrderId": "cfe0123456789012345678",
        "symbol": "BTC/USDT:USDT",
        "side": "buy",
        "type": "limit",
        "price": 63500.0,
        "amount": 0.01,
        "filled": 0.0,
        "remaining": 0.01,
        "reduceOnly": False,
        "status": "open",
        "timestamp": 1717000000000,
    }


@pytest.fixture
def ccxt_stop_order():
    return {
        "id": "9876543210",
        "clientOrderId": "cfs0123456789012345678",
        "symbol": "BTC/USDT:USDT",
        "side": "sell",
        "type": "market",
        "price": None,
        "amount": 0.01,
        "filled": 0.0,
        "remaining": 0.01,
        "reduceOnly": True,
        "status": "open",
        "timestamp": 1717000000000,
    }


@pytest.fixture
def ccxt_trade():
    return {
        "id": "t-1",
        "order": "1234567890",
        "clientOrderId": "cfe0123456789012345678",
        "symbol": "BTC/USDT:USDT",
        "side": "buy",
        "price": 63500.0,
        "amount": 0.01,
        "cost": 635.0,
        "fee": {"cost": 0.127, "currency": "USDT"},
        "timestamp": 1717000000000,
    }


@pytest.fixture
def ccxt_closed_position():
    return {
        "symbol": "BTC/USDT:USDT",
        "side": "long",
        "realizedPnl": -12.5,
        "lastUpdateTimestamp": 1717000000000,
        "info": {"netProfit": "-12.5", "utime": "1717000000000"},
    }


@pytest.fixture
def valid_config_yaml() -> str:
    return """
exchange:
  symbols:
    - "BTC/USDT:USDT"
  timeframe: "5m"
  product_type: "USDT-FUTURES"
  margin_coin: "USDT"
risk:
  risk_pct: 1.0
strategy:
  k: 2.5
  s: 1.25
  p: 30
runtime:
  loop_interval_seconds: 15.0
  heartbeat_seconds: 300.0
  kill_file: "KILL"
logging:
  level: "INFO"
  file: "logs/cf_bot.jsonl"
"""


@pytest.fixture
def write_config(tmp_path: Path):
    def _write(text: str) -> Path:
        path = tmp_path / "config.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    return _write
