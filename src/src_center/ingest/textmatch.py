"""Mention matching and pre-LLM text hygiene (dedupe, length filter, cap)."""

from __future__ import annotations

import re
from functools import lru_cache

from rapidfuzz import fuzz

from ..models import Asset, TextItem

# Tickers that are common English words / abbreviations: only match them as $CASHTAG or by name.
AMBIGUOUS = {
    "A", "ALL", "AN", "ARE", "BE", "BIG", "CAN", "CAT", "COST", "DAY", "DD", "DOW", "EL", "FAST", "FOR", "GO",
    "GOOD", "HAS", "HD", "HE", "IT", "KEY", "LOW", "MA", "MO", "NOW", "ON", "ONE", "OR", "OUT", "PM", "RE",
    "SO", "TECH", "TRUE", "TT", "UP", "USA", "V", "WELL", "WAT", "AI", "ARM", "BILL", "CASH", "CHAT", "COIN",
    "EDGE", "EPS", "FUN", "GAIN", "HOPE", "IMO", "JOB", "LIFE", "LOVE", "MAN", "MOON", "OPEN", "PAY", "PEAK",
    "PLAY", "REAL", "RUN", "SAFE", "SEE", "SHIP", "STAR", "TEAM", "TOP", "WIN", "YOLO", "CEO", "IPO", "ETF",
    "USD", "SEC", "FED", "GDP", "CPI", "ATH", "FOMO", "HODL", "DOGE", "APE", "PEPE", "LINK", "SOL", "NEAR",
    "ONDO", "TAO", "ENA", "SUI", "OM", "S", "HYPE", "TON", "DOT", "ATOM", "ICP", "FET", "RENDER", "MOVE",
    "FLOW", "GAS", "SAND", "MANA", "AXS", "IMX", "STX", "JUP", "W", "ZRO", "PYTH", "CORE", "BEAM", "KAS",
    "ENS", "INJ", "OP", "ARB", "APT", "SEI", "TIA", "LDO", "CRV", "AAVE", "GT", "LEO", "XDC", "BGB",
}

# Name words that are too generic to identify a company on their own.
GENERIC_FIRST_WORDS = {
    "american", "first", "general", "united", "international", "national", "the", "bank", "global", "new",
    "southern", "western", "eastern", "north", "south", "public", "digital", "advanced", "applied", "consolidated",
    "energy", "health", "life", "real", "capital", "state", "texas", "pacific", "atlantic", "universal", "royal",
    "central", "federal", "principal", "cardinal", "arch", "air", "church", "welltower", "live",
}


@lru_cache(maxsize=4096)
def _patterns(symbol: str, name: str, asset_class: str) -> list[re.Pattern]:
    pats = [re.compile(rf"\${re.escape(symbol)}\b", re.IGNORECASE)]
    if symbol.upper() not in AMBIGUOUS and len(symbol) >= 3:
        pats.append(re.compile(rf"(?<![\w$]){re.escape(symbol)}(?!\w)"))  # uppercase ticker, case-sensitive
    if name and len(name) >= 4:
        pats.append(re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE))
        first = name.split()[0].strip(".,&'")
        if first != name and len(first) >= 5 and first.lower() not in GENERIC_FIRST_WORDS:
            # "Costco" for "Costco Wholesale": capitalized only, to avoid plain-word hits ("apple pie")
            pats.append(re.compile(rf"\b{re.escape(first[0].upper() + first[1:])}\b"))
    if asset_class == "crypto":
        pats.append(re.compile(rf"\b{re.escape(symbol)}(?:USDT|/USD|-USD)\b", re.IGNORECASE))
    return pats


def mentions(asset: Asset, text: str) -> bool:
    return any(p.search(text) for p in _patterns(asset.symbol, asset.short_name, asset.asset_class))


def filter_and_dedupe(items: list[TextItem], min_chars: int, body_chars: int) -> list[TextItem]:
    """Drop short items, exact URL dupes and near-identical titles (syndicated news)."""
    out: list[TextItem] = []
    seen_urls: set[str] = set()
    titles: list[str] = []
    for it in sorted(items, key=lambda i: i.engagement, reverse=True):
        it.title = (it.title or "").strip()
        it.body = re.sub(r"\s+", " ", (it.body or "")).strip()[:body_chars]
        if len(it.title) + len(it.body) < min_chars:
            continue
        if it.url and it.url in seen_urls:
            continue
        norm = re.sub(r"\W+", " ", it.title.lower()).strip()
        if norm and any(fuzz.token_set_ratio(norm, t) >= 90 for t in titles[-300:]):
            continue
        seen_urls.add(it.url)
        titles.append(norm)
        out.append(it)
    return out
