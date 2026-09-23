"""Core data models for equity market data.

These models are the shared vocabulary of the engine: providers produce
them, the cache serializes them, and downstream engines (backtesting,
strategies, risk) consume them. They are intentionally plain, immutable
dataclasses with no third-party dependencies so every sibling repository
can depend on them cheaply.

Design notes
------------
* All timestamps are timezone-aware UTC ``datetime`` objects. Naive inputs
  are assumed to be UTC rather than silently guessed.
* ``Bar`` validates OHLC consistency on construction: ``high`` must be the
  maximum and ``low`` the minimum of the four prices.
* Monetary values are ``float``. This engine is a market-data layer, not an
  accounting layer; exact-decimal bookkeeping belongs in the portfolio
  engine that consumes these bars.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from math import isfinite


class Timeframe(Enum):
    """Bar granularity supported by the engine."""

    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    DAILY = "1d"
    WEEKLY = "1wk"
    MONTHLY = "1mo"

    @property
    def is_intraday(self) -> bool:
        """True for sub-daily bar sizes."""
        return self in (Timeframe.M1, Timeframe.M5, Timeframe.M15, Timeframe.H1)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class InstrumentKind(Enum):
    """Equity-like instruments share one OHLCV data shape."""

    STOCK = "stock"
    ETF = "etf"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


def ensure_utc(value: datetime) -> datetime:
    """Return ``value`` as a tz-aware UTC datetime.

    Naive datetimes are assumed to already be UTC; aware datetimes are
    converted. Anything else raises :class:`TypeError`.
    """
    if not isinstance(value, datetime):
        raise TypeError(f"expected datetime, got {type(value).__name__}")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _check_price(name: str, value: float) -> float:
    if not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value!r}")
    return float(value)


@dataclass(frozen=True, slots=True)
class Symbol:
    """A tradable equity identifier.

    ``ticker`` is normalized to uppercase on construction. ``kind``
    distinguishes common stock from ETFs; both flow through the same
    OHLCV pipeline.
    """

    ticker: str
    kind: InstrumentKind = InstrumentKind.STOCK
    exchange: str | None = None
    name: str | None = None
    currency: str = "USD"

    def __post_init__(self) -> None:
        ticker = self.ticker.strip().upper()
        if not ticker:
            raise ValueError("ticker must be a non-empty string")
        object.__setattr__(self, "ticker", ticker)
        if not isinstance(self.kind, InstrumentKind):
            raise ValueError(f"kind must be an InstrumentKind, got {self.kind!r}")

    def __str__(self) -> str:
        return self.ticker


@dataclass(frozen=True, slots=True)
class Bar:
    """One OHLCV bar with a UTC timestamp.

    ``timestamp`` marks the **open** of the bar interval. The engine treats
    a bar series as the half-open range ``[timestamp, timestamp + interval)``.
    """

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", ensure_utc(self.timestamp))
        for name in ("open", "high", "low", "close"):
            _check_price(name, getattr(self, name))
        if not isinstance(self.volume, (int, float)) or not isfinite(self.volume):
            raise ValueError(f"volume must be a finite number, got {self.volume!r}")
        if self.volume < 0:
            raise ValueError(f"volume must be non-negative, got {self.volume!r}")
        object.__setattr__(self, "volume", float(self.volume))
        if self.high < max(self.open, self.high, self.low, self.close):
            raise ValueError("high must be >= open, low and close")
        if self.low > min(self.open, self.high, self.low, self.close):
            raise ValueError("low must be <= open, high and close")

    @property
    def mid(self) -> float:
        """Midpoint of high and low."""
        return (self.high + self.low) / 2.0

    @property
    def typical(self) -> float:
        """Typical price: (high + low + close) / 3."""
        return (self.high + self.low + self.close) / 3.0

    @property
    def dollar_volume(self) -> float:
        """Close * volume, a rough liquidity proxy."""
        return self.close * self.volume


@dataclass(frozen=True, slots=True)
class Quote:
    """A point-in-time quote snapshot."""

    symbol: Symbol
    timestamp: datetime
    bid: float
    ask: float
    last: float
    bid_size: float = 0.0
    ask_size: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", ensure_utc(self.timestamp))
        for name in ("bid", "ask", "last"):
            _check_price(name, getattr(self, name))

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


@dataclass(frozen=True, slots=True)
class Split:
    """A stock split corporate action.

    ``ratio`` is expressed as new-shares-per-old-share, so a 4-for-1 split
    is ``ratio=4.0``. Prices before ``ex_date`` are divided by ``ratio``
    and volumes multiplied by it.
    """

    ex_date: date
    ratio: float

    def __post_init__(self) -> None:
        if not isinstance(self.ex_date, date):
            raise TypeError("ex_date must be a datetime.date")
        if not isinstance(self.ratio, (int, float)) or not isfinite(self.ratio):
            raise ValueError(f"ratio must be finite, got {self.ratio!r}")
        if self.ratio <= 0:
            raise ValueError(f"ratio must be positive, got {self.ratio!r}")
        object.__setattr__(self, "ratio", float(self.ratio))


@dataclass(frozen=True, slots=True)
class Dividend:
    """A cash dividend corporate action."""

    ex_date: date
    amount: float

    def __post_init__(self) -> None:
        if not isinstance(self.ex_date, date):
            raise TypeError("ex_date must be a datetime.date")
        if not isinstance(self.amount, (int, float)) or not isfinite(self.amount):
            raise ValueError(f"amount must be finite, got {self.amount!r}")
        if self.amount < 0:
            raise ValueError(f"amount must be non-negative, got {self.amount!r}")
        object.__setattr__(self, "amount", float(self.amount))
