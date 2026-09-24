# Polygon provider

`trade_data_equities.providers.PolygonProvider` fetches US stock and ETF
aggregate bars from the Polygon REST API — the paid-data counterpart to
`YFinanceProvider`. It implements `MarketDataProvider` exactly, so the
swap is one line and every downstream engine (backtests, strategies,
screener, breadth, agents) works untouched.

## The rebrand: Polygon → Massive

Polygon.io rebranded to **Massive** on 2025-10-30. What changed for you:

- New API base: `https://api.massive.com` (this provider's default).
- Old base `https://api.polygon.io` remains live and supported in parallel.
- Existing API keys, accounts, and integrations keep working unchanged.

Override the base with the `POLYGON_BASE_URL` environment variable or the
`base_url=` constructor argument if you need the old domain, a proxy, or
a mirror.

## Tiers (recap)

Polygon sells per-asset-class subscriptions; the stocks tier is what this
provider uses. Roughly, at time of writing:

| Tier | Price (indicative) | What you get |
|---|---|---|
| Free | $0 | End-of-day aggregates |
| Starter | ~$29/mo | 15-minute delayed, intraday + history |
| Developer | ~$79/mo | 15-minute delayed, deeper history |
| Advanced | ~$199/mo | Real-time |

Check [massive.com/pricing](https://massive.com/pricing) for current
numbers — they change. **Delay is a property of your tier, not of this
code**: the provider reports `delay_minutes = None` (unknown) because it
cannot know which plan your key is on.

## Setup

1. Get a key at [massive.com](https://massive.com) (free tier available).
2. `export POLYGON_API_KEY=...`
3. Swap the provider:

```python
from trade_data_equities import EquitiesDataClient
from trade_data_equities.providers import PolygonProvider

client = EquitiesDataClient(PolygonProvider())  # reads POLYGON_API_KEY
```

The key is never logged, never appears in error messages (they say "the
key itself is never logged" instead), and never shows in `repr`. It
travels only as the `apiKey` query parameter over HTTPS.

## Timeframe mapping

| Suite `Timeframe` | Polygon multiplier | Polygon timespan |
|---|---|---|
| `M1` (`1m`) | 1 | minute |
| `M5` (`5m`) | 5 | minute |
| `M15` (`15m`) | 15 | minute |
| `H1` (`1h`) | 1 | hour |
| `DAILY` (`1d`) | 1 | day |
| `WEEKLY` (`1wk`) | 1 | week |
| `MONTHLY` (`1mo`) | 1 | month |

Wire format: `GET /v2/aggs/ticker/{TICKER}/range/{mult}/{timespan}/{from}/{to}`
with `adjusted=false&sort=asc&limit=50000`. Bounds are sent as `YYYY-MM-DD`
dates derived from the UTC-normalized `[start, end)` range; the client
filters the merged series to the exact bounds afterward.

## Pagination

Polygon caps a response at 50,000 aggregates and returns a `next_url`
cursor when more pages exist. The provider follows cursors until
exhausted, then concatenates and sorts. A `max_pages` guard (default 100)
aborts with `ProviderError` rather than silently returning partial data —
narrow the range or raise the cap if you hit it.

## Rate limits and politeness

- `min_interval` (default 1.0 s) throttles between calls. On the free
  tier (~5 req/min), raise this to ~12 s.
- HTTP 429 triggers exponential backoff with jitter
  (`min(60, 1·2^attempt) + U(0,1)`); a server `Retry-After` header is
  honored verbatim when sane. After `max_retries` (default 5) attempts the
  provider raises `RateLimitError`.
- 5xx errors are retried the same way. A 401 (bad key) fails fast and
  loud. A 403 usually means the endpoint needs a higher tier.

## Corporate actions

Per the `MarketDataProvider` contract, `get_bars` returns **unadjusted**
bars (`adjusted=false` on the wire — Polygon's own `adjusted=true` only
corrects splits, while the engine's client-side adjustment covers splits
*and* dividends). `get_splits` / `get_dividends` read Polygon's reference
endpoints (`/v3/reference/splits`, `/v3/reference/dividends`) best-effort:
a 403/404 (tier lacks reference-data entitlement) degrades to `[]` rather
than failing the read. Consequence to be aware of: on a tier without
reference access, "adjusted" reads are silently less-adjusted — the
limitation is documented here so it is never a surprise.

## Multi-symbol batching

Today: sequential, one symbol at a time, with throttle + backoff. That is
deliberate — it keeps request accounting trivially predictable against
your plan's quota. The documented future path is one client per worker
thread sharing a `DiskCache` root (the repo's established pattern);
a shared token-bucket across threads would be the next step if throughput
ever binds, with the caveat that Polygon quotas are per-key, so threads
share one budget.

## Field compromises

Polygon aggregate fields map as: `t` (ms) → `timestamp` (UTC, bar open),
`o/h/l/c` → OHLC, `v` → volume (missing → 0.0). `vw` (VWAP) and `n`
(trade count) have no `Bar` counterpart and are dropped. Malformed rows
are skipped, matching `YFinanceProvider` behavior. Only trading days are
returned — weekends, holidays, and halts pass through as gaps, unfilled.

## Honest limitations

- **Delay depends on your tier, not the code.** Free = EOD; real-time
  costs money. The provider cannot upgrade your data.
- **No survivorship-bias correction.** Delisted symbols vanish from
  history, same as every other provider in this engine.
- **Reference-data entitlement varies by tier.** See "Corporate actions"
  above: adjustment quality follows your plan.
- **API surface drift.** Endpoint shapes were verified against public
  docs at build time; if Massive renames a field, the defensive parsing
  skips rows rather than crashing, and `MockPolygonHTTP` lets you pin the
  new shape in tests before touching production code.
- **Key hygiene is yours.** The provider reads the key from the
  environment and redacts it everywhere, but *you* choose where the env
  var lives (shell profile, secret manager, CI secrets — never in git).
