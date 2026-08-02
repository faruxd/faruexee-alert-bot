"""Config, the mode gate, and the risk ceiling. Every one of these must fail closed."""

from __future__ import annotations

from decimal import Decimal

import pytest

from cf_bot.config import (
    ConfigError,
    Credentials,
    load_credentials,
    load_settings,
    resolve_mode,
)
from cf_bot.constants import MAX_RISK_PCT


# --- mode gate -------------------------------------------------------------


def test_mode_defaults_to_demo_when_unset():
    assert resolve_mode({}) == "demo"


def test_mode_defaults_to_demo_when_empty():
    assert resolve_mode({"MODE": "   "}) == "demo"


def test_mode_demo_explicit():
    assert resolve_mode({"MODE": "demo"}) == "demo"


@pytest.mark.parametrize("bad", ["production", "paper", "real", "LIVE_", "1", "true"])
def test_unknown_mode_raises_rather_than_falling_back(bad):
    with pytest.raises(ConfigError) as exc:
        resolve_mode({"MODE": bad})
    assert "not valid" in str(exc.value)


def test_live_requires_confirmation_env():
    with pytest.raises(ConfigError) as exc:
        resolve_mode({"MODE": "live"})
    assert "I_UNDERSTAND_THIS_TRADES_REAL_MONEY" in str(exc.value)


@pytest.mark.parametrize("bad", ["YES", "Yes", "true", "1", "y", ""])
def test_live_confirmation_must_be_exactly_lowercase_yes(bad):
    with pytest.raises(ConfigError):
        resolve_mode({"MODE": "live", "I_UNDERSTAND_THIS_TRADES_REAL_MONEY": bad})


def test_live_allowed_with_exact_confirmation():
    env = {"MODE": "live", "I_UNDERSTAND_THIS_TRADES_REAL_MONEY": "yes"}
    assert resolve_mode(env) == "live"


def test_live_is_never_reached_by_an_error_path():
    """A malformed MODE must never degrade into live."""
    for bad in ["liv", "LIVEE", "demo live", None]:
        env = {} if bad is None else {"MODE": bad}
        try:
            assert resolve_mode(env) != "live"
        except ConfigError:
            pass  # raising is also acceptable; silently becoming live is not


# --- credentials -----------------------------------------------------------


def test_credentials_load_from_env():
    creds = load_credentials(
        {
            "BITGET_API_KEY": "k",
            "BITGET_API_SECRET": "s",
            "BITGET_API_PASSPHRASE": "p",
        }
    )
    assert creds == Credentials("k", "s", "p")


@pytest.mark.parametrize(
    "missing", ["BITGET_API_KEY", "BITGET_API_SECRET", "BITGET_API_PASSPHRASE"]
)
def test_missing_any_credential_raises(missing):
    env = {
        "BITGET_API_KEY": "k",
        "BITGET_API_SECRET": "s",
        "BITGET_API_PASSPHRASE": "p",
    }
    del env[missing]
    with pytest.raises(ConfigError) as exc:
        load_credentials(env)
    assert missing in str(exc.value)


def test_blank_credential_is_treated_as_missing():
    with pytest.raises(ConfigError):
        load_credentials(
            {"BITGET_API_KEY": "   ", "BITGET_API_SECRET": "s", "BITGET_API_PASSPHRASE": "p"}
        )


def test_credentials_never_render_their_values():
    """repr/str/f-string must not leak. This is the last line of defence in a traceback."""
    creds = Credentials("REALKEY123", "REALSECRET456", "REALPASS789")
    for rendered in (repr(creds), str(creds), f"{creds}"):
        assert "REALKEY123" not in rendered
        assert "REALSECRET456" not in rendered
        assert "REALPASS789" not in rendered
        assert "redacted" in rendered


# --- risk ceiling ----------------------------------------------------------


def test_risk_pct_at_the_ceiling_is_accepted(write_config, valid_config_yaml):
    settings = load_settings(write_config(valid_config_yaml))
    assert settings.risk.risk_pct == Decimal("1.0")
    assert settings.risk.risk_pct <= MAX_RISK_PCT


@pytest.mark.parametrize("over", ["1.5", "2", "1.01", "100"])
def test_risk_pct_above_ceiling_raises_on_startup(write_config, valid_config_yaml, over):
    text = valid_config_yaml.replace("risk_pct: 1.0", f"risk_pct: {over}")
    with pytest.raises(ConfigError) as exc:
        load_settings(write_config(text))
    assert "MAX_RISK_PCT" in str(exc.value)


@pytest.mark.parametrize("bad", ["0", "-1", "-0.5"])
def test_non_positive_risk_pct_raises(write_config, valid_config_yaml, bad):
    text = valid_config_yaml.replace("risk_pct: 1.0", f"risk_pct: {bad}")
    with pytest.raises(ConfigError):
        load_settings(write_config(text))


def test_risk_pct_is_exact_decimal_not_binary_float(write_config, valid_config_yaml):
    """0.1 as a float is 0.1000000000000000055...; through Decimal(str()) it is exact."""
    text = valid_config_yaml.replace("risk_pct: 1.0", "risk_pct: 0.1")
    settings = load_settings(write_config(text))
    assert settings.risk.risk_pct == Decimal("0.1")
    assert str(settings.risk.risk_pct) == "0.1"


# --- config.yaml parsing ---------------------------------------------------


def test_unknown_config_key_is_fatal(write_config, valid_config_yaml):
    """A typo'd key must crash, not be silently ignored."""
    text = valid_config_yaml.replace("  risk_pct: 1.0", "  risk_pct: 1.0\n  risk_pcnt: 5.0")
    with pytest.raises(ConfigError):
        load_settings(write_config(text))


def test_missing_config_file_raises(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_settings(tmp_path / "nope.yaml")
    assert "not found" in str(exc.value)


def test_malformed_yaml_raises(write_config):
    with pytest.raises(ConfigError) as exc:
        load_settings(write_config("exchange: [unclosed\n"))
    assert "valid YAML" in str(exc.value)


def test_non_mapping_yaml_raises(write_config):
    with pytest.raises(ConfigError) as exc:
        load_settings(write_config("- just\n- a\n- list\n"))
    assert "mapping" in str(exc.value)


@pytest.mark.parametrize("bad_symbol", ["BTCUSDT", "BTC/USDT", "BTC-USDT", ""])
def test_symbol_must_be_a_unified_swap_symbol(write_config, valid_config_yaml, bad_symbol):
    text = valid_config_yaml.replace('- "BTC/USDT:USDT"', f'- "{bad_symbol}"')
    with pytest.raises(ConfigError):
        load_settings(write_config(text))


def test_empty_symbol_list_is_rejected(write_config, valid_config_yaml):
    text = valid_config_yaml.replace('  symbols:\n    - "BTC/USDT:USDT"', "  symbols: []")
    with pytest.raises(ConfigError):
        load_settings(write_config(text))


def test_duplicate_symbols_are_rejected(write_config, valid_config_yaml):
    """A duplicated symbol would be scanned twice and could double-count entries."""
    text = valid_config_yaml.replace(
        '    - "BTC/USDT:USDT"', '    - "BTC/USDT:USDT"\n    - "BTC/USDT:USDT"'
    )
    with pytest.raises(ConfigError) as exc:
        load_settings(write_config(text))
    assert "duplicate" in str(exc.value)


def test_multiple_symbols_are_accepted(write_config, valid_config_yaml):
    text = valid_config_yaml.replace(
        '    - "BTC/USDT:USDT"', '    - "BTC/USDT:USDT"\n    - "ETH/USDT:USDT"'
    )
    settings = load_settings(write_config(text))
    assert settings.exchange.symbols == ["BTC/USDT:USDT", "ETH/USDT:USDT"]


@pytest.mark.parametrize("bad_timeframe", ["1m", "15m", "1h", "5min"])
def test_timeframe_other_than_5m_is_rejected(write_config, valid_config_yaml, bad_timeframe):
    """The strategy's fixed bar counts are defined against 5m and do not transfer."""
    text = valid_config_yaml.replace('timeframe: "5m"', f'timeframe: "{bad_timeframe}"')
    with pytest.raises(ConfigError):
        load_settings(write_config(text))


def test_a_fourth_strategy_parameter_is_rejected(write_config, valid_config_yaml):
    """The spec allows exactly three tunables. A fourth must not load silently."""
    text = valid_config_yaml.replace("  p: 30", "  p: 30\n  q: 1.5")
    with pytest.raises(ConfigError):
        load_settings(write_config(text))


@pytest.mark.parametrize("bad_p", ["-1", "101", "150"])
def test_percentile_floor_must_be_a_percentile(write_config, valid_config_yaml, bad_p):
    text = valid_config_yaml.replace("  p: 30", f"  p: {bad_p}")
    with pytest.raises(ConfigError):
        load_settings(write_config(text))


def test_settings_are_frozen(write_config, valid_config_yaml):
    settings = load_settings(write_config(valid_config_yaml))
    with pytest.raises(Exception):
        settings.risk.risk_pct = Decimal("99")


@pytest.mark.parametrize("bad", ["0.5", "0", "301", "-5"])
def test_insane_loop_interval_raises(write_config, valid_config_yaml, bad):
    text = valid_config_yaml.replace("loop_interval_seconds: 15.0", f"loop_interval_seconds: {bad}")
    with pytest.raises(ConfigError):
        load_settings(write_config(text))
