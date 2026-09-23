"""trade-data-equities: US stock and ETF market-data engine.

Free delayed data today (yfinance), pluggable paid feeds tomorrow.
See README.md for the full guide.
"""

from .adjustments import adjust_bars, adjust_for_dividends, adjust_for_splits
from .cache import DiskCache
from .client import EquitiesDataClient
from .exceptions import (
    ProviderError,
    RateLimitError,
    SymbolNotFoundError,
    TradeDataError,
)
from .models import (
    Bar,
    Dividend,
    InstrumentKind,
    Quote,
    Split,
    Symbol,
    Timeframe,
    ensure_utc,
)
from .providers import MarketDataProvider, YFinanceProvider
from .universe import Universe

__version__ = "0.1.0"

__all__ = [
    "Bar",
    "Dividend",
    "DiskCache",
    "EquitiesDataClient",
    "InstrumentKind",
    "MarketDataProvider",
    "ProviderError",
    "Quote",
    "RateLimitError",
    "Split",
    "Symbol",
    "SymbolNotFoundError",
    "Timeframe",
    "TradeDataError",
    "Universe",
    "YFinanceProvider",
    "__version__",
    "adjust_bars",
    "adjust_for_dividends",
    "adjust_for_splits",
    "ensure_utc",
]
