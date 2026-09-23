"""Tests for the on-disk JSON cache."""

from datetime import date, datetime, timedelta, timezone

from trade_data_equities.cache import DiskCache
from trade_data_equities.models import Bar, Dividend, Split, Symbol, Timeframe


def _bars():
    return [
        Bar(timestamp=datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc), open=100, high=101, low=99, close=100.5, volume=1000),
        Bar(timestamp=datetime(2024, 1, 3, 14, 30, tzinfo=timezone.utc), open=100.5, high=102, low=100, close=101.5, volume=1100),
    ]


def test_bars_round_trip(tmp_path):
    cache = DiskCache(root=tmp_path)
    sym, tf = Symbol("AAPL"), Timeframe.DAILY
    assert cache.get_bars("p", sym, tf, date(2024, 1, 1), date(2024, 2, 1)) is None
    cache.put_bars("p", sym, tf, date(2024, 1, 1), date(2024, 2, 1), _bars())
    hit = cache.get_bars("p", sym, tf, date(2024, 1, 1), date(2024, 2, 1))
    assert hit is not None
    assert [b.close for b in hit] == [100.5, 101.5]
    assert hit[0].timestamp.tzinfo == timezone.utc


def test_expired_entries_are_misses(tmp_path):
    cache = DiskCache(root=tmp_path, ttl={"daily": timedelta(seconds=0)})
    sym, tf = Symbol("AAPL"), Timeframe.DAILY
    cache.put_bars("p", sym, tf, date(2024, 1, 1), date(2024, 2, 1), _bars())
    assert cache.get_bars("p", sym, tf, date(2024, 1, 1), date(2024, 2, 1)) is None


def test_keys_differ_by_provider_and_timeframe(tmp_path):
    cache = DiskCache(root=tmp_path)
    sym = Symbol("AAPL")
    k1 = cache.bars_key("p1", sym, Timeframe.DAILY, date(2024, 1, 1), date(2024, 2, 1))
    k2 = cache.bars_key("p2", sym, Timeframe.DAILY, date(2024, 1, 1), date(2024, 2, 1))
    k3 = cache.bars_key("p1", sym, Timeframe.WEEKLY, date(2024, 1, 1), date(2024, 2, 1))
    assert len({k1, k2, k3}) == 3


def test_corporate_actions_round_trip(tmp_path):
    cache = DiskCache(root=tmp_path)
    sym = Symbol("AAPL")
    splits = [Split(ex_date=date(2024, 8, 1), ratio=4.0)]
    dividends = [Dividend(ex_date=date(2024, 8, 2), amount=0.5)]
    cache.put_actions("p", sym, "splits", date(2024, 1, 1), date(2025, 1, 1), splits)
    cache.put_actions("p", sym, "dividends", date(2024, 1, 1), date(2025, 1, 1), dividends)
    assert cache.get_actions("p", sym, "splits", date(2024, 1, 1), date(2025, 1, 1)) == splits
    assert cache.get_actions("p", sym, "dividends", date(2024, 1, 1), date(2025, 1, 1)) == dividends
    assert cache.get_actions("p", sym, "splits", date(2023, 1, 1), date(2024, 1, 1)) is None


def test_clear_removes_files(tmp_path):
    cache = DiskCache(root=tmp_path)
    sym = Symbol("AAPL")
    cache.put_bars("p", sym, Timeframe.DAILY, date(2024, 1, 1), date(2024, 2, 1), _bars())
    assert cache.clear() == 1
    assert cache.get_bars("p", sym, Timeframe.DAILY, date(2024, 1, 1), date(2024, 2, 1)) is None
