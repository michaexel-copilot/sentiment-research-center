"""Core domain objects shared across pipeline stages."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal

AssetClass = Literal["stock", "crypto"]
Signal = Literal["BUY", "HOLD", "SELL", "NO SIGNAL"]


@dataclass
class Asset:
    symbol: str                 # NVDA, BTC
    name: str                   # NVIDIA Corporation, Bitcoin
    asset_class: AssetClass
    coingecko_id: str | None = None
    sector: str | None = None
    universes: list[str] = field(default_factory=list)
    rank: int | None = None

    @property
    def key(self) -> str:
        """Unique id across classes (a stock and a coin may share a ticker)."""
        return f"{self.asset_class}:{self.symbol}"

    @property
    def yf_symbol(self) -> str:
        return self.symbol.replace(".", "-")

    @property
    def short_name(self) -> str:
        """Company/coin name without legal suffixes, for text matching and search queries."""
        name = self.name.strip()
        suffixes = [", Inc.", " Inc.", " Inc", " Corporation", " Corp.", " Corp", " Holdings", " Company",
                    " plc", " PLC", " Ltd.", " Ltd", " N.V.", " S.A.", " Co.", " Group", " Incorporated",
                    " (Class A)", " (Class B)", " (Class C)", " Class A", " Class B", " Class C"]
        changed = True
        while changed:  # "Alphabet Inc. (Class A)" needs two passes
            changed = False
            for suffix in suffixes:
                if name.endswith(suffix):
                    name = name[: -len(suffix)].strip(" ,")
                    changed = True
        return name

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TextItem:
    asset_key: str
    source: str                 # google_news, reddit, stocktwits, ...
    kind: Literal["news", "social"]
    title: str
    body: str
    url: str
    published_at: datetime
    engagement: float = 0.0     # upvotes + comments, likes ...
    author_label: str | None = None   # StockTwits self-declared Bullish/Bearish

    @property
    def text_id(self) -> str:
        basis = self.url or f"{self.source}|{self.title}"
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]
