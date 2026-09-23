"""Corporate-action adjustments for OHLCV series.

The engine stores and caches **unadjusted** bars, then applies adjustments
on read. This keeps one canonical series per symbol/timeframe and lets
callers choose raw or adjusted views without duplicating cache entries.

Methodology
-----------
* **Splits** are applied first: every bar with ``timestamp.date() <
  ex_date`` has OHLC divided by ``ratio`` and volume multiplied by
  ``ratio``. Multiple splits compound multiplicatively.
* **Dividends** are applied second, on the split-adjusted closes: for each
  dividend, ``factor = (prev_close - amount) / prev_close`` where
  ``prev_close`` is the close of the last bar before ``ex_date``; all bars
  before ``ex_date`` are multiplied by ``factor``. Dividends whose amount
  is zero, or where no prior close exists (or the close is not larger than
  the dividend), are skipped defensively.

All functions are pure: inputs are never mutated and a new list is
returned.
"""

from __future__ import annotations

from dataclasses import replace

from .models import Bar, Dividend, Split


def adjust_for_splits(bars: list[Bar], splits: list[Split]) -> list[Bar]:
    """Return ``bars`` retro-adjusted for ``splits``."""
    splits = sorted(splits, key=lambda s: s.ex_date)
    if not splits or not bars:
        return list(bars)
    out: list[Bar] = []
    for bar in bars:
        price_factor = 1.0
        volume_factor = 1.0
        bar_date = bar.timestamp.date()
        for split in splits:
            if split.ex_date > bar_date:
                price_factor /= split.ratio
                volume_factor *= split.ratio
        if price_factor == 1.0:
            out.append(bar)
        else:
            out.append(
                replace(
                    bar,
                    open=bar.open * price_factor,
                    high=bar.high * price_factor,
                    low=bar.low * price_factor,
                    close=bar.close * price_factor,
                    volume=bar.volume * volume_factor,
                )
            )
    return out


def adjust_for_dividends(bars: list[Bar], dividends: list[Dividend]) -> list[Bar]:
    """Return ``bars`` retro-adjusted for cash ``dividends``."""
    dividends = sorted(dividends, key=lambda d: d.ex_date)
    if not dividends or not bars:
        return list(bars)
    # Work on a mutable copy of per-bar price multipliers.
    factors = [1.0] * len(bars)
    dates = [bar.timestamp.date() for bar in bars]
    for dividend in dividends:
        if dividend.amount <= 0:
            continue
        # Close of the last bar strictly before the ex-date.
        prev_close = None
        for bar, bar_date in zip(bars, dates):
            if bar_date < dividend.ex_date:
                prev_close = bar.close
            else:
                break
        if prev_close is None or prev_close <= dividend.amount:
            continue
        factor = (prev_close - dividend.amount) / prev_close
        for i, bar_date in enumerate(dates):
            if bar_date < dividend.ex_date:
                factors[i] *= factor
    out: list[Bar] = []
    for bar, factor in zip(bars, factors):
        if factor == 1.0:
            out.append(bar)
        else:
            out.append(
                replace(
                    bar,
                    open=bar.open * factor,
                    high=bar.high * factor,
                    low=bar.low * factor,
                    close=bar.close * factor,
                )
            )
    return out


def adjust_bars(
    bars: list[Bar],
    splits: list[Split] | None = None,
    dividends: list[Dividend] | None = None,
) -> list[Bar]:
    """Return ``bars`` adjusted for splits, then dividends.

    Split adjustment runs first so dividend factors are computed from
    split-adjusted closes, matching the convention used by most data
    vendors for "adjusted close" series.
    """
    adjusted = adjust_for_splits(bars, splits or [])
    return adjust_for_dividends(adjusted, dividends or [])
