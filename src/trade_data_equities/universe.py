"""Named symbol universes: the working sets strategies scan.

A :class:`Universe` is an immutable, named collection of symbols --
a watchlist, an index proxy, or a full tradable universe. Engines that
scan many symbols (screeners, backtests, agents) take a universe rather
than a bare list so runs stay labeled and reproducible.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from .models import InstrumentKind, Symbol


@dataclass(frozen=True, slots=True)
class Universe:
    """An immutable named set of symbols."""

    name: str
    symbols: tuple[Symbol, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("universe name must be non-empty")
        seen: set[str] = set()
        deduped: list[Symbol] = []
        for symbol in self.symbols:
            if symbol.ticker not in seen:
                seen.add(symbol.ticker)
                deduped.append(symbol)
        object.__setattr__(self, "symbols", tuple(deduped))

    @classmethod
    def from_tickers(
        cls,
        name: str,
        tickers: list[str],
        kind: InstrumentKind = InstrumentKind.STOCK,
    ) -> "Universe":
        """Build a universe from ticker strings."""
        return cls(name=name, symbols=tuple(Symbol(ticker=t, kind=kind) for t in tickers))

    @classmethod
    def from_csv(cls, name: str, path: str | Path) -> "Universe":
        """Build a universe from a CSV with ``ticker`` and optional
        ``kind`` (stock/etf), ``exchange`` and ``name`` columns."""
        symbols: list[Symbol] = []
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if not row.get("ticker"):
                    continue
                kind = InstrumentKind(row.get("kind", "stock").strip().lower() or "stock")
                symbols.append(
                    Symbol(
                        ticker=row["ticker"],
                        kind=kind,
                        exchange=row.get("exchange") or None,
                        name=row.get("name") or None,
                    )
                )
        return cls(name=name, symbols=tuple(symbols))

    def __len__(self) -> int:
        return len(self.symbols)

    def __iter__(self) -> Iterator[Symbol]:
        return iter(self.symbols)

    @property
    def tickers(self) -> list[str]:
        return [s.ticker for s in self.symbols]

    def select(self, predicate: Callable[[Symbol], bool]) -> "Universe":
        """Return a sub-universe of symbols matching ``predicate``."""
        return Universe(name=f"{self.name} [filtered]", symbols=tuple(s for s in self.symbols if predicate(s)))

    def without(self, *tickers: str) -> "Universe":
        """Return a copy excluding the given tickers."""
        banned = {t.strip().upper() for t in tickers}
        return Universe(name=f"{self.name} [exclusions]", symbols=tuple(s for s in self.symbols if s.ticker not in banned))
