"""Tests for the Polygon provider. Everything runs offline against MockPolygonHTTP.

No test in this file touches the network or needs POLYGON_API_KEY.
"""

from datetime import date, datetime, timezone

import pytest

from trade_data_equities import EquitiesDataClient
from trade_data_equities.cache import DiskCache
from trade_data_equities.exceptions import (
    ProviderError,
    RateLimitError,
    SymbolNotFoundError,
)
from trade_data_equities.models import Bar, Symbol, Timeframe
from trade_data_equities.providers.polygon import (
    TIMEFRAME_MAP,
    MockPolygonHTTP,
    PolygonProvider,
    _redact,
    _retry_delay,
    bars_from_polygon,
)


def _provider(mock=None, **kwargs):
    kwargs.setdefault("api_key", "test-key")
    kwargs.setdefault("min_interval", 0.0)  # no politeness sleeps in tests
    if mock is not None:
        kwargs["transport"] = mock
    return PolygonProvider(**kwargs)


def _aggs_payload(rows, next_url=None):
    payload = {"status": "OK", "results": rows}
    if next_url:
        payload["next_url"] = next_url
    return payload


def _row(t_ms, o, h, l, c, v=1000):
    return {"t": t_ms, "o": o, "h": h, "l": l, "c": c, "v": v, "vw": c, "n": 10}


def _daily_rows(n, start_ms=1704067200000):  # 2024-01-01T00:00Z
    rows = []
    for i in range(n):
        t = start_ms + i * 86_400_000
        c = 100.0 + i
        rows.append(_row(t, c - 1, c + 1, c - 2, c, 1000 + i))
    return rows


# -- mapping -----------------------------------------------------------

def test_timeframe_mapping_covers_every_timeframe():
    assert set(TIMEFRAME_MAP) == set(Timeframe)
    assert TIMEFRAME_MAP[Timeframe.M1] == (1, "minute")
    assert TIMEFRAME_MAP[Timeframe.M5] == (5, "minute")
    assert TIMEFRAME_MAP[Timeframe.M15] == (15, "minute")
    assert TIMEFRAME_MAP[Timeframe.H1] == (1, "hour")
    assert TIMEFRAME_MAP[Timeframe.DAILY] == (1, "day")
    assert TIMEFRAME_MAP[Timeframe.WEEKLY] == (1, "week")
    assert TIMEFRAME_MAP[Timeframe.MONTHLY] == (1, "month")


def test_url_construction():
    mock = MockPolygonHTTP().script_json(_aggs_payload(_daily_rows(2)))
    p = _provider(mock)
    bars = p.get_bars(Symbol("aapl"), Timeframe.DAILY, date(2024, 1, 1), date(2024, 1, 3))
    assert len(bars) == 2
    url = mock.requests[0]
    assert url.startswith("https://api.massive.com/v2/aggs/ticker/AAPL/range/1/day/2024-01-01/2024-01-03")
    assert "adjusted=false" in url  # contract: provider returns UNADJUSTED bars
    assert "sort=asc" in url
    assert "limit=50000" in url
    assert "apiKey=test-key" in url


def test_intraday_url_uses_multiplier_timespan():
    mock = MockPolygonHTTP().script_json(_aggs_payload([_row(1704067200000, 1, 2, 0.5, 1.5)]))
    p = _provider(mock)
    p.get_bars(Symbol("MSFT"), Timeframe.M15, date(2024, 1, 2), date(2024, 1, 3))
    assert "/range/15/minute/" in mock.requests[0]


def test_default_base_url_is_massive():
    p = _provider()
    assert p.base_url == "https://api.massive.com"


def test_base_url_env_override(monkeypatch):
    monkeypatch.setenv("POLYGON_BASE_URL", "https://api.polygon.io")
    p = _provider()
    assert p.base_url == "https://api.polygon.io"


def test_explicit_api_key_used():
    p = PolygonProvider(api_key="explicit", min_interval=0.0)
    assert p._api_key == "explicit"


def test_api_key_env_actually_read(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "env-key-123")
    p = PolygonProvider(min_interval=0.0)
    assert p._api_key == "env-key-123"


def test_missing_api_key_raises_clear_error(monkeypatch):
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="POLYGON_API_KEY"):
        PolygonProvider(min_interval=0.0)


# -- pagination --------------------------------------------------------

def test_multi_page_pagination_assembles_in_order():
    page2_url = "https://api.massive.com/v2/aggs/ticker/AAPL/range/1/day/x?cursor=page2"
    mock = (
        MockPolygonHTTP()
        .script_json(_aggs_payload(_daily_rows(2), next_url=page2_url))
        .script_json(_aggs_payload(_daily_rows(2, start_ms=1704240000000)))
    )
    p = _provider(mock)
    bars = p.get_bars(Symbol("AAPL"), Timeframe.DAILY, date(2024, 1, 1), date(2024, 1, 5))
    assert len(bars) == 4
    assert [b.timestamp for b in bars] == sorted(b.timestamp for b in bars)
    # absolute next_url used verbatim, key appended, base NOT doubled
    assert mock.requests[1].startswith(page2_url)
    assert "api.massive.com/v2/aggs/ticker/AAPL/range/1/day/x?cursor=page2&apiKey=" in mock.requests[1]
    assert "apiKey=test-key" in mock.requests[1]
    assert "adjusted=false" in mock.requests[0]


def test_pagination_cap_raises_instead_of_partial_data():
    nxt = "https://api.massive.com/v2/aggs/c?cursor=forever"
    mock = MockPolygonHTTP()
    for _ in range(4):
        mock.script_json(_aggs_payload(_daily_rows(1), next_url=nxt))
    p = _provider(mock, max_pages=3)
    with pytest.raises(ProviderError, match="exceeded 3 pages"):
        p.get_bars(Symbol("AAPL"), Timeframe.DAILY, date(2024, 1, 1), date(2024, 2, 1))


# -- rate limits / errors ----------------------------------------------

def test_429_then_success_retries(monkeypatch):
    sleeps = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    mock = (
        MockPolygonHTTP()
        .script(429, {"status": "ERROR"}, headers={"Retry-After": "7"})
        .script_json(_aggs_payload(_daily_rows(1)))
    )
    p = _provider(mock)
    bars = p.get_bars(Symbol("AAPL"), Timeframe.DAILY, date(2024, 1, 1), date(2024, 1, 2))
    assert len(bars) == 1
    assert len(mock.requests) == 2
    assert sleeps == [7.0]  # Retry-After honored exactly


def test_429_exhaustion_raises_rate_limit(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    mock = MockPolygonHTTP()
    for _ in range(5):
        mock.script(429, {"status": "ERROR"})
    p = _provider(mock, max_retries=3)
    with pytest.raises(RateLimitError):
        p.get_bars(Symbol("AAPL"), Timeframe.DAILY, date(2024, 1, 1), date(2024, 1, 2))
    assert len(mock.requests) == 3


def test_retry_delay_bounds_and_retry_after():
    assert _retry_delay(0, "5") == 5.0
    assert _retry_delay(0, "99999") < 61.0  # insane Retry-After ignored, capped
    assert _retry_delay(0, "bogus") >= 1.0
    for attempt in range(6):
        d = _retry_delay(attempt, None)
        assert 2**attempt <= d <= 2**attempt + 1.0
    assert _retry_delay(99, None) <= 61.0  # capped


def test_401_never_leaks_key():
    mock = MockPolygonHTTP().script(401, {"status": "ERROR", "error": "bad key"})
    p = _provider(mock)
    with pytest.raises(ProviderError) as excinfo:
        p.get_bars(Symbol("AAPL"), Timeframe.DAILY, date(2024, 1, 1), date(2024, 1, 2))
    assert "test-key" not in str(excinfo.value)
    assert "401" in str(excinfo.value)


def test_empty_results_raise_symbol_not_found():
    mock = MockPolygonHTTP().script_json({"status": "OK", "results": []})
    p = _provider(mock)
    with pytest.raises(SymbolNotFoundError):
        p.get_bars(Symbol("NOPE"), Timeframe.DAILY, date(2024, 1, 1), date(2024, 1, 2))


def test_redact_hides_key():
    assert "test-key" not in _redact("https://x/y?adjusted=false&apiKey=test-key")
    assert "apiKey=***" in _redact("https://x/y?apiKey=test-key")
    assert _redact("https://x/y?nobody=here") == "https://x/y?nobody=here"


def test_repr_redacts_key():
    assert "test-key" not in repr(_provider())


# -- bar mapping --------------------------------------------------------

def test_bar_mapping_and_canonical_shape():
    rows = _daily_rows(3)
    bars = bars_from_polygon(rows)
    assert len(bars) == 3
    assert all(isinstance(b, Bar) for b in bars)
    first = bars[0]
    assert first.timestamp == datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert (first.open, first.high, first.low, first.close) == (99.0, 101.0, 98.0, 100.0)
    assert first.volume == 1000.0
    assert all(b.timestamp.tzinfo is not None for b in bars)
    assert [b.timestamp for b in bars] == sorted(b.timestamp for b in bars)


def test_bar_shape_parity_with_yfinance_contract():
    """Same Bar dataclass, same keys, same ascending/UTC invariants."""
    rows = _daily_rows(2)
    for b in bars_from_polygon(rows):
        assert isinstance(b.timestamp, datetime)
        assert b.high >= max(b.open, b.low, b.close)
        assert b.low <= min(b.open, b.high, b.close)
        assert b.volume >= 0


def test_missing_volume_defaults_to_zero():
    row = {"t": 1704067200000, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5}
    (bar,) = bars_from_polygon([row])
    assert bar.volume == 0.0


def test_malformed_rows_skipped_not_fatal():
    rows = _daily_rows(2) + [{"t": "not-a-time", "o": 1}, {"o": 1.0}]  # junk
    bars = bars_from_polygon(rows)
    assert len(bars) == 2


def test_weekend_gaps_pass_through_unfilled():
    # Friday then Monday: provider returns what the server returns, no filling.
    fri = _row(1704412800000, 99, 101, 98, 100)  # 2024-01-05 Fri
    mon = _row(1704672000000, 100, 102, 99, 101)  # 2024-01-08 Mon
    bars = bars_from_polygon([fri, mon])
    assert len(bars) == 2
    assert (bars[1].timestamp - bars[0].timestamp).days == 3


# -- corporate actions --------------------------------------------------

def test_get_splits_mapping():
    payload = {
        "status": "OK",
        "results": [
            {"ticker": "AAPL", "execution_date": "2020-08-31", "split_from": 1, "split_to": 4}
        ],
    }
    mock = MockPolygonHTTP().script_json(payload)
    p = _provider(mock)
    splits = p.get_splits(Symbol("AAPL"), date(2020, 1, 1), date(2021, 1, 1))
    assert len(splits) == 1
    assert splits[0].ex_date == date(2020, 8, 31)
    assert splits[0].ratio == 4.0
    assert "execution_date.gte=2020-01-01" in mock.requests[0]


def test_get_splits_403_degrades_to_empty():
    mock = MockPolygonHTTP().script(403, {"status": "ERROR"})
    p = _provider(mock)
    assert p.get_splits(Symbol("AAPL"), date(2020, 1, 1), date(2021, 1, 1)) == []


def test_get_dividends_mapping():
    payload = {
        "status": "OK",
        "results": [
            {"ticker": "AAPL", "ex_dividend_date": "2024-02-09", "cash_amount": 0.24}
        ],
    }
    mock = MockPolygonHTTP().script_json(payload)
    p = _provider(mock)
    divs = p.get_dividends(Symbol("AAPL"), date(2024, 1, 1), date(2024, 3, 1))
    assert len(divs) == 1
    assert divs[0].ex_date == date(2024, 2, 9)
    assert divs[0].amount == 0.24


# -- client interop -----------------------------------------------------

def test_client_integration_chunks_and_merges(tmp_path):
    """The one-line swap: EquitiesDataClient works unchanged on Polygon."""
    base_ms = 1704067200000  # 2024-01-01T00:00Z
    mock = (
        MockPolygonHTTP()
        .script_json(_aggs_payload(_daily_rows(7, start_ms=base_ms)))
        .script_json(_aggs_payload(_daily_rows(7, start_ms=base_ms + 7 * 86_400_000)))
        .script_json(_aggs_payload(_daily_rows(6, start_ms=base_ms + 14 * 86_400_000)))
    )
    p = _provider(mock)
    client = EquitiesDataClient(p, cache=DiskCache(root=tmp_path), auto_adjust=False)
    bars = client.get_bars("AAPL", Timeframe.M1, date(2024, 1, 1), date(2024, 1, 21))
    assert len(mock.requests) == 3  # chunked by max_window
    assert len(bars) == 20  # 7 + 7 + 6, merged across chunk boundaries
    stamps = [b.timestamp for b in bars]
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == len(stamps)


def test_supports_all_timeframes():
    p = _provider()
    assert all(p.supports(tf) for tf in Timeframe)


def test_max_window_mirrors_yfinance_chunking():
    from datetime import timedelta

    p = _provider()
    assert p.max_window(Timeframe.M1) == timedelta(days=7)
    assert p.max_window(Timeframe.DAILY) is None
