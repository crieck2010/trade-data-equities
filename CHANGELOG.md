# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-09-24

### Added
- `PolygonProvider`: equity aggregates via the Polygon/Massive REST API
  (`/v2/aggs`), the paid-data counterpart to `YFinanceProvider`. Implements
  `MarketDataProvider` exactly, so the swap is one line:
  `EquitiesDataClient(PolygonProvider())` — no downstream code changes.
- `MockPolygonHTTP`: scripted in-memory transport (paginated, 429, empty,
  error responses) so the whole provider is tested offline with no key.
- `bars_from_polygon`: pure Polygon-aggregates → `Bar` mapper, unit-tested
  without network access.
- Key handling: `POLYGON_API_KEY` env (or explicit arg), never logged, never
  in errors or `repr`; `POLYGON_BASE_URL` overrides the default
  `https://api.massive.com` (post-rebrand base; `api.polygon.io` still works).
- Pagination via `next_url` with a `max_pages` guard that raises instead of
  returning partial data; 429 backoff with jitter honoring `Retry-After`;
  `get_splits`/`get_dividends` via reference endpoints, fail-soft on tier
  limits.
- `docs/POLYGON.md`: setup, tier recap, timeframe mapping, pagination and
  rate-limit behavior, batching guidance, honest limitations.

## [0.1.0] - 2026-09-23

### Added
- Core models: `Symbol`, `Bar`, `Quote`, `Split`, `Dividend`, `Timeframe`, `InstrumentKind`
  (stocks and ETFs share one OHLCV data shape).
- `MarketDataProvider` abstract interface: the seam where data sources plug in.
- `YFinanceProvider`: free delayed (~15 min) equity data via yfinance, with request
  throttling, retries, rate-limit detection, and per-timeframe request windows.
- `EquitiesDataClient`: date normalization, automatic request chunking, merge and
  dedupe, read-time split/dividend adjustment, and a bounded-memory `stream_bars`
  iterator for very long histories.
- `DiskCache`: stdlib-only JSON file cache with per-timeframe TTLs; always stores
  unadjusted vendor data.
- `adjustments`: pure split and dividend retro-adjustment functions with documented
  methodology (splits first, then dividends on split-adjusted closes).
- `Universe`: named, deduplicated, immutable symbol sets with CSV loading and filtering.
- `to_dataframe`: optional pandas export for analysis interop.
- Full test suite (no network access required) and comprehensive README.
