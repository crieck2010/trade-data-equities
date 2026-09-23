"""Tests for universes and the yfinance adapter mapping."""

from datetime import datetime, timezone

import pytest

from trade_data_equities.models import InstrumentKind, Symbol, Timeframe
from trade_data_equities.providers import MarketDataProvider, YFinanceProvider, bars_from_frame
from trade_data_equities.universe import Universe


def test_from_tickers_dedupes_case_insensitively():
    u = Universe.from_tickers("tech", ["AAPL", "aapl", "MSFT"])
    assert u.tickers == ["AAPL", "MSFT"]
    assert len(u) == 2
    assert list(u)[0].kind is InstrumentKind.STOCK


def test_from_tickers_supports_etfs():
    u = Universe.from_tickers("funds", ["SPY", "QQQ"], kind=InstrumentKind.ETF)
    assert all(s.kind is InstrumentKind.ETF for s in u)


def test_from_csv(tmp_path):
    path = tmp_path / "watch.csv"
    path.write_text("ticker,kind,exchange,name\nAAPL,stock,NASDAQ,Apple Inc\nSPY,etf,ARCA,SPDR S&P 500\n")
    u = Universe.from_csv("watch", path)
    assert u.tickers == ["AAPL", "SPY"]
    assert u.symbols[1].kind is InstrumentKind.ETF
    assert u.symbols[0].exchange == "NASDAQ"


def test_select_and_without():
    u = Universe.from_tickers("all", ["AAPL", "MSFT", "SPY"], kind=InstrumentKind.STOCK)
    tech = u.select(lambda s: s.ticker != "SPY")
    assert tech.tickers == ["AAPL", "MSFT"]
    assert u.tickers == ["AAPL", "MSFT", "SPY"]  # original untouched
    assert u.without("msft").tickers == ["AAPL", "SPY"]


def test_empty_name_rejected():
    with pytest.raises(ValueError):
        Universe(name="  ", symbols=())


def test_yfinance_provider_conforms_to_protocol():
    provider = YFinanceProvider()
    assert isinstance(provider, MarketDataProvider)
    assert provider.name == "yfinance"
    assert provider.delay_minutes == 15
    assert provider.supports(Timeframe.DAILY)


def test_bars_from_frame_handles_multiindex_columns():
    pd = pytest.importorskip("pandas")
    idx = pd.DatetimeIndex(
        [
            datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc),
            datetime(2024, 1, 3, 14, 30, tzinfo=timezone.utc),
        ]
    )
    cols = pd.MultiIndex.from_product([["Open", "High", "Low", "Close", "Volume"], ["AAPL"]])
    df = pd.DataFrame(
        [
            [100.0, 101.0, 99.0, 100.5, 1000.0],
            [100.5, 102.0, 100.0, 101.5, 1100.0],
        ],
        index=idx,
        columns=cols,
    )
    bars = bars_from_frame(df)
    assert [b.close for b in bars] == [100.5, 101.5]
    assert [b.volume for b in bars] == [1000.0, 1100.0]
    assert all(b.timestamp.tzinfo == timezone.utc for b in bars)


def test_bars_from_frame_skips_malformed_rows():
    pd = pytest.importorskip("pandas")
    import math

    idx = pd.DatetimeIndex(
        [
            datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc),
            datetime(2024, 1, 3, 14, 30, tzinfo=timezone.utc),
        ]
    )
    df = pd.DataFrame(
        {
            "open": [100.0, math.nan],
            "high": [101.0, math.nan],
            "low": [99.0, math.nan],
            "close": [100.5, math.nan],
            "volume": [1000.0, 0.0],
        },
        index=idx,
    )
    bars = bars_from_frame(df)
    assert len(bars) == 1
    assert bars[0].close == 100.5


def test_yfinance_max_windows():
    provider = YFinanceProvider()
    assert provider.max_window(Timeframe.M1).days == 7
    assert provider.max_window(Timeframe.DAILY) is None
