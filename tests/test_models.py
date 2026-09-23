"""Tests for core models: validation, normalization, helpers."""

from datetime import date, datetime, timedelta, timezone

import pytest

from trade_data_equities.models import (
    Bar,
    Dividend,
    InstrumentKind,
    Quote,
    Split,
    Symbol,
    Timeframe,
    ensure_utc,
)


def _bar(ts, o, h, l, c, v=1000.0):
    return Bar(timestamp=ts, open=o, high=h, low=l, close=c, volume=v)


def test_symbol_normalizes_ticker():
    s = Symbol(ticker="  aapl ")
    assert s.ticker == "AAPL"
    assert s.kind is InstrumentKind.STOCK
    assert str(s) == "AAPL"


def test_symbol_rejects_empty_ticker():
    with pytest.raises(ValueError):
        Symbol(ticker="   ")


def test_symbol_rejects_bad_kind():
    with pytest.raises(ValueError):
        Symbol(ticker="AAPL", kind="stock")


def test_bar_coerces_naive_timestamp_to_utc():
    bar = _bar(datetime(2024, 1, 2, 14, 30), 100, 101, 99, 100.5)
    assert bar.timestamp.tzinfo == timezone.utc


def test_bar_converts_aware_timestamp_to_utc():
    eastern = timezone(timedelta(hours=-5))
    bar = _bar(datetime(2024, 1, 2, 9, 30, tzinfo=eastern), 100, 101, 99, 100.5)
    assert bar.timestamp == datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)


def test_bar_rejects_inconsistent_ohlc():
    ts = datetime(2024, 1, 2, tzinfo=timezone.utc)
    with pytest.raises(ValueError):  # high below close
        _bar(ts, 100, 99, 98, 100)
    with pytest.raises(ValueError):  # low above open
        _bar(ts, 100, 102, 101, 100)


def test_bar_rejects_bad_prices_and_volume():
    ts = datetime(2024, 1, 2, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        _bar(ts, 0, 1, 0.5, 1)
    with pytest.raises(ValueError):
        _bar(ts, 100, 101, 99, 100, v=-5)


def test_bar_helpers():
    bar = _bar(datetime(2024, 1, 2, tzinfo=timezone.utc), 100, 110, 90, 105, v=2000)
    assert bar.mid == 100.0
    assert bar.typical == pytest.approx((110 + 90 + 105) / 3)
    assert bar.dollar_volume == pytest.approx(105 * 2000)


def test_timeframe_intraday_flag():
    assert Timeframe.M1.is_intraday
    assert Timeframe.H1.is_intraday
    assert not Timeframe.DAILY.is_intraday
    assert not Timeframe.MONTHLY.is_intraday


def test_split_validation():
    Split(ex_date=date(2024, 8, 1), ratio=4.0)
    with pytest.raises(ValueError):
        Split(ex_date=date(2024, 8, 1), ratio=0)


def test_dividend_validation():
    Dividend(ex_date=date(2024, 8, 1), amount=0.5)
    with pytest.raises(ValueError):
        Dividend(ex_date=date(2024, 8, 1), amount=-1)


def test_ensure_utc_rejects_non_datetime():
    with pytest.raises(TypeError):
        ensure_utc("2024-01-02")


def test_quote_spread_and_mid():
    q = Quote(
        symbol=Symbol("AAPL"),
        timestamp=datetime(2024, 1, 2, tzinfo=timezone.utc),
        bid=100.0,
        ask=100.5,
        last=100.4,
    )
    assert q.spread == pytest.approx(0.5)
    assert q.mid == pytest.approx(100.25)
