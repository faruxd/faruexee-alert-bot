"""
Bar hygiene: the forming-bar drop and the 4H -> UTC-day resample.

These two are where a daily scanner actually goes wrong. The RSI maths is
easy to verify; "which bar am I looking at" is the part that silently reads
a half-formed candle and alerts on a signal that does not exist yet.
"""

from rsi_scanner.bitget import (
    MS_PER_4H,
    MS_PER_DAY,
    drop_forming_bar,
    resample_4h_to_utc_days,
)

DAY0 = 1_700_000_000_000 // MS_PER_DAY * MS_PER_DAY   # an exact UTC midnight


def bar(ts, o=1.0, h=2.0, lo=0.5, c=1.5, v=10.0):
    return [float(ts), o, h, lo, c, v]


# --------------------------------------------------------------------------
# drop_forming_bar
# --------------------------------------------------------------------------

def test_drops_the_still_forming_daily_bar():
    """The classic bug: Bitget's last daily row is the bar in progress."""
    bars = [bar(DAY0 - MS_PER_DAY), bar(DAY0)]
    now = DAY0 + 12 * 3_600_000          # midway through the DAY0 bar
    kept = drop_forming_bar(bars, MS_PER_DAY, now_ms=now)
    assert [b[0] for b in kept] == [float(DAY0 - MS_PER_DAY)]


def test_keeps_a_bar_that_closed_exactly_now():
    bars = [bar(DAY0)]
    kept = drop_forming_bar(bars, MS_PER_DAY, now_ms=DAY0 + MS_PER_DAY)
    assert len(kept) == 1


def test_keeps_everything_when_all_bars_are_closed():
    bars = [bar(DAY0 - MS_PER_DAY * 2), bar(DAY0 - MS_PER_DAY)]
    kept = drop_forming_bar(bars, MS_PER_DAY, now_ms=DAY0 + MS_PER_DAY)
    assert len(kept) == 2


def test_result_is_a_copy_not_an_alias():
    """Callers mutate the result; the input must not change under them."""
    bars = [bar(DAY0 - MS_PER_DAY)]
    kept = drop_forming_bar(bars, MS_PER_DAY, now_ms=DAY0 + MS_PER_DAY)
    kept[0][4] = 999.0
    assert bars[0][4] == 1.5


# --------------------------------------------------------------------------
# resample_4h_to_utc_days
# --------------------------------------------------------------------------

def full_day(day_start, base):
    """Six 4H bars covering one whole UTC day."""
    return [
        bar(day_start + i * MS_PER_4H, o=base + i, h=base + i + 5,
            lo=base + i - 5, c=base + i + 1, v=100.0)
        for i in range(6)
    ]


def test_six_bars_fold_into_one_day_with_correct_ohlcv():
    bars = full_day(DAY0, 100.0)
    days = resample_4h_to_utc_days(bars)
    assert len(days) == 1
    ts, o, h, lo, c, v = days[0]
    assert ts == float(DAY0)
    assert o == 100.0                       # open of the FIRST 4H bar
    assert c == 106.0                       # close of the LAST 4H bar
    assert h == max(b[2] for b in bars)
    assert lo == min(b[3] for b in bars)
    assert v == 600.0                       # summed, not averaged


def test_partial_day_is_dropped_not_emitted_as_a_short_bar():
    """
    A day with five 4H bars has a 'close' that is really a 20:00 price. Using
    it would put a phantom bar into the RSI series.
    """
    bars = full_day(DAY0, 100.0)[:5] + full_day(DAY0 + MS_PER_DAY, 200.0)
    days = resample_4h_to_utc_days(bars)
    assert [d[0] for d in days] == [float(DAY0 + MS_PER_DAY)]


def test_days_come_back_in_chronological_order():
    bars = full_day(DAY0 + MS_PER_DAY, 200.0) + full_day(DAY0, 100.0)
    days = resample_4h_to_utc_days(bars)
    assert [d[0] for d in days] == [float(DAY0), float(DAY0 + MS_PER_DAY)]


def test_unsorted_bars_within_a_day_still_open_and_close_correctly():
    bars = list(reversed(full_day(DAY0, 100.0)))
    days = resample_4h_to_utc_days(bars)
    assert days[0][1] == 100.0   # open
    assert days[0][4] == 106.0   # close


def test_empty_input_is_empty_output():
    assert resample_4h_to_utc_days([]) == []
