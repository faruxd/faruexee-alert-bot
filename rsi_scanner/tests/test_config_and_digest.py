"""
Config validation and digest rendering.

The config tests exist because a typo'd threshold must fail loudly at
startup, not silently scan with defaults. The digest tests exist because the
message is the entire product -- if it renders SHIB's price in scientific
notation, the scanner has failed at its job even with perfect maths.
"""

import pytest

from rsi_scanner.config import Config
from rsi_scanner.notify import _fmt_price, build_digest
from rsi_scanner.scan import ScanResult, Signal

WEBHOOK = "https://discord.com/api/webhooks/1/abc"


def sig(symbol, direction, prev, now, close=100.0, prev_close=98.0, crypto=True):
    return Signal(
        symbol=symbol, direction=direction, rsi_prev=prev, rsi_now=now,
        close=close, prev_close=prev_close, bar_ts_ms=1_700_000_000_000,
        is_crypto=crypto,
    )


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def test_defaults_need_no_environment():
    cfg = Config.from_env({})
    assert cfg.webhook_url is None
    assert cfg.day_boundary == "utc"
    assert cfg.post_when_empty is False
    assert len(cfg.symbols) > 20


def test_non_https_webhook_is_rejected():
    """A plain-http webhook would leak the credential over the wire."""
    with pytest.raises(ValueError):
        Config.from_env({"DISCORD_RSI_WEBHOOK": "http://discord.com/api/webhooks/1/x"})


def test_error_message_never_contains_the_webhook():
    secret = "http://discord.com/api/webhooks/1/SUPERSECRETTOKEN"
    with pytest.raises(ValueError) as excinfo:
        Config.from_env({"DISCORD_RSI_WEBHOOK": secret})
    assert "SUPERSECRETTOKEN" not in str(excinfo.value)


def test_blank_webhook_is_treated_as_absent():
    assert Config.from_env({"DISCORD_RSI_WEBHOOK": "   "}).webhook_url is None


def test_inverted_thresholds_are_rejected():
    with pytest.raises(ValueError):
        Config.from_env({"RSI_OVERSOLD": "70", "RSI_OVERBOUGHT": "30"})


def test_out_of_range_thresholds_are_rejected():
    with pytest.raises(ValueError):
        Config.from_env({"RSI_OVERSOLD": "-5"})
    with pytest.raises(ValueError):
        Config.from_env({"RSI_OVERBOUGHT": "150"})


def test_bad_day_boundary_is_rejected():
    with pytest.raises(ValueError):
        Config.from_env({"DAY_BOUNDARY": "tokyo"})


def test_symbols_override_is_parsed_and_uppercased():
    cfg = Config.from_env({"RSI_SYMBOLS": "btcusdt, ethusdt ,SOLUSDT"})
    assert cfg.symbols == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def test_flags_accept_common_truthy_spellings():
    for value in ("1", "true", "TRUE", "yes", "on"):
        assert Config.from_env({"POST_WHEN_EMPTY": value}).post_when_empty is True
    for value in ("0", "false", "no", "", "maybe"):
        assert Config.from_env({"POST_WHEN_EMPTY": value}).post_when_empty is False


# --------------------------------------------------------------------------
# Price formatting
# --------------------------------------------------------------------------

def test_prices_never_render_in_scientific_notation():
    """SHIB near 1e-5 is the case that breaks naive formatting."""
    for value in (0.0000082, 0.00001, 0.5, 1.0, 182.4, 4012.75, 78221.2):
        assert "e" not in _fmt_price(value).lower()


def test_large_and_small_prices_stay_readable():
    assert _fmt_price(78221.2) == "78,221.2"
    assert _fmt_price(182.437) == "182.437"
    assert _fmt_price(0.0000082).startswith("0.0000082")


# --------------------------------------------------------------------------
# Digest
# --------------------------------------------------------------------------

def test_digest_lists_both_directions():
    result = ScanResult(
        signals=[sig("BTCUSDT", "bullish", 26.1, 32.6), sig("ETHUSDT", "bearish", 74.0, 68.2)],
        scanned=29, failures=[],
    )
    text = build_digest(result)
    assert "Bullish reset" in text and "BTC" in text
    assert "Bearish reset" in text and "ETH" in text


def test_digest_shows_configured_thresholds_not_hardcoded_ones():
    result = ScanResult(signals=[sig("BTCUSDT", "bullish", 18.0, 21.0)], scanned=29, failures=[])
    text = build_digest(result, oversold=20.0, overbought=80.0)
    assert "`20`" in text
    assert "`30`" not in text


def test_empty_scan_says_so_rather_than_rendering_blank_sections():
    text = build_digest(ScanResult(signals=[], scanned=29, failures=[]))
    assert "No resets" in text
    assert "Bullish reset" not in text


def test_failures_are_surfaced_not_hidden():
    result = ScanResult(signals=[], scanned=27, failures=[("FOOUSDT", "bad"), ("BARUSDT", "bad")])
    assert "2 failed" in build_digest(result)


def test_non_crypto_symbols_are_tagged():
    result = ScanResult(
        signals=[sig("XAUUSDT", "bullish", 26.0, 31.0, crypto=False)], scanned=29, failures=[]
    )
    text = build_digest(result)
    assert "⧉" in text and "non-crypto" in text


def test_digest_stays_under_the_discord_limit_on_a_full_house():
    """Every symbol firing at once must not produce a rejected 2000+ char post."""
    signals = [sig(f"SYM{i:02d}USDT", "bullish" if i % 2 else "bearish", 26.0, 31.0)
               for i in range(40)]
    text = build_digest(ScanResult(signals=signals, scanned=40, failures=[]))
    assert len(text) <= 1900


def test_crypto_sorts_above_non_crypto():
    result = ScanResult(
        signals=[sig("XAUUSDT", "bullish", 25.0, 31.0, crypto=False),
                 sig("BTCUSDT", "bullish", 29.0, 31.0, crypto=True)],
        scanned=29, failures=[],
    )
    text = build_digest(result)
    assert text.index("BTC") < text.index("XAU")


def test_empty_scan_is_dated_by_the_last_closed_bar_not_by_now():
    """
    Regression: with no signals there is no Signal to read a timestamp from,
    and falling back to the wall clock labelled the digest with the date of
    the bar still forming -- i.e. tomorrow.
    """
    bar_ts = 1_755_734_400_000            # 2025-08-21 00:00 UTC
    result = ScanResult(signals=[], scanned=29, failures=[], last_bar_ts_ms=bar_ts)
    assert "2025-08-21" in build_digest(result)


def test_signal_timestamp_is_used_when_present():
    result = ScanResult(
        signals=[sig("BTCUSDT", "bullish", 26.0, 31.0)], scanned=29, failures=[],
        last_bar_ts_ms=1_755_734_400_000,
    )
    assert "2025-08-21" in build_digest(result)
