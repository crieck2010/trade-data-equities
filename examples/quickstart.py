"""End-to-end smoke test: fetch real delayed data and print a summary.

Requires the yfinance extra and network access:
    pip install "trade-data-equities[yfinance]"
"""

from datetime import date, timedelta

from trade_data_equities import (
    EquitiesDataClient,
    Timeframe,
    Universe,
    YFinanceProvider,
)


def main() -> None:
    client = EquitiesDataClient(YFinanceProvider())
    universe = Universe.from_tickers("demo", ["AAPL", "MSFT", "SPY"])
    end = date.today()
    start = end - timedelta(days=30)

    for symbol in universe:
        bars = client.get_bars(symbol, Timeframe.DAILY, start, end)
        first, last = bars[0], bars[-1]
        ret = (last.close / first.close - 1.0) * 100
        print(
            f"{symbol.ticker:5s} {len(bars):3d} bars  "
            f"{first.timestamp.date()} -> {last.timestamp.date()}  "
            f"close {last.close:8.2f}  {ret:+6.2f}%"
        )

    # Raw vs adjusted comparison around a known split (AAPL 4:1, 2020-08-31).
    raw = client.get_bars("AAPL", Timeframe.DAILY, date(2020, 8, 28), date(2020, 9, 2), adjusted=False)
    adj = client.get_bars("AAPL", Timeframe.DAILY, date(2020, 8, 28), date(2020, 9, 2), adjusted=True)
    print("\nAAPL split adjustment check (4:1 on 2020-08-31):")
    for r, a in zip(raw, adj):
        print(f"  {r.timestamp.date()}  raw close {r.close:8.2f}  adjusted {a.close:8.2f}")


if __name__ == "__main__":
    main()
