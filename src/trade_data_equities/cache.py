"""On-disk cache for fetched market data.

The cache stores **unadjusted** provider responses as JSON so that:

* repeated backtests and dashboard refreshes never re-hit the vendor,
* corporate-action adjustments stay a read-time choice, and
* cache entries remain valid even if adjustment methodology changes.

Layout: ``<root>/<namespace>/<sha1-key>.json`` where the namespace is the
provider name (``"corp"`` is reserved for corporate actions). Each file
holds ``{"fetched_at": <iso>, "payload": [...]}``; entries older than the
per-timeframe TTL are treated as misses.

The cache is deliberately stdlib-only (JSON, not parquet) so the core
engine has zero mandatory dependencies. For very large universes, point
``root`` at fast local storage and raise ``max_entries`` handling to the
caller -- the cache never evicts on its own.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import Bar, Dividend, Split, Symbol, Timeframe, ensure_utc

_DEFAULT_TTL = {
    "intraday": timedelta(minutes=15),
    "daily": timedelta(hours=12),
    "weekly": timedelta(days=7),
    "monthly": timedelta(days=30),
    "corp": timedelta(days=7),
}


def _default_root() -> Path:
    return Path(os.environ.get("TRADE_DATA_CACHE", Path.home() / ".cache" / "trade-data-equities"))


def _iso(value: date | datetime) -> str:
    if isinstance(value, datetime):
        return ensure_utc(value).isoformat()
    return value.isoformat()


class DiskCache:
    """JSON file cache keyed by provider, symbol, timeframe and range."""

    def __init__(
        self,
        root: str | Path | None = None,
        ttl: dict[str, timedelta] | None = None,
    ) -> None:
        self.root = Path(root) if root is not None else _default_root()
        self.ttl = {**_DEFAULT_TTL, **(ttl or {})}

    # -- keying -------------------------------------------------------
    @staticmethod
    def _key(*parts: str) -> str:
        digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
        return digest

    def _path(self, namespace: str, key: str) -> Path:
        return self.root / namespace / f"{key}.json"

    def _ttl_for(self, timeframe: Timeframe | None, namespace: str) -> timedelta:
        if namespace == "corp":
            return self.ttl["corp"]
        if timeframe is None:
            return self.ttl["daily"]
        if timeframe.is_intraday:
            return self.ttl["intraday"]
        return self.ttl[{"1d": "daily", "1wk": "weekly", "1mo": "monthly"}[timeframe.value]]

    # -- bars ---------------------------------------------------------
    def bars_key(
        self,
        provider: str,
        symbol: Symbol,
        timeframe: Timeframe,
        start: date | datetime,
        end: date | datetime,
    ) -> str:
        return self._key(provider, symbol.ticker, timeframe.value, _iso(start), _iso(end))

    def get_bars(
        self,
        provider: str,
        symbol: Symbol,
        timeframe: Timeframe,
        start: date | datetime,
        end: date | datetime,
    ) -> list[Bar] | None:
        raw = self._read(
            provider, self.bars_key(provider, symbol, timeframe, start, end), self._ttl_for(timeframe, provider)
        )
        if raw is None:
            return None
        return [
            Bar(
                timestamp=datetime.fromisoformat(item["timestamp"]),
                open=item["open"],
                high=item["high"],
                low=item["low"],
                close=item["close"],
                volume=item["volume"],
            )
            for item in raw
        ]

    def put_bars(
        self,
        provider: str,
        symbol: Symbol,
        timeframe: Timeframe,
        start: date | datetime,
        end: date | datetime,
        bars: list[Bar],
    ) -> None:
        payload = [
            {
                "timestamp": bar.timestamp.isoformat(),
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
            }
            for bar in bars
        ]
        self._write(provider, self.bars_key(provider, symbol, timeframe, start, end), payload)

    # -- corporate actions --------------------------------------------
    def get_actions(
        self, provider: str, symbol: Symbol, kind: str, start: date | datetime, end: date | datetime
    ) -> list | None:
        raw = self._read("corp", self._key(provider, symbol.ticker, kind, _iso(start), _iso(end)), self.ttl["corp"])
        if raw is None:
            return None
        cls = Split if kind == "splits" else Dividend
        field = "ratio" if kind == "splits" else "amount"
        return [cls(ex_date=date.fromisoformat(item["ex_date"]), **{field: item[field]}) for item in raw]

    def put_actions(
        self, provider: str, symbol: Symbol, kind: str, start: date | datetime, end: date | datetime, actions: list
    ) -> None:
        field = "ratio" if kind == "splits" else "amount"
        payload = [{"ex_date": a.ex_date.isoformat(), field: getattr(a, field)} for a in actions]
        self._write("corp", self._key(provider, symbol.ticker, kind, _iso(start), _iso(end)), payload)

    # -- file io ------------------------------------------------------
    def _read(self, namespace: str, key: str, ttl: timedelta) -> Any | None:
        path = self._path(namespace, key)
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        fetched = datetime.fromisoformat(doc["fetched_at"])
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - fetched > ttl:
            return None
        return doc["payload"]

    def _write(self, namespace: str, key: str, payload: Any) -> None:
        path = self._path(namespace, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = {"fetched_at": datetime.now(timezone.utc).isoformat(), "payload": payload}
        path.write_text(json.dumps(doc), encoding="utf-8")

    def clear(self, namespace: str | None = None) -> int:
        """Delete cached files; returns the number removed."""
        targets = [self.root / namespace] if namespace else [self.root]
        removed = 0
        for target in targets:
            if not target.exists():
                continue
            for path in target.rglob("*.json"):
                path.unlink()
                removed += 1
        return removed
