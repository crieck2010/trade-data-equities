"""Market-data provider backed by yfinance (free, delayed quotes).

yfinance is an *optional* dependency -- it is imported lazily so the core
engine stays importable without it::

    pip install trade-data-equities[yfinance]

Data is delayed ~15 minutes and intended for research, backtesting, and
paper trading -- not for live execution.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

from ..exceptions import ProviderError, RateLimitError, SymbolNotFoundError
from ..models import Bar, Dividend, Split, Symbol, Timeframe, ensure_utc
from .base import DateLike, MarketDataProvider

_INTERVALS = {
    Timeframe.M1: "1m",
    Timeframe.M5: "5m",
    Timeframe.M15: "15m",
    Timeframe.H1: "1h",
    Timeframe.DAILY: "1d",
    Timeframe.WEEKLY: "1wk",
    Timeframe.MONTHLY: "1mo",
}

# Longest span Yahoo serves per request for each granularity.
_WINDOWS = {
    Timeframe.M1: timedelta(days=7),
    Timeframe.M5: timedelta(days=60),
    Timeframe.M15: timedelta(days=60),
    Timeframe.H1: timedelta(days=730),
    Timeframe.DAILY: None,
    Timeframe.WEEKLY: None,
    Timeframe.MONTHLY: None,
}


def _flatten_columns(df):
    """Collapse yfinance's MultiIndex columns to plain OHLCV names."""
    cols = df.columns
    if getattr(cols, "nlevels", 1) > 1:
        df = df.copy()
        df.columns = [str(c[0]).strip().lower() for c in cols]
    else:
        df = df.copy()
        df.columns = [str(c).strip().lower() for c in cols]
    return df


def bars_from_frame(df) -> list[Bar]:
    """Convert a yfinance-style OHLCV DataFrame to :class:`Bar` list.

    Kept as a module-level pure function so it is unit-testable without
    network access. Expects columns open/high/low/close/volume (case
    insensitive, MultiIndex tolerated) and a tz-aware DatetimeIndex.
    """
    df = _flatten_columns(df)
    bars: list[Bar] = []
    for ts, row in df.iterrows():
        stamp = ts.tz_convert("UTC").to_pydatetime() if ts.tzinfo else ts.to_pydatetime().replace(
            tzinfo=timezone.utc
        )
        try:
            bars.append(
                Bar(
                    timestamp=stamp,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume", 0.0) or 0.0),
                )
            )
        except (KeyError, TypeError, ValueError):
            # Skip malformed rows (e.g. NaN holidays) rather than failing
            # the whole fetch; gaps are the caller's domain.
            continue
    bars.sort(key=lambda b: b.timestamp)
    return bars


class YFinanceProvider(MarketDataProvider):
    """Free delayed equity data via the yfinance package."""

    name = "yfinance"
    delay_minutes = 15

    def __init__(self, min_interval: float = 0.5, max_retries: int = 3) -> None:
        """
        :param min_interval: minimum seconds between HTTP calls (politeness).
        :param max_retries: attempts per request before giving up.
        """
        self.min_interval = min_interval
        self.max_retries = max_retries
        self._last_call = 0.0

    # -- internals ----------------------------------------------------
    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _ticker(self, symbol: Symbol):
        try:
            import yfinance as yf
        except ImportError as exc:  # pragma: no cover - exercised in docs
            raise ProviderError(
                "yfinance is not installed; run `pip install trade-data-equities[yfinance]`"
            ) from exc
        return yf.Ticker(symbol.ticker)

    def _call(self, func, *args, **kwargs):
        last: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                return func(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - vendor errors vary
                last = exc
                if "429" in str(exc) or "rate" in str(exc).lower():
                    raise RateLimitError(f"yfinance rate limit hit: {exc}") from exc
                time.sleep(2**attempt)
        raise ProviderError(
            f"yfinance request failed after {self.max_retries} attempts: {last}"
        ) from last

    # -- MarketDataProvider -------------------------------------------
    def max_window(self, timeframe: Timeframe) -> timedelta | None:
        return _WINDOWS[timeframe]

    def get_bars(
        self, symbol: Symbol, timeframe: Timeframe, start: DateLike, end: DateLike
    ) -> list[Bar]:
        ticker = self._ticker(symbol)
        df = self._call(
            ticker.history,
            start=start if isinstance(start, datetime) else start.isoformat(),
            end=end if isinstance(end, datetime) else end.isoformat(),
            interval=_INTERVALS[timeframe],
            auto_adjust=False,
            actions=False,
        )
        if df is None or len(df) == 0:
            raise SymbolNotFoundError(
                f"yfinance returned no {timeframe.value} bars for {symbol.ticker}"
            )
        return bars_from_frame(df)

    def _actions(self, symbol: Symbol, start: DateLike, end: DateLike, kind: str):
        ticker = self._ticker(symbol)
        actions = self._call(ticker.get_actions)
        items = []
        start_d = start.date() if isinstance(start, datetime) else start
        end_d = end.date() if isinstance(end, datetime) else end
        for ts, row in actions.iterrows():
            day = ensure_utc(ts.to_pydatetime()).date() if ts.tzinfo else ts.date()
            if not (start_d <= day < end_d):
                continue
            if kind == "splits" and float(row.get("Stock Splits", 0) or 0) > 0:
                items.append(Split(ex_date=day, ratio=float(row["Stock Splits"])))
            elif kind == "dividends" and float(row.get("Dividends", 0) or 0) > 0:
                items.append(Dividend(ex_date=day, amount=float(row["Dividends"])))
        items.sort(key=lambda a: a.ex_date)
        return items

    def get_splits(self, symbol: Symbol, start: DateLike, end: DateLike) -> list[Split]:
        return self._actions(symbol, start, end, "splits")

    def get_dividends(
        self, symbol: Symbol, start: DateLike, end: DateLike
    ) -> list[Dividend]:
        return self._actions(symbol, start, end, "dividends")
