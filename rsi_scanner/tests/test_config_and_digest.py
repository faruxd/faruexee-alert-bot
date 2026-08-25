"""
Config validation and digest rendering.

The config tests exist because a typo'd threshold must fail loudly at
startup, not silently scan with defaults. The digest tests exist because the
message is the entire product -- if it renders SHIB's price in scientific
notation, the scanner has failed at its job even with perfect maths.
"""

import pytest

from rsi_scanner.config import Config
from rsi_scanner.notify import _fmt_price, build_digest, build_messages
from rsi_scanner.scan import ScanResult, Signal

WEBHOOK = "https://discord.com/api/webhooks/1/abc"


def sig(symbol, direction, prev, now, tf="1D", close=100.0, prev_close=98.0,
        crypto=True, daily_rsi=None):
    return Signal(
        symbol=symbol, direction=direction, timeframe=tf, rsi_prev=prev, rsi_now=now,
        close=close, prev_close=prev_close, bar_ts_ms=1_700_000_000_000,
        is_crypto=crypto, daily_rsi=daily_rsi,
    )


def res(signals=(), reported=("1D",), **kw):
    kw.setdefault("scanned", 34)
    return ScanResult(signals=list(signals), reported=list(reported), **kw)


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

def test_daily_section_lists_both_directions():
    text = build_digest(res([
        sig("BTCUSDT", "bullish", 26.1, 32.6),
        sig("ETHUSDT", "bearish", 74.0, 68.2),
    ]))
    assert "Daily" in text
    assert "Bullish" in text and "BTC" in text
    assert "Bearish" in text and "ETH" in text


def test_digest_shows_configured_thresholds_not_hardcoded_ones():
    text = build_digest(res([sig("BTCUSDT", "bullish", 18.0, 21.0)]),
                        oversold=20.0, overbought=80.0)
    assert "`20`" in text and "`30`" not in text


def test_timeframe_with_no_fresh_bar_is_omitted_entirely():
    """
    A 04:00 run has new 4H information but no new daily. Rendering a "Daily"
    header there would imply the daily had just closed.
    """
    text = build_digest(res([sig("SOLUSDT", "bullish", 26.0, 33.0, tf="4H")],
                            reported=["4H"]))
    assert "4-Hour" in text
    assert "Daily" not in text


def test_both_sections_render_when_both_are_fresh():
    text = build_digest(res(
        [sig("BTCUSDT", "bearish", 74.0, 68.0, tf="1D"),
         sig("SOLUSDT", "bullish", 26.0, 33.0, tf="4H", daily_rsi=58.0)],
        reported=["1D", "4H"],
    ))
    assert "Daily" in text and "4-Hour" in text
    assert text.index("Daily") < text.index("4-Hour")


def test_4h_lines_carry_the_daily_rsi_as_context():
    text = build_digest(res([sig("SOLUSDT", "bullish", 26.0, 33.0, tf="4H", daily_rsi=58.4)],
                            reported=["4H"]))
    assert "1D `58`" in text


def test_no_fresh_bar_at_all_says_so():
    """The duplicate-guard case: a run that had nothing new to look at."""
    text = build_digest(res([], reported=[]))
    assert "No newly closed bar" in text


def test_suppressed_count_is_surfaced():
    """A silent filter that swallows everything must not look like a quiet market."""
    text = build_digest(res([], suppressed=7))
    assert "7" in text and "suppressed" in text


def test_empty_scan_says_no_resets():
    text = build_digest(res([]))
    assert "No resets" in text


def test_failures_are_surfaced_not_hidden():
    text = build_digest(res([], failures=[("FOOUSDT", "bad"), ("BARUSDT", "bad")]))
    assert "2 failed" in text


def test_non_crypto_symbols_are_tagged():
    text = build_digest(res([sig("XAUUSDT", "bullish", 26.0, 31.0, crypto=False)]))
    assert "⧉" in text and "non-crypto" in text


def test_a_full_house_is_split_across_messages_not_truncated():
    """
    Three tiers on a big reset day runs past 2000 characters. Cutting the tail
    would silently drop the counter-trend section; splitting keeps everything.
    """
    signals = [sig(f"SYM{i:02d}USDT", "bullish" if i % 2 else "bearish", 26.0, 31.0,
                   tf="1D" if i % 3 else "4H")
               for i in range(60)]
    result = res(signals, reported=["1D", "4H"])
    messages = build_messages(result)
    assert all(len(m) <= 1900 for m in messages)
    # Nothing lost: every symbol still appears somewhere.
    joined = chr(10).join(messages)
    for i in range(60):
        assert f"SYM{i:02d}" in joined


def test_split_messages_are_numbered():
    signals = [sig(f"SYM{i:02d}USDT", "bearish", 78.0, 65.0) for i in range(60)]
    messages = build_messages(res(signals, reported=["1D"]))
    assert len(messages) > 1
    assert "(1/" in messages[0]


def test_a_short_digest_is_a_single_unnumbered_message():
    messages = build_messages(res([sig("BTCUSDT", "bullish", 26.0, 33.0)]))
    assert len(messages) == 1
    assert "(1/" not in messages[0]


def test_crypto_sorts_above_non_crypto():
    text = build_digest(res([
        sig("XAUUSDT", "bullish", 25.0, 31.0, crypto=False),
        sig("BTCUSDT", "bullish", 29.0, 31.0, crypto=True),
    ]))
    assert text.index("BTC") < text.index("XAU")


def test_most_stretched_sorts_first():
    text = build_digest(res([
        sig("AAAUSDT", "bearish", 71.0, 69.0),
        sig("BBBUSDT", "bearish", 88.0, 65.0),
    ]))
    assert text.index("BBB") < text.index("AAA")


def test_empty_scan_is_dated_by_the_last_closed_bar_not_by_now():
    """
    Regression: with no signals there is no Signal to read a timestamp from,
    and falling back to the wall clock labelled the digest with the date of
    the bar still forming -- i.e. tomorrow.
    """
    text = build_digest(res([], last_bar_ts_ms=1_755_734_400_000))
    assert "2025-08-21" in text
