"""
The 4H/1D confluence gate and the duplicate guard.

These tests drive scan() with canned RSI series rather than canned prices.
The RSI arithmetic is already pinned against Wilder's reference in
test_rsi.py; what needs proving here is the ORCHESTRATION -- which signals
survive the daily filter, and which timeframes a given run is allowed to
report at all.

Closes carry a sentinel in the last slot (1.0 = daily, 2.0 = 4H) so the fake
RSI can tell which series it is being asked for.
"""

import pytest

from rsi_scanner import scan as scan_mod
from rsi_scanner.bitget import MS_PER_4H, MS_PER_DAY
from rsi_scanner.config import Config

DAY0 = 1_700_000_000_000 // MS_PER_DAY * MS_PER_DAY

DAILY, H4 = 1.0, 2.0


@pytest.fixture
def wiring(monkeypatch):
    """Returns a setup(daily_series, h4_series, now_ms) -> ScanResult driver."""

    def setup(daily_series, h4_series, now_ms=None, symbols=("BTCUSDT",),
              daily_ts=DAY0, h4_ts=None, **cfg_kw):
        if h4_ts is None:
            h4_ts = DAY0 + 20 * 3_600_000       # the 20:00-00:00 bar

        def fake_bars_for(symbol, timeframe, boundary="utc", session=None, now_ms=None):
            marker = DAILY if timeframe == "1D" else H4
            ts = daily_ts if timeframe == "1D" else h4_ts
            bars = [[float(ts - i), 1.0, 1.0, 1.0, 0.0, 1.0] for i in range(40, 0, -1)]
            bars[-1] = [float(ts), 1.0, 1.0, 1.0, marker, 1.0]
            bars[-2] = [float(ts - 1), 1.0, 1.0, 1.0, marker, 1.0]
            return bars

        def fake_rsi(closes, period=7):
            return list(daily_series) if closes[-1] == DAILY else list(h4_series)

        monkeypatch.setattr(scan_mod, "bars_for", fake_bars_for)
        monkeypatch.setattr(scan_mod, "wilder_rsi", fake_rsi)

        cfg = Config.from_env({})
        cfg.symbols = list(symbols)
        cfg.request_delay_seconds = 0.0
        for k, v in cfg_kw.items():
            setattr(cfg, k, v)
        return scan_mod.scan(cfg, now_ms=now_ms, log=lambda *a: None)

    return setup


def pad(*tail):
    """A 40-long series whose last entries are the ones that matter."""
    return [50.0] * (40 - len(tail)) + list(tail)


FRESH = DAY0 + MS_PER_DAY + 600_000          # 10 min after both bars closed


# --------------------------------------------------------------------------
# The confluence gate
# --------------------------------------------------------------------------

def test_4h_bullish_fires_when_daily_is_also_bullish(wiring):
    """Daily above the midline, 4H crossing up out of oversold. Buy the dip."""
    result = wiring(daily_series=pad(60.0, 62.0), h4_series=pad(26.0, 33.0), now_ms=FRESH)
    h4 = result.confirmed_4h()
    assert len(h4) == 1 and h4[0].direction == "bullish"
    assert h4[0].daily_confirmed is True
    assert result.unconfirmed_4h() == []


def test_4h_bullish_against_a_bearish_daily_is_reported_but_flagged(wiring):
    """
    A bounce inside a downtrend. Still reported -- the user asked to see these
    -- but it must land in the counter-trend bucket, not alongside the ones
    that passed the filter.
    """
    result = wiring(daily_series=pad(40.0, 38.0), h4_series=pad(26.0, 33.0), now_ms=FRESH)
    assert result.confirmed_4h() == []
    assert len(result.unconfirmed_4h()) == 1
    assert result.unconfirmed_4h()[0].daily_confirmed is False
    assert result.suppressed == 0


def test_4h_bearish_fires_when_daily_is_also_bearish(wiring):
    result = wiring(daily_series=pad(40.0, 38.0), h4_series=pad(74.0, 68.0), now_ms=FRESH)
    h4 = result.confirmed_4h()
    assert len(h4) == 1 and h4[0].direction == "bearish"


def test_4h_bearish_against_a_bullish_daily_is_reported_but_flagged(wiring):
    result = wiring(daily_series=pad(60.0, 62.0), h4_series=pad(74.0, 68.0), now_ms=FRESH)
    assert result.confirmed_4h() == []
    assert len(result.unconfirmed_4h()) == 1


def test_turning_the_toggle_off_restores_suppression(wiring):
    """ALERT_4H_UNCONFIRMED=false goes back to agreeing-only, ~5 alerts/day."""
    result = wiring(daily_series=pad(40.0, 38.0), h4_series=pad(26.0, 33.0),
                    now_ms=FRESH, alert_4h_unconfirmed=False)
    assert result.for_tf("4H") == []
    assert result.suppressed == 1


def test_daily_exactly_on_the_midline_confirms_nothing(wiring):
    """No bias either way, so there is nothing for a 4H reset to agree with."""
    result = wiring(daily_series=pad(50.0, 50.0), h4_series=pad(26.0, 33.0), now_ms=FRESH)
    assert result.confirmed_4h() == []
    assert len(result.unconfirmed_4h()) == 1


def test_4h_signal_carries_the_daily_rsi_for_context(wiring):
    result = wiring(daily_series=pad(60.0, 63.5), h4_series=pad(26.0, 33.0), now_ms=FRESH)
    assert result.confirmed_4h()[0].daily_rsi == pytest.approx(63.5)


def test_daily_signal_is_not_gated_by_anything(wiring):
    """1D stands on its own merits -- there is no higher timeframe to consult."""
    result = wiring(daily_series=pad(26.0, 33.0), h4_series=pad(50.0, 50.0), now_ms=FRESH)
    assert len(result.for_tf("1D")) == 1


def test_custom_midline_moves_the_gate(wiring):
    """Daily 55 is bullish at midline 50, bearish at midline 60."""
    kw = dict(daily_series=pad(55.0, 55.0), h4_series=pad(26.0, 33.0), now_ms=FRESH)
    assert len(wiring(**kw, bias_midline=50.0).confirmed_4h()) == 1
    assert wiring(**kw, bias_midline=60.0).confirmed_4h() == []


# --------------------------------------------------------------------------
# The duplicate guard
# --------------------------------------------------------------------------

def test_fresh_bars_are_reported(wiring):
    result = wiring(daily_series=pad(26.0, 33.0), h4_series=pad(50.0, 50.0), now_ms=FRESH)
    assert set(result.reported) == {"1D", "4H"}


def test_stale_daily_bar_is_not_reported(wiring):
    """
    The production bug: a schedule firing every ten minutes re-read the same
    closed daily bar all day and re-posted it every time.
    """
    eight_hours_late = DAY0 + MS_PER_DAY + 8 * 3_600_000
    result = wiring(daily_series=pad(26.0, 33.0), h4_series=pad(50.0, 50.0),
                    now_ms=eight_hours_late)
    assert "1D" not in result.reported
    assert result.for_tf("1D") == []
    assert result.stale["1D"] > 90


def test_a_run_between_bar_closes_reports_nothing(wiring):
    """Both bars long closed. Nothing is new, so the digest must not post."""
    late = DAY0 + MS_PER_DAY + 3 * 3_600_000
    result = wiring(daily_series=pad(26.0, 33.0), h4_series=pad(26.0, 33.0), now_ms=late)
    assert result.reported == []
    assert result.signals == []


def test_4h_can_be_fresh_while_the_daily_is_stale(wiring):
    """The 04:00 run: new 4H information, no new daily."""
    h4_ts = DAY0 + MS_PER_DAY          # the 00:00-04:00 bar
    now = h4_ts + MS_PER_4H + 300_000  # 5 min after it closed
    result = wiring(daily_series=pad(60.0, 62.0), h4_series=pad(26.0, 33.0),
                    now_ms=now, h4_ts=h4_ts)
    assert result.reported == ["4H"]
    assert len(result.for_tf("4H")) == 1


def test_widening_the_window_lets_a_late_run_through(wiring):
    """GitHub Actions is routinely 30 min late; that must not be a silent miss."""
    late = DAY0 + MS_PER_DAY + 100 * 60_000     # 100 minutes after close
    assert wiring(daily_series=pad(26.0, 33.0), h4_series=pad(50.0, 50.0),
                  now_ms=late).reported == []
    assert "1D" in wiring(daily_series=pad(26.0, 33.0), h4_series=pad(50.0, 50.0),
                          now_ms=late, max_bar_age_minutes=120.0).reported


def test_digest_dates_from_the_reported_bar_not_the_daily(wiring):
    """
    On a 04:00 run only the 4H bar is news. Stamping the digest with the
    daily's date makes fresh information look a day old.
    """
    h4_ts = DAY0 + MS_PER_DAY
    now = h4_ts + MS_PER_4H + 300_000
    result = wiring(daily_series=pad(60.0, 62.0), h4_series=pad(26.0, 33.0),
                    now_ms=now, h4_ts=h4_ts)
    assert result.reported_bar_ts_ms == h4_ts
    assert result.reported_bar_ts_ms != result.last_bar_ts_ms
