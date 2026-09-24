# trade-data-equities

US stock and ETF market-data engine. It is the first data module of a larger
algorithmic/agentic trading system: one focused repository per instrument type
(equities, options, futures, crypto), each exposing the same provider/client
pattern so strategies, backtests, and agents can mix instruments freely.

Free delayed data today (yfinance, ~15 minutes), pluggable paid feeds tomorrow.
Built for **research, backtesting, and paper trading** — not for live execution.

## Features

- **One data shape for stocks and ETFs** — both are OHLCV instruments, so they
  share one engine instead of two.
- **Pluggable providers** — every source implements `MarketDataProvider`.
  Swap yfinance for Polygon, Alpaca, or a proprietary feed without touching
  strategy or backtest code.
- **Automatic chunking** — long histories are split into provider-sized windows
  (e.g. 7-day pages for 1-minute bars) and merged into one clean series.
- **Read-time corporate-action adjustment** — the cache stores raw vendor data;
  splits and dividends are applied on read, so raw and adjusted views never
  require duplicate storage.
- **Disk cache with per-timeframe TTLs** — intraday data refreshes every
  15 minutes, daily bars every 12 hours; stdlib-only JSON, no database needed.
- **Bounded-memory streaming** — `stream_bars` yields chunk-by-chunk for
  decade-long histories.
- **Universes** — named, deduplicated symbol sets (watchlists, index proxies)
  loadable from ticker lists or CSV.
- **pandas interop** — optional `to_dataframe` export; the core engine itself
  has zero mandatory dependencies.

## Installation

```bash
pip install trade-data-equities            # core engine, stdlib only
pip install "trade-data-equities[yfinance]"  # + free delayed data feed
pip install "trade-data-equities[pandas]"    # + DataFrame export
```

Requires Python 3.10+.

## Quickstart

```python
from datetime import date
from trade_data_equities import (
    EquitiesDataClient, Timeframe, Universe, YFinanceProvider,
)

client = EquitiesDataClient(YFinanceProvider())

# One symbol, adjusted daily bars.
bars = client.get_bars("AAPL", Timeframe.DAILY, date(2023, 1, 1), date(2024, 1, 1))
print(bars[-1])  # most recent bar

# Raw (unadjusted) bars for corporate-action research.
raw = client.get_bars("AAPL", Timeframe.DAILY, date(2023, 1, 1), date(2024, 1, 1),
                      adjusted=False)

# A named universe, scanned one symbol at a time.
tech = Universe.from_tickers("tech", ["AAPL", "MSFT", "NVDA", "AVGO"])
for symbol in tech:
    bars = client.get_bars(symbol, Timeframe.DAILY, date(2024, 1, 1), date(2024, 6, 1))
    print(symbol.ticker, len(bars), "bars")

# Pandas for analysis.
df = client.to_dataframe(bars)
print(df["close"].pct_change().std())  # daily volatility
```

Intraday data works the same way — the client pages the provider's window
limits automatically:

```python
bars = client.get_bars("SPY", Timeframe.M5, date(2024, 1, 1), date(2024, 3, 1))
```

For very long histories, stream instead of materializing:

```python
for chunk in client.stream_bars("AAPL", Timeframe.DAILY, date(2000, 1, 1), date(2024, 1, 1)):
    process(chunk)  # memory stays flat
```

## Architecture

```
                        ┌─────────────────────┐
                        │  EquitiesDataClient │  chunking · caching · adjustment
                        └────────┬────────────┘
                 ┌───────────────┼────────────────┐
                 ▼               ▼                ▼
        ┌──────────────┐ ┌──────────────┐ ┌───────────────┐
        │MarketDataPro-│ │  DiskCache   │ │ adjustments   │
        │vider (ABC)   │ │ JSON on disk │ │ splits/divs   │
        └──────┬───────┘ └──────────────┘ └───────────────┘
               ▼
     ┌──────────────────┐   ┌───────────────────┐
     │ YFinanceProvider │   │ YourProviderHere  │  ← implement 4 methods
     └──────────────────┘   └───────────────────┘

models: Symbol · Bar · Quote · Split · Dividend · Timeframe · InstrumentKind
universe: named symbol sets for scanners and backtests
```

**Data flow.** `get_bars` normalizes bounds to UTC `[start, end)`, splits the
range into provider windows, serves each window from cache or network, merges
and dedupes by timestamp, then applies split/dividend adjustments when asked.
Adjusted data is never cached — the cache always holds the vendor's raw series,
so changing adjustment logic never invalidates stored data.

**Timestamps.** All timestamps are tz-aware UTC. A `Bar`'s timestamp marks the
*open* of its interval. Naive datetimes passed as bounds are assumed UTC.

## Adding a provider

Implement `MarketDataProvider` — four methods cover the whole contract:

```python
from trade_data_equities import MarketDataProvider, Bar, Symbol, Timeframe

class PolygonProvider(MarketDataProvider):
    name = "polygon"          # used in cache keys
    delay_minutes = 0         # real-time feed

    def get_bars(self, symbol, timeframe, start, end) -> list[Bar]:
        ...  # return UNADJUSTED bars, ascending by timestamp

    def max_window(self, timeframe):
        from datetime import timedelta
        return timedelta(days=30)  # or None for unlimited
```

`get_splits` / `get_dividends` default to "none known"; `get_quote` and
`supports` are optional overrides. The client, cache, and every downstream
engine work unchanged.

## Adjustment methodology

- **Splits first:** bars with `date < ex_date` get OHLC ÷ ratio and volume × ratio.
  Multiple splits compound multiplicatively.
- **Dividends second:** `factor = (prev_close − amount) / prev_close` from the
  split-adjusted close before the ex-date, applied to earlier bars. Volumes are
  never dividend-adjusted. Zero amounts and unresolvable cases are skipped
  defensively rather than corrupting the series.
- Functions in `trade_data_equities.adjustments` are pure (no input mutation).

## Scaling notes

- **Chunking** keeps any single provider request inside vendor limits; a
  20-year daily history and a 3-month 5-minute history use the same call.
- **Caching** makes repeated backtests over one universe essentially free —
  only the first fetch touches the network.
- **Streaming** (`stream_bars`) processes unbounded histories in constant
  memory; corporate actions are resolved once up front so per-chunk adjustment
  stays correct.
- **Throughput:** one client instance per thread is safe for sequential use;
  for parallel universe scans, give each worker its own client sharing one
  `DiskCache` root (writes are atomic file replacements per key).
- Cache location defaults to `~/.cache/trade-data-equities` and honors the
  `TRADE_DATA_CACHE` environment variable — point it at fast local storage.

## Interoperability

- Core models are immutable stdlib dataclasses — trivially serializable to
  JSON, Parquet, or a database row by sibling engines.
- `to_dataframe` bridges to the pandas/numpy research stack (including the
  `trade-backtest` and `trade-strategies` siblings, which consume `Bar` lists
  directly).
- yfinance remains an *optional* extra: the engine imports and runs without it,
  so headless backtest workers can ship without network-feed dependencies.

## API reference (essentials)

| Name | Kind | Purpose |
|---|---|---|
| `EquitiesDataClient(provider, cache, auto_adjust)` | class | Main entry point |
| `client.get_bars(symbol, timeframe, start, end, adjusted, use_cache)` | method | Fetch merged, adjusted bars |
| `client.stream_bars(...)` | method | Chunk-by-chunk iterator |
| `client.to_dataframe(bars)` | static | pandas export |
| `MarketDataProvider` | ABC | Interface for new feeds |
| `YFinanceProvider(min_interval, max_retries)` | class | Free delayed feed |
| `DiskCache(root, ttl)` | class | JSON cache with TTLs |
| `Universe.from_tickers / from_csv` | classmethods | Named symbol sets |
| `adjust_bars(bars, splits, dividends)` | function | Pure adjustment |

## Testing

```bash
pip install "trade-data-equities[dev]"
pytest -q
```

The suite runs entirely offline: providers are faked, and the yfinance frame
mapper is tested against synthetic DataFrames. A live smoke test is in
`examples/quickstart.py` (requires the `yfinance` extra and network access).

## Roadmap

Sibling repositories in this trading system:

- `trade-data-options` — chains, greeks, IV surfaces (extends the options-screener work)
- `trade-data-futures` — contract specs, expiry calendars, continuous contracts
- `trade-data-crypto` — 24/7 exchange data
- `trade-backtest` — event-driven backtesting on `Bar` streams
- `trade-strategies` — strategy framework + starter strategies
- `trade-risk` — position sizing, exposure limits, drawdown guards
- `trade-agents` — hedge-fund desk: idea agents → portfolio-manager → risk-manager
- `trade-dashboard-web` / `trade-dashboard-desktop` — dashboards
- `trade-suite` — meta-package tying it all together

## The maths

**What you learn.** A data engine's maths is its measurement discipline: how raw vendor candles become one clean, split/dividend-adjusted, tz-aware bar series that every backtest and screener in the suite can trust. The two computations that matter here are corporate-action adjustment and windowed series assembly.

**Why it matters.** An unadjusted 4:1 split looks like a −75% crash; an unadjusted dividend looks like a gap down. Any return, volatility, or drawdown statistic computed on raw prices is wrong at every corporate-action boundary. This engine stores the vendor's raw series and applies adjustments on read, so raw and adjusted views coexist without duplicate storage — and fixing adjustment logic never invalidates the cache.

**The maths.** Splits apply first: every bar with `date < ex_date` gets `OHLC ÷ ratio` and `volume × ratio`; multiple splits compound multiplicatively (a 2:1 then a 3:1 is a 6:1 cumulative factor). Dividends apply second, off the split-adjusted series: `factor = (prev_close − amount) / prev_close` computed from the close before the ex-date, multiplied backward through all earlier bars; volumes are never dividend-adjusted. Series assembly: bounds are normalized to tz-aware UTC `[start, end)` (a bar's timestamp marks the *open* of its interval), the range is split into provider-sized windows (e.g. 7-day pages for 1-minute bars), each window is served from the JSON disk cache or the network, then merged, sorted, and deduped by timestamp. Universes are immutable, deduplicated ticker sets, so a scan over "tech" is reproducible by name. TTLs are per timeframe (15 min intraday, 12 h daily); `stream_bars` resolves corporate actions once up front so chunked decade-long histories stay correctly adjusted in constant memory.

**Honest limitations.** Adjustment factors derive from the vendor's reported splits/dividends — bad corporate-action metadata in means bad adjusted prices out. Dividend adjustment uses the classic backward-ratio method, not total-return reinvestment accounting. yfinance data is delayed ~15 minutes and its corporate-action history can lag. Naive datetimes are assumed UTC, which silently mislabels exchange-local timestamps if you pass them. The engine does no survivorship-bias correction: delisted symbols simply stop appearing.

## License

MIT — see [LICENSE](LICENSE).
