"""Tests for corporate-action adjustments (hand-computed expectations)."""

from datetime import datetime, timezone, date

from trade_data_equities.adjustments import (
    adjust_bars,
    adjust_for_dividends,
    adjust_for_splits,
)
from trade_data_equities.models import Bar, Dividend, Split


def _daily_bars(closes):
    """One bar per day starting 2024-01-02 with close-driven OHLC."""
    bars = []
    for i, c in enumerate(closes):
        ts = datetime(2024, 1, 2 + i, 14, 30, tzinfo=timezone.utc)
        bars.append(Bar(timestamp=ts, open=c - 1, high=c + 1, low=c - 2, close=c, volume=1000.0))
    return bars


def test_split_halves_prices_and_doubles_volume_before_ex_date():
    bars = _daily_bars([100.0, 102.0, 104.0])
    splits = [Split(ex_date=date(2024, 1, 4), ratio=2.0)]  # ex on the 3rd bar's date
    out = adjust_for_splits(bars, splits)
    # Bars on 01-02 and 01-03 are before ex-date -> adjusted.
    assert out[0].close == 50.0
    assert out[0].volume == 2000.0
    assert out[1].close == 51.0
    # Bar on ex-date itself is not adjusted.
    assert out[2].close == 104.0
    assert out[2].volume == 1000.0


def test_reverse_split_multiplies_prices():
    bars = _daily_bars([10.0, 11.0])
    out = adjust_for_splits(bars, [Split(ex_date=date(2024, 1, 3), ratio=0.25)])
    assert out[0].close == 40.0
    assert out[0].volume == 250.0
    assert out[1].close == 11.0  # on/after ex-date: untouched


def test_multiple_splits_compound():
    bars = _daily_bars([100.0, 100.0, 100.0, 100.0])
    splits = [
        Split(ex_date=date(2024, 1, 3), ratio=2.0),
        Split(ex_date=date(2024, 1, 4), ratio=2.0),
    ]
    out = adjust_for_splits(bars, splits)
    assert out[0].close == 25.0  # both splits apply
    assert out[1].close == 50.0  # on the first ex-date: only the later split applies
    assert out[2].close == 100.0  # on the second ex-date: untouched
    assert out[3].close == 100.0


def test_dividend_scales_bars_before_ex_date():
    # Closes: 100, 102 then dividend of 2.0 ex on 2024-01-04.
    bars = _daily_bars([100.0, 102.0, 105.0])
    out = adjust_for_dividends(bars, [Dividend(ex_date=date(2024, 1, 4), amount=2.0)])
    factor = (102.0 - 2.0) / 102.0
    assert out[0].close == 100.0 * factor
    assert out[1].close == 102.0 * factor
    assert out[2].close == 105.0  # on/after ex-date: untouched
    # Volumes are never dividend-adjusted.
    assert out[0].volume == 1000.0


def test_dividend_skipped_without_prior_close_or_too_large():
    bars = _daily_bars([100.0])
    # Ex-date before any bar -> no prior close -> skipped.
    out = adjust_for_dividends(bars, [Dividend(ex_date=date(2024, 1, 1), amount=1.0)])
    assert out[0].close == 100.0
    # Amount larger than prior close -> skipped.
    out = adjust_for_dividends(bars, [Dividend(ex_date=date(2024, 1, 3), amount=500.0)])
    assert out[0].close == 100.0


def test_adjust_bars_applies_splits_before_dividends():
    # 2:1 split ex 01-03, then $1 dividend ex 01-04 on split-adjusted closes.
    bars = _daily_bars([100.0, 104.0, 52.0])
    out = adjust_bars(
        bars,
        splits=[Split(ex_date=date(2024, 1, 3), ratio=2.0)],
        dividends=[Dividend(ex_date=date(2024, 1, 4), amount=1.0)],
    )
    # After split: closes are 50, 104, 52 (only the 01-02 bar predates the
    # 01-03 ex-date). Dividend factor from the split-adjusted prior close:
    # (104 - 1) / 104.
    factor = 103.0 / 104.0
    assert out[0].close == 50.0 * factor
    assert out[1].close == 104.0 * factor
    assert out[2].close == 52.0


def test_adjustments_do_not_mutate_inputs():
    bars = _daily_bars([100.0, 102.0])
    original = list(bars)
    adjust_bars(bars, [Split(ex_date=date(2024, 1, 3), ratio=2.0)])
    assert [b.close for b in bars] == [b.close for b in original]


def test_empty_inputs_pass_through():
    assert adjust_for_splits([], [Split(ex_date=date(2024, 1, 3), ratio=2.0)]) == []
    assert adjust_for_dividends(_daily_bars([100.0]), [])[0].close == 100.0
