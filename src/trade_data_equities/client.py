"""High-level client: chunking, caching, and adjustment orchestration.

:class:`EquitiesDataClient` is the primary entry point most callers use.
Given a :class:`MarketDataProvider` it:

* normalizes ``date``/``datetime`` bounds to UTC datetimes (``[start, end)``),
* splits long requests into provider-sized chunks
  (:meth:`MarketDataProvider.max_window`),
* serves each chunk from :class:`DiskCache` when fresh, otherwise fetches,
* merges chunks into one deduplicated, time-sorted series, and
* applies split/dividend adjustments on read when requested.

The client never mutates provider output and never writes adjusted bars
to the cache -- the cache always holds raw vendor data.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Iterator

from .adjustments import adjust_bars
from .cache import DiskCache
from .exceptions import ProviderError
from .models import Bar, Dividend, Split, Symbol, Timeframe, ensure_utc
from .providers.base import DateLike, MarketDataProvider


def _normalize_symbol(symbol: Symbol | str) -> Symbol:
    if isinstance(symbol, Symbol):
        return symbol
    return Symbol(ticker=symbol)


def _normalize_bound(value: DateLike) -> datetime:
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    raise TypeError(f"expected date or datetime, got {type(value).__name__}")


def _chunks(start: datetime, end: datetime, window: timedelta | None) -> list[tuple[datetime, datetime]]:
    if window is None or end - start <= window:
        return [(start, end)]
    out: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        nxt = min(cursor + window, end)
        out.append((cursor, nxt))
        cursor = nxt
    return out


class EquitiesDataClient:
    """Fetch, cache, and adjust equity bars through any provider."""

    def __init__(
        self,
        provider: MarketDataProvider,
        cache: DiskCache | None = None,
        auto_adjust: bool = True,
    ) -> None:
        """
        :param provider: the market-data source.
        :param cache: disk cache; a default one is created when omitted.
          Pass ``cache=None``... to disable, pass ``use_cache=False`` per call.
        :param auto_adjust: default for the ``adjusted`` flag on reads.
        """
        self.provider = provider
        self.cache = cache if cache is not None else DiskCache()
        self.auto_adjust = auto_adjust

    # -- corporate actions (cached) ------------------------------------
    def _actions(
        self, kind: str, symbol: Symbol, start: datetime, end: datetime, use_cache: bool
    ) -> list[Split] | list[Dividend]:
        if use_cache and self.cache is not None:
            hit = self.cache.get_actions(self.provider.name, symbol, kind, start, end)
            if hit is not None:
                return hit
        fetch = self.provider.get_splits if kind == "splits" else self.provider.get_dividends
        actions = fetch(symbol, start, end)
        if use_cache and self.cache is not None:
            self.cache.put_actions(self.provider.name, symbol, kind, start, end, actions)
        return actions

    # -- reads ----------------------------------------------------------
    def get_bars(
        self,
        symbol: Symbol | str,
        timeframe: Timeframe,
        start: DateLike,
        end: DateLike,
        *,
        adjusted: bool | None = None,
        use_cache: bool = True,
    ) -> list[Bar]:
        """Return bars for ``[start, end)``, ascending by timestamp.

        :param adjusted: apply split/dividend adjustments; defaults to the
          client's ``auto_adjust`` setting.
        :param use_cache: set False to bypass the cache (still writes
          through on fetch).
        """
        sym = _normalize_symbol(symbol)
        start_dt, end_dt = _normalize_bound(start), _normalize_bound(end)
        if end_dt <= start_dt:
            raise ValueError("end must be after start")
        if not self.provider.supports(timeframe):
            raise ProviderError(f"provider {self.provider.name!r} cannot serve {timeframe.value}")

        merged: dict[datetime, Bar] = {}
        for chunk_start, chunk_end in _chunks(start_dt, end_dt, self.provider.max_window(timeframe)):
            bars = self._chunk(sym, timeframe, chunk_start, chunk_end, use_cache)
            for bar in bars:
                if start_dt <= bar.timestamp < end_dt:
                    merged[bar.timestamp] = bar
        bars = [merged[ts] for ts in sorted(merged)]

        if adjusted if adjusted is not None else self.auto_adjust:
            splits = self._actions("splits", sym, start_dt, end_dt, use_cache)
            dividends = self._actions("dividends", sym, start_dt, end_dt, use_cache)
            bars = adjust_bars(bars, splits, dividends)  # type: ignore[arg-type]
        return bars

    def _chunk(
        self, symbol: Symbol, timeframe: Timeframe, start: datetime, end: datetime, use_cache: bool
    ) -> list[Bar]:
        if use_cache and self.cache is not None:
            hit = self.cache.get_bars(self.provider.name, symbol, timeframe, start, end)
            if hit is not None:
                return hit
        bars = self.provider.get_bars(symbol, timeframe, start, end)
        bars = sorted(bars, key=lambda b: b.timestamp)
        if use_cache and self.cache is not None:
            self.cache.put_bars(self.provider.name, symbol, timeframe, start, end, bars)
        return bars

    def stream_bars(
        self,
        symbol: Symbol | str,
        timeframe: Timeframe,
        start: DateLike,
        end: DateLike,
        *,
        adjusted: bool | None = None,
        use_cache: bool = True,
    ) -> Iterator[list[Bar]]:
        """Yield one chunk of bars at a time for very long ranges.

        Corporate actions are resolved once for the full range up front so
        per-chunk adjustment stays correct while memory stays bounded.
        """
        sym = _normalize_symbol(symbol)
        start_dt, end_dt = _normalize_bound(start), _normalize_bound(end)
        if end_dt <= start_dt:
            raise ValueError("end must be after start")
        want_adjusted = adjusted if adjusted is not None else self.auto_adjust
        splits = self._actions("splits", sym, start_dt, end_dt, use_cache) if want_adjusted else []
        dividends = self._actions("dividends", sym, start_dt, end_dt, use_cache) if want_adjusted else []
        for chunk_start, chunk_end in _chunks(start_dt, end_dt, self.provider.max_window(timeframe)):
            bars = [b for b in self._chunk(sym, timeframe, chunk_start, chunk_end, use_cache)
                    if start_dt <= b.timestamp < end_dt]
            if want_adjusted:
                bars = adjust_bars(bars, splits, dividends)  # type: ignore[arg-type]
            yield bars

    # -- convenience ------------------------------------------------------
    def get_splits(self, symbol: Symbol | str, start: DateLike, end: DateLike, *, use_cache: bool = True):
        sym = _normalize_symbol(symbol)
        return self._actions("splits", sym, _normalize_bound(start), _normalize_bound(end), use_cache)

    def get_dividends(self, symbol: Symbol | str, start: DateLike, end: DateLike, *, use_cache: bool = True):
        sym = _normalize_symbol(symbol)
        return self._actions("dividends", sym, _normalize_bound(start), _normalize_bound(end), use_cache)

    @staticmethod
    def to_dataframe(bars: list[Bar]):
        """Convert bars to a pandas DataFrame (requires pandas)."""
        try:
            import pandas as pd
        except ImportError as exc:
            raise ProviderError("pandas is not installed; run `pip install trade-data-equities[pandas]`") from exc
        return pd.DataFrame(
            {
                "open": [b.open for b in bars],
                "high": [b.high for b in bars],
                "low": [b.low for b in bars],
                "close": [b.close for b in bars],
                "volume": [b.volume for b in bars],
            },
            index=pd.DatetimeIndex([b.timestamp for b in bars], name="timestamp"),
        )
