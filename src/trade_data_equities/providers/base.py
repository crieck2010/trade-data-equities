"""Provider interface: the seam where market-data sources plug in.

Every data source -- free delayed feeds today, paid real-time or
fundamental feeds tomorrow -- implements :class:`MarketDataProvider`.
The client, cache, and all downstream engines program against this
interface, never against a specific vendor, which is what makes sources
interchangeable.

Contract
--------
* :meth:`get_bars` returns **unadjusted** bars sorted ascending by
  timestamp. Adjustment is the client's job (see ``adjustments``).
* ``start`` is inclusive, ``end`` is exclusive; both accept ``date`` or
  ``datetime`` (naive datetimes are treated as UTC).
* :meth:`max_window` is an optional hint: the longest span the provider
  can serve in one call for a timeframe. The client chunks larger
  requests automatically. Return ``None`` for "no practical limit".
* Implementations must be thread-safe enough for sequential use; the
  client performs its own throttling per provider instance.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta
from typing import Iterable

from ..exceptions import ProviderError
from ..models import Bar, Dividend, Quote, Split, Symbol, Timeframe

DateLike = date | datetime


class MarketDataProvider(ABC):
    """Abstract market-data source."""

    #: Human-readable source name, used in cache keys and logs.
    name: str = "base"

    #: Expected data delay in minutes (None = unknown).
    delay_minutes: int | None = None

    @abstractmethod
    def get_bars(
        self,
        symbol: Symbol,
        timeframe: Timeframe,
        start: DateLike,
        end: DateLike,
    ) -> list[Bar]:
        """Fetch unadjusted bars for ``[start, end)``, ascending by time."""
        raise NotImplementedError

    def get_splits(
        self, symbol: Symbol, start: DateLike, end: DateLike
    ) -> list[Split]:
        """Splits with ex-dates in ``[start, end)``. Default: none known."""
        return []

    def get_dividends(
        self, symbol: Symbol, start: DateLike, end: DateLike
    ) -> list[Dividend]:
        """Dividends with ex-dates in ``[start, end)``. Default: none known."""
        return []

    def get_quote(self, symbol: Symbol) -> Quote:
        """Latest quote snapshot. Optional; default raises."""
        raise ProviderError(f"provider {self.name!r} does not supply quotes")

    def max_window(self, timeframe: Timeframe) -> timedelta | None:
        """Longest span servable in one request, or None if unlimited."""
        return None

    def supports(self, timeframe: Timeframe) -> bool:
        """Whether this provider can serve ``timeframe`` at all."""
        return True

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"{type(self).__name__}(name={self.name!r})"
