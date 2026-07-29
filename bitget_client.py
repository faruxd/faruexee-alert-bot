# =============================================================
#   BITGET V2 MIX (USDT-M FUTURES) REST CLIENT
# =============================================================
#
#   Minimal, explicit client for the endpoints the trade bot needs.
#   Every call either returns parsed data or raises BitgetError —
#   there is no silent failure path, because a swallowed error on a
#   stop-loss placement is how accounts die.
#
#   Credentials come from environment variables only. Never hardcode
#   them and never commit them.
# =============================================================

import base64
import hashlib
import hmac
import json
import time
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

import requests


BASE_URL = "https://api.bitget.com"


class BitgetError(Exception):
    """Any non-success response or transport failure from Bitget."""

    def __init__(self, message, code=None, endpoint=None):
        super().__init__(message)
        self.code = code
        self.endpoint = endpoint


class BitgetClient:
    def __init__(self, api_key, api_secret, passphrase,
                 product_type="USDT-FUTURES", margin_coin="USDT",
                 base_url=BASE_URL, recv_window_ms=5000, min_call_gap=0.12):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.product_type = product_type
        self.margin_coin = margin_coin
        self.base_url = base_url.rstrip("/")
        self.recv_window_ms = recv_window_ms
        self.min_call_gap = min_call_gap          # crude rate limiter
        self._last_call = 0.0
        self._specs: dict[str, dict] = {}
        self._session = requests.Session()

    # ---------------------------------------------------------
    #   SIGNING / TRANSPORT
    # ---------------------------------------------------------

    def _sign(self, timestamp, method, request_path, body_str):
        prehash = f"{timestamp}{method.upper()}{request_path}{body_str}"
        digest = hmac.new(
            self.api_secret.encode("utf-8"),
            prehash.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("utf-8")

    @staticmethod
    def _query_string(params):
        if not params:
            return ""
        clean = {k: v for k, v in params.items() if v is not None}
        if not clean:
            return ""
        return "?" + "&".join(f"{k}={v}" for k, v in clean.items())

    def _throttle(self):
        gap = time.time() - self._last_call
        if gap < self.min_call_gap:
            time.sleep(self.min_call_gap - gap)
        self._last_call = time.time()

    def _request(self, method, path, params=None, body=None, auth=True, retries=2):
        self._throttle()

        qs = self._query_string(params)
        request_path = path + qs
        body_str = json.dumps(body, separators=(",", ":")) if body else ""
        url = self.base_url + request_path

        headers = {"Content-Type": "application/json", "locale": "en-US"}
        if auth:
            if not (self.api_key and self.api_secret and self.passphrase):
                raise BitgetError("API credentials are not set", endpoint=path)
            ts = str(int(time.time() * 1000))
            headers.update({
                "ACCESS-KEY": self.api_key,
                "ACCESS-SIGN": self._sign(ts, method, request_path, body_str),
                "ACCESS-TIMESTAMP": ts,
                "ACCESS-PASSPHRASE": self.passphrase,
            })

        last_err = None
        for attempt in range(retries + 1):
            try:
                resp = self._session.request(
                    method.upper(), url,
                    headers=headers,
                    data=body_str if body else None,
                    timeout=15,
                )
                payload = resp.json()
            except Exception as e:
                last_err = BitgetError(f"transport failure: {e}", endpoint=path)
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise last_err

            code = str(payload.get("code", ""))
            if code == "00000":
                return payload.get("data")

            msg = payload.get("msg", "unknown error")
            # 429 / rate limit codes are worth retrying; business errors are not.
            if resp.status_code == 429 or code in ("429", "40018"):
                if attempt < retries:
                    time.sleep(2.0 * (attempt + 1))
                    continue
            raise BitgetError(f"{msg} (code {code}) on {path}", code=code, endpoint=path)

        raise last_err or BitgetError("request failed", endpoint=path)

    # ---------------------------------------------------------
    #   PUBLIC MARKET DATA
    # ---------------------------------------------------------

    def get_candles(self, symbol, granularity="1H", limit=300):
        """Oldest -> newest: [ts, open, high, low, close, baseVol, quoteVol]."""
        data = self._request(
            "GET", "/api/v2/mix/market/candles",
            params={
                "symbol": symbol,
                "productType": self.product_type,
                "granularity": granularity,
                "limit": limit,
            },
            auth=False,
        )
        return data or []

    def get_ticker(self, symbol):
        data = self._request(
            "GET", "/api/v2/mix/market/ticker",
            params={"symbol": symbol, "productType": self.product_type},
            auth=False,
        )
        return (data or [{}])[0]

    def load_contracts(self):
        """Fetch and cache contract specs for every symbol. Call once at startup."""
        data = self._request(
            "GET", "/api/v2/mix/market/contracts",
            params={"productType": self.product_type},
            auth=False,
        )
        self._specs = {c["symbol"]: c for c in (data or [])}
        return self._specs

    def spec(self, symbol):
        if symbol not in self._specs:
            self.load_contracts()
        if symbol not in self._specs:
            raise BitgetError(f"unknown symbol {symbol} on {self.product_type}")
        return self._specs[symbol]

    # ---------------------------------------------------------
    #   PRECISION HELPERS
    # ---------------------------------------------------------

    def price_tick(self, symbol):
        s = self.spec(symbol)
        place = int(s["pricePlace"])
        end_step = int(s.get("priceEndStep", 1) or 1)
        return Decimal(end_step) * (Decimal(10) ** -place)

    def round_price(self, symbol, price, mode="nearest"):
        """
        Quantise a price onto the symbol's tick grid.
        mode: "nearest" | "down" | "up"
        """
        tick = self.price_tick(symbol)
        place = int(self.spec(symbol)["pricePlace"])
        p = Decimal(str(price))
        units = p / tick
        if mode == "down":
            units = units.to_integral_value(rounding=ROUND_DOWN)
        elif mode == "up":
            units = (units.to_integral_value(rounding=ROUND_DOWN)
                     + (1 if units % 1 != 0 else 0))
        else:
            units = units.to_integral_value(rounding=ROUND_HALF_UP)
        out = (units * tick).quantize(Decimal(10) ** -place)
        return float(out)

    def round_size(self, symbol, size):
        """
        Quantise a position size DOWN onto the symbol's size grid.
        Returns 0.0 when the result is below the exchange minimum —
        callers must treat 0.0 as "cannot trade this".
        """
        s = self.spec(symbol)
        place = int(s["volumePlace"])
        min_qty = Decimal(str(s["minTradeNum"]))
        multiplier = Decimal(str(s.get("sizeMultiplier", "0") or "0"))

        step = multiplier if multiplier > 0 else (Decimal(10) ** -place)
        q = Decimal(str(size))
        units = (q / step).to_integral_value(rounding=ROUND_DOWN)
        out = (units * step).quantize(Decimal(10) ** -place)

        if out < min_qty:
            return 0.0
        return float(out)

    def min_notional(self, symbol):
        s = self.spec(symbol)
        return float(s.get("minTradeUSDT", 5) or 5)

    # ---------------------------------------------------------
    #   ACCOUNT
    # ---------------------------------------------------------

    def get_account(self):
        """Futures account for the configured margin coin."""
        data = self._request(
            "GET", "/api/v2/mix/account/accounts",
            params={"productType": self.product_type},
        )
        for acct in data or []:
            if acct.get("marginCoin") == self.margin_coin:
                return acct
        raise BitgetError(f"no {self.margin_coin} futures account found")

    def get_equity(self):
        acct = self.get_account()
        for key in ("usdtEquity", "accountEquity", "equity"):
            if acct.get(key) not in (None, ""):
                return float(acct[key])
        raise BitgetError("could not read account equity")

    def get_available(self):
        acct = self.get_account()
        return float(acct.get("available", 0) or 0)

    def set_position_mode(self, one_way=True):
        return self._request(
            "POST", "/api/v2/mix/account/set-position-mode",
            body={
                "productType": self.product_type,
                "posMode": "one_way_mode" if one_way else "hedge_mode",
            },
        )

    def set_margin_mode(self, symbol, mode="isolated"):
        return self._request(
            "POST", "/api/v2/mix/account/set-margin-mode",
            body={
                "symbol": symbol,
                "productType": self.product_type,
                "marginCoin": self.margin_coin,
                "marginMode": mode,
            },
        )

    def set_leverage(self, symbol, leverage):
        return self._request(
            "POST", "/api/v2/mix/account/set-leverage",
            body={
                "symbol": symbol,
                "productType": self.product_type,
                "marginCoin": self.margin_coin,
                "leverage": str(leverage),
            },
        )

    # ---------------------------------------------------------
    #   POSITIONS / ORDERS
    # ---------------------------------------------------------

    def get_positions(self):
        """Open positions with non-zero size."""
        data = self._request(
            "GET", "/api/v2/mix/position/all-position",
            params={"productType": self.product_type, "marginCoin": self.margin_coin},
        )
        return [p for p in (data or []) if float(p.get("total", 0) or 0) > 0]

    def get_position(self, symbol):
        for p in self.get_positions():
            if p.get("symbol") == symbol:
                return p
        return None

    def get_position_history(self, symbol=None, limit=20,
                             start_time=None, end_time=None):
        """
        Closed positions, newest first. Used by the journal to record the
        real net PnL of a trade rather than guessing from equity deltas.

        start_time / end_time are epoch milliseconds.
        """
        data = self._request(
            "GET", "/api/v2/mix/position/history-position",
            params={
                "symbol": symbol,
                "productType": self.product_type,
                "limit": limit,
                "startTime": start_time,
                "endTime": end_time,
            },
        )
        if isinstance(data, dict):
            return data.get("list") or []
        return data or []

    def get_realized_pnl_since(self, start_time_ms):
        """
        Net realised PnL from every position closed since start_time_ms.

        This is read straight from the exchange, so it survives the bot
        losing its local state — which is what makes the daily-loss stop
        durable on hosts with an ephemeral filesystem.
        """
        total = 0.0
        try:
            rows = self.get_position_history(limit=100, start_time=start_time_ms)
        except BitgetError:
            raise
        for r in rows:
            for key in ("netProfit", "pnl", "achievedProfits"):
                if r.get(key) not in (None, ""):
                    try:
                        total += float(r[key])
                    except (TypeError, ValueError):
                        pass
                    break
        return total

    def get_pending_orders(self, symbol=None):
        data = self._request(
            "GET", "/api/v2/mix/order/orders-pending",
            params={"productType": self.product_type, "symbol": symbol},
        )
        if isinstance(data, dict):
            return data.get("entrustedList") or []
        return data or []

    def get_order(self, symbol, order_id=None, client_oid=None):
        return self._request(
            "GET", "/api/v2/mix/order/detail",
            params={
                "symbol": symbol,
                "productType": self.product_type,
                "orderId": order_id,
                "clientOid": client_oid,
            },
        )

    def place_limit_entry(self, symbol, side, size, price,
                          stop_loss=None, take_profit=None,
                          client_oid=None, force="gtc", margin_mode="isolated"):
        """
        Resting limit entry in one-way mode.

        stop_loss is attached via presetStopLossPrice so the protective
        stop exists the instant the order fills — there is never a window
        where the position sits naked on the exchange.
        """
        body = {
            "symbol": symbol,
            "productType": self.product_type,
            "marginMode": margin_mode,
            "marginCoin": self.margin_coin,
            "size": str(size),
            "price": str(price),
            "side": side,                 # "buy" opens long, "sell" opens short
            "orderType": "limit",
            "force": force,
        }
        if client_oid:
            body["clientOid"] = client_oid
        if stop_loss is not None:
            body["presetStopLossPrice"] = str(stop_loss)
        if take_profit is not None:
            body["presetStopSurplusPrice"] = str(take_profit)

        return self._request("POST", "/api/v2/mix/order/place-order", body=body)

    def cancel_order(self, symbol, order_id=None, client_oid=None):
        body = {"symbol": symbol, "productType": self.product_type}
        if order_id:
            body["orderId"] = order_id
        if client_oid:
            body["clientOid"] = client_oid
        return self._request("POST", "/api/v2/mix/order/cancel-order", body=body)

    def close_position_market(self, symbol, hold_side, size):
        """Emergency / manual exit — reduce-only market order."""
        side = "sell" if hold_side == "long" else "buy"
        body = {
            "symbol": symbol,
            "productType": self.product_type,
            "marginMode": "isolated",
            "marginCoin": self.margin_coin,
            "size": str(size),
            "side": side,
            "orderType": "market",
            "reduceOnly": "YES",
        }
        return self._request("POST", "/api/v2/mix/order/place-order", body=body)

    # ---------------------------------------------------------
    #   TP / SL PLAN ORDERS
    # ---------------------------------------------------------

    def place_partial_tp(self, symbol, hold_side, size, trigger_price,
                         client_oid=None, trigger_type="mark_price"):
        """Partial take-profit — closes `size` of the position at trigger."""
        body = {
            "marginCoin": self.margin_coin,
            "productType": self.product_type,
            "symbol": symbol,
            "planType": "profit_plan",
            "triggerPrice": str(trigger_price),
            "triggerType": trigger_type,
            "executePrice": "0",          # 0 = market execution on trigger
            "holdSide": hold_side,        # "long" | "short"
            "size": str(size),
        }
        if client_oid:
            body["clientOid"] = client_oid
        return self._request("POST", "/api/v2/mix/order/place-tpsl-order", body=body)

    def set_position_stop(self, symbol, hold_side, trigger_price,
                          client_oid=None, trigger_type="mark_price"):
        """
        Whole-position stop loss. Placing a new one replaces the existing
        position stop, which is how the break-even move is applied.
        """
        body = {
            "marginCoin": self.margin_coin,
            "productType": self.product_type,
            "symbol": symbol,
            "planType": "pos_loss",
            "triggerPrice": str(trigger_price),
            "triggerType": trigger_type,
            "executePrice": "0",
            "holdSide": hold_side,
        }
        if client_oid:
            body["clientOid"] = client_oid
        return self._request("POST", "/api/v2/mix/order/place-tpsl-order", body=body)

    def get_plan_orders(self, symbol=None, plan_type="profit_loss"):
        data = self._request(
            "GET", "/api/v2/mix/order/orders-plan-pending",
            params={
                "productType": self.product_type,
                "planType": plan_type,
                "symbol": symbol,
            },
        )
        if isinstance(data, dict):
            return data.get("entrustedList") or []
        return data or []

    def cancel_plan_order(self, symbol, order_id, plan_type="profit_loss"):
        body = {
            "productType": self.product_type,
            "marginCoin": self.margin_coin,
            "orderIdList": [{"symbol": symbol, "orderId": order_id}],
            "planType": plan_type,
        }
        return self._request("POST", "/api/v2/mix/order/cancel-plan-order", body=body)
