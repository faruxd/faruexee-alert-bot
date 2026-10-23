"""
The trade filters: EMA seeding, the funding percentile, and every gate.

The data fetchers are replaced with canned series, so these tests prove the
decision logic without touching Binance or Bitget. The live fetchers are
exercised by a dry run (python -m rsi_scanner --dry-run).
"""

import pytest

from rsi_scanner.filters import (
    SOURCE_BITGET,
    FilterContext,
    ema_tv,
    funding_crowding,
    percentile,
)

DAY = 86_400_000
B = 1_790_000_000_000 // DAY * DAY                      # a UTC midnight
NOW = B + 5 * 60_000                                    # 00:05 UTC, like the cron


# --------------------------------------------------------------------------
# Maths
# --------------------------------------------------------------------------

def test_ema_is_seeded_with_the_sma_like_tradingview():
    # seed = mean(1,2,3) = 2; alpha = 0.5 -> 3 after 4, 4 after 5
    assert ema_tv([1, 2, 3, 4, 5], 3) == pytest.approx(4.0)


def test_ema_needs_at_least_length_values():
    assert ema_tv([1.0] * 199, 200) is None


def test_percentile_interpolates_linearly():
    assert percentile([1, 2, 3, 4], 0.9) == pytest.approx(3.7)
    assert percentile([1, 2, 3, 4], 0.1) == pytest.approx(1.3)


def funding_rows(days=400, rate=0.0001, recent_days=0, recent_rate=None, per_day=3):
    """`days` of settlements before today, on exact interval boundaries like the
    real ones (00:00/08:00/16:00); the last `recent_days` use `recent_rate`."""
    rows = []
    step = DAY // per_day
    for d in range(days, 0, -1):
        for k in range(per_day):
            ts = B - d * DAY + k * step
            r = recent_rate if (recent_rate is not None and d <= recent_days) else rate
            rows.append((ts, r))
    return rows


def test_quiet_funding_is_not_crowded():
    current, low, high = funding_crowding(funding_rows(), NOW)
    assert low <= current <= high


def test_a_week_of_high_funding_is_crowded_long():
    current, _, high = funding_crowding(funding_rows(recent_days=7, recent_rate=0.001), NOW)
    assert current > high


def test_funding_needs_enough_history():
    assert funding_crowding(funding_rows(days=40), NOW) is None


def test_stale_funding_data_is_not_judged():
    """Newest settlement 3 days old: the feed is broken, not the market quiet."""
    rows = [r for r in funding_rows() if r[0] < B - 3 * DAY]
    assert funding_crowding(rows, NOW) is None


def test_result_does_not_depend_on_whether_the_newest_settlement_is_published():
    """At 00:05 the 00:00 settlement may or may not be out yet. Same answer either way."""
    without = funding_crowding(funding_rows(), NOW)
    with_ = funding_crowding(funding_rows() + [(B, 0.0001)], NOW)
    assert without == pytest.approx(with_)


def test_funding_compares_sums_so_an_interval_change_is_like_for_like():
    """Same daily funding paid as 6 small settlements instead of 3 is not 'crowded'."""
    rows = funding_rows(days=400, rate=0.0001)
    rows = [r for r in rows if not (B - 3 * DAY <= r[0] < B - 2 * DAY)]
    rows += [(B - 3 * DAY + k * (DAY // 6), 0.00005) for k in range(6)]
    current, low, high = funding_crowding(rows, NOW)
    assert low <= current <= high


# --------------------------------------------------------------------------
# The gates
# --------------------------------------------------------------------------

def series(closes):
    start = NOW - len(closes) * DAY - DAY
    return [(start + i * DAY, float(c)) for i, c in enumerate(closes)]


UP = series(range(100, 360))                 # steadily rising: above both EMAs
DOWN = series(range(360, 100, -1))           # steadily falling: below both EMAs
# Above its 200-day EMA but below its 50-day EMA (derivation in the test below).
DIP = series([100] * 200 + [200] * 30 + [150] * 10)

QUIET = (funding_rows(), "binance")
CROWDED_LONG = (funding_rows(recent_days=7, recent_rate=0.001), "binance")
CROWDED_SHORT = (funding_rows(recent_days=7, recent_rate=-0.001), "binance")


class FakeFilters(FilterContext):
    def __init__(self, daily, funding):
        super().__init__(session=object(), now_ms=NOW, log=lambda *a: None)
        self.fake_daily, self.fake_funding = daily, funding

    def daily_closes(self, symbol):
        return self.fake_daily.get(symbol)

    def funding(self, symbol):
        return self.fake_funding.get(symbol, (None, None))


def check(symbol, direction, daily, funding, crypto=True):
    return FakeFilters(daily, funding).check(symbol, direction, crypto)


def test_dip_fixture_is_above_the_200_and_below_the_50():
    closes = [c for _, c in DIP]
    assert closes[-1] > ema_tv(closes, 200)
    assert closes[-1] < ema_tv(closes, 50)


def test_alt_long_passes_when_all_three_agree():
    v = check("SOLUSDT", "bullish", {"SOLUSDT": UP, "BTCUSDT": UP}, {"SOLUSDT": QUIET})
    assert v.passed is True and v.blocked_by == []


def test_long_below_the_200_ema_is_blocked():
    v = check("SOLUSDT", "bullish", {"SOLUSDT": DOWN, "BTCUSDT": UP}, {"SOLUSDT": QUIET})
    assert v.passed is False
    assert "below EMA200" in v.blocked_by


def test_short_above_the_200_ema_is_blocked():
    v = check("SOLUSDT", "bearish", {"SOLUSDT": UP, "BTCUSDT": DOWN}, {"SOLUSDT": QUIET})
    assert v.passed is False
    assert "above EMA200" in v.blocked_by


def test_alt_long_is_blocked_while_btc_is_below_its_50_ema():
    v = check("ETHUSDT", "bullish", {"ETHUSDT": UP, "BTCUSDT": DIP}, {"ETHUSDT": QUIET})
    assert v.passed is False
    assert "BTC below EMA50" in v.blocked_by


def test_btc_itself_is_not_gated_by_the_btc_filter():
    """BTC above its 200 but below its 50: an alt would be blocked, BTC is not."""
    v = check("BTCUSDT", "bullish", {"BTCUSDT": DIP}, {"BTCUSDT": QUIET})
    assert v.passed is True


def test_non_crypto_is_not_gated_by_the_btc_filter():
    v = check("XAUUSDT", "bullish", {"XAUUSDT": UP, "BTCUSDT": DOWN}, {"XAUUSDT": QUIET}, crypto=False)
    assert v.passed is True


def test_crowded_long_funding_blocks_longs_only():
    long_v = check("SOLUSDT", "bullish", {"SOLUSDT": UP, "BTCUSDT": UP}, {"SOLUSDT": CROWDED_LONG})
    short_v = check("SOLUSDT", "bearish", {"SOLUSDT": DOWN, "BTCUSDT": DOWN}, {"SOLUSDT": CROWDED_LONG})
    assert long_v.passed is False and "funding crowded long" in long_v.blocked_by
    assert short_v.passed is True


def test_crowded_short_funding_blocks_shorts():
    v = check("SOLUSDT", "bearish", {"SOLUSDT": DOWN, "BTCUSDT": DOWN}, {"SOLUSDT": CROWDED_SHORT})
    assert v.passed is False and "funding crowded short" in v.blocked_by


def test_symbol_without_binance_data_is_unverified_not_dropped():
    """CRO and the metals aren't on Binance: the alert goes out, marked."""
    v = check("CROUSDT", "bullish", {"BTCUSDT": UP}, {})
    assert v.passed is None
    assert v.unavailable == ["trend", "funding"]


def test_a_block_wins_over_missing_data():
    v = check("SOLUSDT", "bullish", {"SOLUSDT": DOWN, "BTCUSDT": UP}, {})
    assert v.passed is False


def test_bitget_fallback_is_reported():
    v = check("SOLUSDT", "bullish", {"SOLUSDT": UP, "BTCUSDT": UP},
              {"SOLUSDT": (funding_rows(days=90), SOURCE_BITGET)})
    assert v.passed is True
    assert v.funding_source == SOURCE_BITGET
