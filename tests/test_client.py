"""Tests for the client: chunking, caching, merging, adjustment."""

from datetime import date, datetime, timedelta, timezone

import pytest

from trade_data_equities.cache import DiskCache
from trade_data_equities.client import EquitiesDataClient
from trade_data_equities.exceptions import ProviderError
from trade_data_equities.models import Bar, Split, Symbol, Timeframe
from trade_data_equities.providers import MarketDataProvider


class FakeProvider(MarketDataProvider):
    name = "fake"

    def __init__(self, bars, splits=None, window=None):
        self._bars = sorted(bars, key=lambda b: b.timestamp)
        self._splits = splits or []
        self._window = window
        self.calls = []

    def max_window(self, timeframe):
        return self._window

    def get_bars(self, symbol, timeframe, start, end):
        self.calls.append((symbol.ticker, start, end))
        return [b for b in self._bars if start <= b.timestamp < end]

    def get_splits(self, symbol, start, end):
        return [s for s in self._splits if start.date() <= s.ex_date < end.date()]


def _daily_bars(n, start_day=2, close0=100.0):
    bars = []
    base = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    for i in range(n):
        ts = base + timedelta(days=i)
        c = close0 + i
        bars.append(Bar(timestamp=ts, open=c - 1, high=c + 1, low=c - 2, close=c, volume=1000.0))
    return bars


def _client(provider, tmp_path, **kwargs):
    return EquitiesDataClient(provider, cache=DiskCache(root=tmp_path), **kwargs)


def test_long_range_is_chunked_and_merged(tmp_path):
    provider = FakeProvider(_daily_bars(75), window=timedelta(days=30))
    client = _client(provider, tmp_path, auto_adjust=False)
    bars = client.get_bars("AAPL", Timeframe.DAILY, date(2024, 1, 2), date(2024, 3, 20))
    assert len(bars) == 75
    assert len(provider.calls) == 3  # 75 days / 30-day windows
    stamps = [b.timestamp for b in bars]
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == len(stamps)


def test_cache_serves_repeat_requests_without_provider_calls(tmp_path):
    provider = FakeProvider(_daily_bars(5))
    client = _client(provider, tmp_path, auto_adjust=False)
    first = client.get_bars("AAPL", Timeframe.DAILY, date(2024, 1, 2), date(2024, 1, 10))
    assert len(provider.calls) == 1
    second = client.get_bars("AAPL", Timeframe.DAILY, date(2024, 1, 2), date(2024, 1, 10))
    assert len(provider.calls) == 1
    assert [b.close for b in first] == [b.close for b in second]


def test_bypass_cache_still_fetches(tmp_path):
    provider = FakeProvider(_daily_bars(5))
    client = _client(provider, tmp_path, auto_adjust=False)
    client.get_bars("AAPL", Timeframe.DAILY, date(2024, 1, 2), date(2024, 1, 10), use_cache=False)
    client.get_bars("AAPL", Timeframe.DAILY, date(2024, 1, 2), date(2024, 1, 10), use_cache=False)
    assert len(provider.calls) == 2


def test_adjustment_applied_on_read_and_not_cached(tmp_path):
    bars = _daily_bars(4)  # closes 100..103, dates 01-02..01-05
    splits = [Split(ex_date=date(2024, 1, 4), ratio=2.0)]
    provider = FakeProvider(bars, splits=splits)
    client = _client(provider, tmp_path, auto_adjust=True)
    out = client.get_bars("AAPL", Timeframe.DAILY, date(2024, 1, 2), date(2024, 1, 6))
    assert [b.close for b in out] == [50.0, 50.5, 102.0, 103.0]
    # Raw cached copy is untouched by adjustment.
    raw = client.cache.get_bars("fake", Symbol("AAPL"), Timeframe.DAILY,
                                datetime(2024, 1, 2, tzinfo=timezone.utc),
                                datetime(2024, 1, 6, tzinfo=timezone.utc))
    assert raw is None or [b.close for b in raw] == [100.0, 101.0, 102.0, 103.0]


def test_end_before_start_raises(tmp_path):
    client = _client(FakeProvider([]), tmp_path)
    with pytest.raises(ValueError):
        client.get_bars("AAPL", Timeframe.DAILY, date(2024, 2, 1), date(2024, 1, 1))


def test_unsupported_timeframe_raises(tmp_path):
    class Nope(MarketDataProvider):
        name = "nope"

        def get_bars(self, symbol, timeframe, start, end):
            return []

        def supports(self, timeframe):
            return False

    client = _client(Nope(), tmp_path)
    with pytest.raises(ProviderError):
        client.get_bars("AAPL", Timeframe.M1, date(2024, 1, 1), date(2024, 1, 2))


def test_stream_bars_yields_chunks(tmp_path):
    provider = FakeProvider(_daily_bars(65), window=timedelta(days=30))
    client = _client(provider, tmp_path, auto_adjust=False)
    chunks = list(client.stream_bars("AAPL", Timeframe.DAILY, date(2024, 1, 2), date(2024, 3, 10)))
    assert len(chunks) == 3
    assert sum(len(c) for c in chunks) == 65


def test_to_dataframe(tmp_path):
    pd = pytest.importorskip("pandas")
    client = _client(FakeProvider([]), tmp_path)
    df = client.to_dataframe(_daily_bars(3))
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 3
    assert str(df.index.tz) == "UTC"


def test_string_symbol_is_accepted(tmp_path):
    provider = FakeProvider(_daily_bars(2))
    client = _client(provider, tmp_path, auto_adjust=False)
    bars = client.get_bars("aapl", Timeframe.DAILY, date(2024, 1, 2), date(2024, 1, 5))
    assert len(bars) == 2
    assert provider.calls[0][0] == "AAPL"
