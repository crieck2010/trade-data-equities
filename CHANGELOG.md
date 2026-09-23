# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
