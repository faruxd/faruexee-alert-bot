"""The pure snapshot types, and the guarantee that logs never carry credentials."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from cf_bot.logging_setup import redact_secrets
from cf_bot.state import AccountState, OpenOrder, Position, to_decimal


def _position(**overrides) -> Position:
    base = dict(
        symbol="BTC/USDT:USDT",
        side="long",
        contracts=Decimal("0.01"),
        entry_price=Decimal("64000"),
        mark_price=Decimal("64250"),
        liquidation_price=Decimal("58000"),
        unrealized_pnl=Decimal("2.5"),
        margin_mode="isolated",
        leverage=Decimal("10"),
    )
    base.update(overrides)
    return Position(**base)


def _state(**overrides) -> AccountState:
    base = dict(
        fetched_at_ms=1717000000000,
        mode="demo",
        position_mode="one_way_mode",
        equity=Decimal("1000"),
        available=Decimal("900"),
        positions=(),
        open_orders=(),
        todays_fills=(),
    )
    base.update(overrides)
    return AccountState(**base)


# --- decimal conversion ----------------------------------------------------


def test_to_decimal_avoids_binary_float_error():
    assert to_decimal(0.1) == Decimal("0.1")
    assert to_decimal("64000.5") == Decimal("64000.5")


@pytest.mark.parametrize("empty", [None, "", "  ", "null", "None", "nan"])
def test_to_decimal_returns_none_for_absent_values(empty):
    """Absent must stay absent. Coercing a missing price to 0 is how stops end up at zero."""
    assert to_decimal(empty) is None


# --- immutability ----------------------------------------------------------


def test_position_is_frozen():
    pos = _position()
    with pytest.raises(Exception):
        pos.contracts = Decimal("999")


def test_account_state_is_frozen():
    state = _state()
    with pytest.raises(Exception):
        state.equity = Decimal("0")


# --- flatness --------------------------------------------------------------


def test_empty_state_is_flat():
    assert _state().is_flat is True


def test_state_with_a_position_is_not_flat():
    assert _state(positions=(_position(),)).is_flat is False


def test_state_with_only_zero_size_positions_is_flat():
    assert _state(positions=(_position(contracts=Decimal("0")),)).is_flat is True


# --- restart comparison ----------------------------------------------------


def test_comparable_ignores_fetch_time_and_marks():
    """Two reads of the same account seconds apart must compare equal."""
    a = _state(fetched_at_ms=1, positions=(_position(mark_price=Decimal("64000")),))
    b = _state(fetched_at_ms=999999, positions=(_position(mark_price=Decimal("71000")),))
    assert a.comparable() == b.comparable()


def test_comparable_ignores_position_ordering():
    p1 = _position(symbol="BTC/USDT:USDT")
    p2 = _position(symbol="ETH/USDT:USDT")
    assert _state(positions=(p1, p2)).comparable() == _state(positions=(p2, p1)).comparable()


def test_comparable_detects_a_real_size_change():
    a = _state(positions=(_position(contracts=Decimal("0.01")),))
    b = _state(positions=(_position(contracts=Decimal("0.02")),))
    assert a.comparable() != b.comparable()


def test_comparable_detects_a_new_resting_order():
    order = OpenOrder(
        order_id="1",
        client_order_id=None,
        symbol="BTC/USDT:USDT",
        side="sell",
        order_type="limit",
        price=Decimal("65000"),
        amount=Decimal("0.01"),
        filled=Decimal("0"),
        remaining=Decimal("0.01"),
        reduce_only=True,
        status="open",
        timestamp_ms=1,
    )
    assert _state().comparable() != _state(open_orders=(order,)).comparable()


# --- logging safety --------------------------------------------------------


def test_log_payload_is_json_serialisable():
    payload = _state(positions=(_position(),)).log_payload()
    json.dumps(payload)  # must not raise on Decimal


@pytest.mark.parametrize(
    "key",
    ["apiKey", "api_key", "secret", "passphrase", "password", "sign", "signature",
     "ACCESS-KEY", "Authorization", "token"],
)
def test_redaction_scrubs_secret_keys(key):
    out = redact_secrets(None, None, {key: "REALVALUE", "event": "x"})
    assert out[key] == "<redacted>"
    assert out["event"] == "x"


@pytest.mark.parametrize("key", ["signal", "signal_bar_ts", "signals"])
def test_diagnostic_keys_are_not_over_redacted(key):
    """
    'signal' contains 'sign', which was scrubbing the OS signal number out of
    shutdown logs and the strategy's signal description. Over-redaction is the
    safe direction but it still destroys the diagnostics you need at 3am.
    """
    out = redact_secrets(None, None, {key: "SIGTERM", "event": "x"})
    assert out[key] == "SIGTERM"


def test_real_signature_keys_are_still_redacted():
    """The loosening must not have opened a hole."""
    for key in ("sign", "signature", "ACCESS-SIGN", "req_sign"):
        out = redact_secrets(None, None, {key: "SECRETSIG"})
        assert out[key] == "<redacted>", f"{key} leaked"


def test_redaction_reaches_nested_structures():
    event = {
        "event": "request",
        "headers": {"ACCESS-KEY": "REALKEY", "Content-Type": "application/json"},
        "orders": [{"apiKey": "REALKEY", "symbol": "BTC/USDT:USDT"}],
    }
    out = redact_secrets(None, None, event)
    assert out["headers"]["ACCESS-KEY"] == "<redacted>"
    assert out["headers"]["Content-Type"] == "application/json"
    assert out["orders"][0]["apiKey"] == "<redacted>"
    assert out["orders"][0]["symbol"] == "BTC/USDT:USDT"
    assert "REALKEY" not in json.dumps(out)
