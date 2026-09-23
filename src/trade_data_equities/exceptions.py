"""Exception hierarchy for the equities market-data engine."""


class TradeDataError(Exception):
    """Base class for all engine errors."""


class SymbolNotFoundError(TradeDataError):
    """The provider has no data for the requested symbol."""


class ProviderError(TradeDataError):
    """The provider failed after retries (network, parsing, API errors)."""


class RateLimitError(ProviderError):
    """The provider rate-limited the request; back off and retry later."""
