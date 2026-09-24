"""Market-data provider backed by Polygon (now Massive) REST aggregates.

Polygon sells exchange-grade REST market data: aggregate bars for stocks,
ETFs, options, futures, forex, and crypto, in tiers from free end-of-day
to paid intraday and real-time. This provider covers the **stocks**
aggregates endpoint; other asset classes have their own engines.

Rebrand note
-----------
Polygon.io rebranded to **Massive** on 2025-10-30. The API base moved to
``https://api.massive.com`` while ``https://api.polygon.io`` remains
supported in parallel. This provider defaults to the new base and lets
you override it with ``POLYGON_BASE_URL``.

You need your own API key (``POLYGON_API_KEY``). No key is bundled, none
is requested interactively, and the key never appears in logs, errors,
or ``repr`` output.

The one-line swap::

    from trade_data_equities import EquitiesDataClient
    from trade_data_equities.providers import PolygonProvider

    client = EquitiesDataClient(PolygonProvider())  # reads POLYGON_API_KEY

Everything downstream -- backtests, strategies, screeners, breadth,
agents -- is untouched, because they program against
:class:`MarketDataProvider`, not against Polygon.

Contract notes
--------------
* :meth:`get_bars` returns **unadjusted** bars (``adjusted=false`` on the
  wire), per the :class:`MarketDataProvider` contract. The engine's
  :class:`EquitiesDataClient` applies split/dividend adjustments on read.
  Polygon's ``adjusted=true`` only corrects for splits, so requesting raw
  bars and adjusting client-side is both contract-correct and *more*
  complete (dividends included).
* Polygon timestamps (``t``, milliseconds since epoch) mark the **open**
  of the bar interval, matching :class:`Bar` semantics.
* Only trading days are returned; gaps (weekends, holidays, halts) pass
  through unfilled -- filling is the caller's domain.
* Field compromises: ``vw`` (VWAP) and ``n`` (trade count) are not part
  of :class:`Bar` and are dropped. Missing ``v`` (volume) becomes 0.0.
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from ..exceptions import ProviderError, RateLimitError, SymbolNotFoundError
from ..models import Bar, Dividend, Split, Symbol, Timeframe, ensure_utc
from .base import DateLike, MarketDataProvider

#: Current API base (post-rebrand). ``api.polygon.io`` still works; set
#: ``POLYGON_BASE_URL`` to use it or any proxy/mirror.
DEFAULT_BASE_URL = "https://api.massive.com"

#: Suite timeframe -> (Polygon multiplier, Polygon timespan).
TIMEFRAME_MAP: dict[Timeframe, tuple[int, str]] = {
    Timeframe.M1: (1, "minute"),
    Timeframe.M5: (5, "minute"),
    Timeframe.M15: (15, "minute"),
    Timeframe.H1: (1, "hour"),
    Timeframe.DAILY: (1, "day"),
    Timeframe.WEEKLY: (1, "week"),
    Timeframe.MONTHLY: (1, "month"),
}

#: Chunking hint per timeframe, mirroring YFinanceProvider so cache
#: granularity and client behavior stay consistent across providers.
#: Polygon's hard page is 50,000 aggregates; pagination follows
#: ``next_url`` regardless, so these windows are about cache locality,
#: not capability.
_WINDOWS: dict[Timeframe, timedelta | None] = {
    Timeframe.M1: timedelta(days=7),
    Timeframe.M5: timedelta(days=60),
    Timeframe.M15: timedelta(days=60),
    Timeframe.H1: timedelta(days=730),
    Timeframe.DAILY: None,
    Timeframe.WEEKLY: None,
    Timeframe.MONTHLY: None,
}

_AGGS_LIMIT = 50_000


@dataclass
class HttpResponse:
    """Minimal HTTP response shape used by transports."""

    status: int
    headers: dict[str, str]
    body: bytes

    def json(self):
        return json.loads(self.body.decode("utf-8"))


class UrllibTransport:
    """Default HTTP transport: stdlib ``urllib``, no third-party deps."""

    def get(self, url: str, timeout: float) -> HttpResponse:
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "trade-data-equities/polygon-provider",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                headers = {k.lower(): v for k, v in resp.headers.items()}
                return HttpResponse(resp.status, headers, resp.read())
        except urllib.error.HTTPError as exc:
            headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
            return HttpResponse(exc.code, headers, exc.read() if hasattr(exc, "read") else b"")


class MockPolygonHTTP:
    """Scripted in-memory transport: the offline test double.

    Queue ``(status, headers, payload)`` triples with :meth:`script`;
    each :meth:`get` pops the next one and records the request URL.
    Raise :class:`AssertionError` when the queue runs dry so tests fail
    loudly instead of hanging on the network.
    """

    def __init__(self) -> None:
        self._queue: list[tuple[int, dict, object]] = []
        self.requests: list[str] = []

    def script(self, status: int, payload: object, headers: dict | None = None) -> "MockPolygonHTTP":
        self._queue.append((status, {k.lower(): v for k, v in (headers or {}).items()}, payload))
        return self

    def script_json(self, payload: object, status: int = 200, headers: dict | None = None) -> "MockPolygonHTTP":
        return self.script(status, payload, headers)

    def get(self, url: str, timeout: float) -> HttpResponse:  # noqa: ARG002
        self.requests.append(url)
        if not self._queue:
            raise AssertionError(f"MockPolygonHTTP ran out of scripted responses for {url!r}")
        status, headers, payload = self._queue.pop(0)
        body = payload if isinstance(payload, (bytes, bytearray)) else json.dumps(payload).encode()
        return HttpResponse(status, headers, bytes(body))


def _retry_delay(attempt: int, retry_after: str | None, *, base: float = 1.0, cap: float = 60.0) -> float:
    """Seconds to wait before retry ``attempt`` (0-based).

    Honors a server ``Retry-After`` (seconds) when present and sane;
    otherwise exponential backoff ``base * 2**attempt`` capped at ``cap``,
    plus uniform jitter in ``[0, base)`` so concurrent clients do not
    retry in lockstep.
    """
    if retry_after is not None:
        try:
            wait = float(retry_after)
            if 0 < wait <= 300:
                return wait
        except (TypeError, ValueError):
            pass
    return min(cap, base * 2**attempt) + random.uniform(0, base)


def _redact(url: str) -> str:
    """Hide the API key for any message a human might read."""
    # apiKey is the only secret ever placed in a URL by this module.
    parts = url.split("apiKey=")
    if len(parts) == 1:
        return url
    tail = parts[1]
    cut = len(tail)
    for sep in ("&", "#"):
        idx = tail.find(sep)
        if idx != -1:
            cut = min(cut, idx)
    return parts[0] + "apiKey=***" + tail[cut:]


def bars_from_polygon(results: list[dict]) -> list[Bar]:
    """Convert Polygon aggregate dicts to :class:`Bar` list.

    Pure function -- no network -- so it is unit-testable offline.
    Expects Polygon's ``results`` entries with ``t`` (ms epoch),
    ``o``/``h``/``l``/``c``, and optional ``v``. Malformed rows are
    skipped rather than failing the whole fetch; gaps pass through.
    """
    bars: list[Bar] = []
    for row in results:
        try:
            stamp = datetime.fromtimestamp(float(row["t"]) / 1000.0, tz=timezone.utc)
            bars.append(
                Bar(
                    timestamp=stamp,
                    open=float(row["o"]),
                    high=float(row["h"]),
                    low=float(row["l"]),
                    close=float(row["c"]),
                    volume=float(row.get("v", 0.0) or 0.0),
                )
            )
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            continue
    bars.sort(key=lambda b: b.timestamp)
    return bars


class PolygonProvider(MarketDataProvider):
    """Equity aggregates via the Polygon/Massive REST API (stocks).

    :param api_key: Polygon API key; defaults to ``POLYGON_API_KEY``.
      A missing key raises :class:`ProviderError` immediately -- the
      provider never prompts, and never logs the key.
    :param base_url: API base; defaults to ``POLYGON_BASE_URL`` or
      ``https://api.massive.com``.
    :param min_interval: minimum seconds between HTTP calls (politeness;
      raise this on the free tier, ~5 req/min).
    :param max_retries: attempts per request before giving up.
    :param max_pages: safety cap on ``next_url`` pagination; exceeding it
      raises instead of silently returning partial data.
    :param timeout: per-request socket timeout, seconds.
    :param transport: HTTP layer; defaults to stdlib urllib. Tests inject
      :class:`MockPolygonHTTP`.
    """

    name = "polygon"
    # Delay depends on the subscriber's tier (free EOD -> paid real-time),
    # so "unknown" is the honest value here.
    delay_minutes: int | None = None

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        min_interval: float = 1.0,
        max_retries: int = 5,
        max_pages: int = 100,
        timeout: float = 30.0,
        transport=None,
    ) -> None:
        key = api_key or os.environ.get("POLYGON_API_KEY")
        if not key:
            raise ProviderError(
                "Polygon API key not found: pass api_key=... or set the "
                "POLYGON_API_KEY environment variable. Get a key at "
                "https://massive.com (free tier available)."
            )
        self._api_key = key
        self.base_url = (base_url or os.environ.get("POLYGON_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.max_pages = max_pages
        self.timeout = timeout
        self._transport = transport or UrllibTransport()
        self._last_call = 0.0

    def __repr__(self) -> str:  # never leaks the key
        return f"PolygonProvider(base_url={self.base_url!r}, key=***)"

    # -- internals ----------------------------------------------------
    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _url(self, path: str, params: dict) -> str:
        # ``next_url`` pagination cursors come back as absolute URLs --
        # use them verbatim instead of re-prefixing the base.
        url = path if path.startswith(("http://", "https://")) else f"{self.base_url}{path}"
        query = "&".join(f"{k}={v}" for k, v in params.items())
        if query:
            url += ("&" if "?" in url else "?") + query
        if "apiKey=" not in url:
            url += ("&" if "?" in url else "?") + f"apiKey={self._api_key}"
        return url

    def _get_json(self, url: str) -> dict:
        """GET ``url`` with throttle + retry; return the decoded payload."""
        last: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                resp = self._transport.get(url, self.timeout)
            except Exception as exc:  # noqa: BLE001 - transport errors vary
                last = exc
                time.sleep(_retry_delay(attempt, None))
                continue
            if resp.status == 200:
                try:
                    return resp.json()
                except (ValueError, UnicodeDecodeError) as exc:
                    raise ProviderError(
                        f"Polygon returned undecodable JSON for {_redact(url)}"
                    ) from exc
            if resp.status == 429:
                last = RateLimitError(f"Polygon rate limit (429) for {_redact(url)}")
                time.sleep(_retry_delay(attempt, resp.headers.get("retry-after")))
                continue
            if resp.status in (500, 502, 503, 504):
                last = ProviderError(f"Polygon server error ({resp.status}) for {_redact(url)}")
                time.sleep(_retry_delay(attempt, None))
                continue
            if resp.status == 401:
                raise ProviderError(
                    "Polygon rejected the API key (401). Check POLYGON_API_KEY; "
                    "the key itself is never logged."
                )
            if resp.status == 403:
                raise ProviderError(
                    f"Polygon refused the request (403) for {_redact(url)} -- "
                    "this usually means the endpoint needs a higher subscription tier."
                )
            if resp.status == 404:
                raise SymbolNotFoundError(f"Polygon has no data at {_redact(url)}")
            raise ProviderError(f"Polygon request failed ({resp.status}) for {_redact(url)}")
        if isinstance(last, RateLimitError):
            raise last
        raise ProviderError(
            f"Polygon request failed after {self.max_retries} attempts: {_redact(url)}"
        ) from last

    def _aggs_url(self, symbol: Symbol, timeframe: Timeframe, start: date, end: date) -> str:
        mult, timespan = TIMEFRAME_MAP[timeframe]
        path = (
            f"/v2/aggs/ticker/{symbol.ticker}"
            f"/range/{mult}/{timespan}/{start.isoformat()}/{end.isoformat()}"
        )
        return self._url(
            path,
            {
                "adjusted": "false",  # contract: provider returns UNADJUSTED bars
                "sort": "asc",
                "limit": str(_AGGS_LIMIT),
            },
        )

    # -- MarketDataProvider -------------------------------------------
    def max_window(self, timeframe: Timeframe) -> timedelta | None:
        return _WINDOWS[timeframe]

    def get_bars(
        self, symbol: Symbol, timeframe: Timeframe, start: DateLike, end: DateLike
    ) -> list[Bar]:
        start_d = ensure_utc(start).date() if isinstance(start, datetime) else start
        end_d = ensure_utc(end).date() if isinstance(end, datetime) else end
        url: str | None = self._aggs_url(symbol, timeframe, start_d, end_d)
        results: list[dict] = []
        pages = 0
        while url is not None:
            if pages >= self.max_pages:
                raise ProviderError(
                    f"Polygon pagination exceeded {self.max_pages} pages for "
                    f"{symbol.ticker} {timeframe.value}; refusing to return "
                    "partial data -- narrow the range or raise max_pages."
                )
            payload = self._get_json(url)
            batch = payload.get("results") or []
            results.extend(batch)
            pages += 1
            nxt = payload.get("next_url")
            url = self._url(nxt, {}) if nxt else None
        if not results:
            raise SymbolNotFoundError(
                f"Polygon returned no {timeframe.value} bars for {symbol.ticker} "
                f"in [{start_d}, {end_d})"
            )
        return bars_from_polygon(results)

    def _reference(
        self, endpoint: str, ticker: str, date_field: str, start: date, end: date
    ) -> list[dict]:
        """GET a Polygon reference endpoint; fail soft on tier limits.

        Splits/dividends reference data needs a paid tier on some plans.
        A 403/404 means "not entitled", not "broken" -- return [] so
        adjusted reads degrade to unadjusted-adjusted rather than dying.
        A 401 (bad key) stays loud.
        """
        url = self._url(
            endpoint,
            {
                "ticker": ticker,
                f"{date_field}.gte": start.isoformat(),
                f"{date_field}.lt": end.isoformat(),
                "limit": "1000",
                "sort": "execution_date" if "split" in endpoint else "ex_dividend_date",
            },
        )
        try:
            payload = self._get_json(url)
        except SymbolNotFoundError:
            return []  # 404: no reference data for this ticker on this tier
        except ProviderError as exc:
            if "(403)" in str(exc):
                return []  # tier lacks reference-data entitlement
            raise
        return payload.get("results") or []

    def get_splits(self, symbol: Symbol, start: DateLike, end: DateLike) -> list[Split]:
        start_d = ensure_utc(start).date() if isinstance(start, datetime) else start
        end_d = ensure_utc(end).date() if isinstance(end, datetime) else end
        out: list[Split] = []
        for row in self._reference("/v3/reference/splits", symbol.ticker, "execution_date", start_d, end_d):
            try:
                ratio = float(row["split_to"]) / float(row["split_from"])
                out.append(Split(ex_date=date.fromisoformat(row["execution_date"][:10]), ratio=ratio))
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                continue
        out.sort(key=lambda s: s.ex_date)
        return out

    def get_dividends(self, symbol: Symbol, start: DateLike, end: DateLike) -> list[Dividend]:
        start_d = ensure_utc(start).date() if isinstance(start, datetime) else start
        end_d = ensure_utc(end).date() if isinstance(end, datetime) else end
        out: list[Dividend] = []
        for row in self._reference(
            "/v3/reference/dividends", symbol.ticker, "ex_dividend_date", start_d, end_d
        ):
            try:
                out.append(
                    Dividend(
                        ex_date=date.fromisoformat(row["ex_dividend_date"][:10]),
                        amount=float(row["cash_amount"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        out.sort(key=lambda d: d.ex_date)
        return out
