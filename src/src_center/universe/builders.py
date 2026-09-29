"""Universe builders: S&P 500 and Nasdaq 100 (Wikipedia), crypto top-N (CoinGecko, hygiene-filtered)."""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from io import StringIO

import pandas as pd

from .. import config, http
from ..models import Asset
from ..storage import db

log = logging.getLogger(__name__)

COINGECKO = "https://api.coingecko.com/api/v3"
UNIVERSES = ("sp500", "nasdaq100", "crypto100")
TOP_TAGS = {"crypto": "crypto_top", "nasdaq100": "nasdaq100_top", "sp500": "sp500_top"}


def coingecko_headers() -> dict[str, str]:
    key = config.env("COINGECKO_DEMO_API_KEY")
    return {"x-cg-demo-api-key": key} if key else {}


def _wiki_tables(url: str) -> list[pd.DataFrame]:
    html = http.get(url, as_json=False, ttl=86400)
    return pd.read_html(StringIO(html))


def _clean_name(name: str) -> str:
    """Wikipedia index style 'Lilly (Eli)' -> 'Eli Lilly' (share-class parentheses are kept)."""
    m = re.fullmatch(r"(.+?) \(([^)]+)\)", name.strip())
    if m and not m.group(2).lower().startswith("class"):
        return f"{m.group(2)} {m.group(1)}"
    return name.strip()


def sp500() -> list[Asset]:
    tables = _wiki_tables("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
    df = next(t for t in tables if "Symbol" in t.columns and "Security" in t.columns)
    return [
        Asset(symbol=str(r["Symbol"]).strip(), name=_clean_name(str(r["Security"])), asset_class="stock",
              sector=str(r.get("GICS Sector", "")) or None, universes=["sp500"])
        for _, r in df.iterrows()
    ]


def nasdaq100() -> list[Asset]:
    # Wikipedia moved the constituents table off the index article; try the list page first.
    df = None
    for url in ("https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies",
                "https://en.wikipedia.org/wiki/Nasdaq-100"):
        try:
            tables = _wiki_tables(url)
        except http.SourceUnavailable:
            continue
        df = next((t for t in tables if "Ticker" in t.columns and "Company" in t.columns and len(t) > 90), None)
        if df is not None:
            break
    if df is None:
        raise RuntimeError("Nasdaq-100 constituents table not found on Wikipedia")
    sector_col = next((c for c in df.columns if "Sector" in str(c) or "Industry" in str(c)), None)
    return [
        Asset(symbol=str(r["Ticker"]).strip(), name=str(r["Company"]).strip(), asset_class="stock",
              sector=str(r[sector_col]) if sector_col else None, universes=["nasdaq100"])
        for _, r in df.iterrows()
    ]


def _excluded_coin_ids() -> set[str]:
    ids: set[str] = set()
    for cat in config.settings()["universe"]["crypto_exclude_categories"]:
        try:
            coins = http.get(f"{COINGECKO}/coins/markets", ttl=7 * 86400, headers=coingecko_headers(),
                             params={"vs_currency": "usd", "category": cat, "per_page": 250, "page": 1})
            ids.update(c["id"] for c in coins)
        except http.SourceUnavailable as exc:
            log.warning("could not load CoinGecko category %s: %s", cat, exc)
    return ids


def market_snapshot() -> list[dict]:
    """Top-250 coins by market cap (cached 1h; also the reference price source for price sanity checks)."""
    return http.get(f"{COINGECKO}/coins/markets", ttl=3600, headers=coingecko_headers(),
                    params={"vs_currency": "usd", "order": "market_cap_desc", "per_page": 250, "page": 1,
                            "price_change_percentage": "24h,7d,30d"})


def _stable_value(coin: dict) -> bool:
    """Stablecoins, tokenized treasuries/credit funds and pegged tokens: prices that barely move carry no sentiment."""
    moves = [coin.get(f"price_change_percentage_{p}_in_currency") for p in ("24h", "7d", "30d")]
    moves = [abs(m) for m in moves if m is not None]
    limits = (0.5, 1.5, 3.0)
    return len(moves) >= 2 and all(m < lim for m, lim in zip(moves, limits))


def crypto_top(n: int | None = None) -> list[Asset]:
    n = n or config.settings()["universe"]["crypto_top_n"]
    excluded_ids = _excluded_coin_ids()
    excluded_symbols = {s.upper() for s in config.settings()["universe"]["crypto_exclude_symbols"]}
    coins = market_snapshot()
    assets: list[Asset] = []
    for c in coins:
        sym = c["symbol"].upper()
        if c["id"] in excluded_ids or sym in excluded_symbols or not sym.isascii():
            continue
        if _stable_value(c):
            continue
        assets.append(Asset(symbol=sym, name=c["name"], asset_class="crypto", coingecko_id=c["id"],
                            universes=["crypto100"], rank=len(assets) + 1))
        if len(assets) >= n:
            break
    return assets


def refresh(which: list[str] | None = None) -> dict[str, int]:
    """Rebuild universes and persist them to the assets table. Returns counts per universe."""
    which = which or list(UNIVERSES)
    merged: dict[str, Asset] = {}
    counts: dict[str, int] = {}
    builders = {"sp500": sp500, "nasdaq100": nasdaq100, "crypto100": crypto_top}
    for name in which:
        assets = builders[name]()
        counts[name] = len(assets)
        for a in assets:
            if a.key in merged:
                merged[a.key].universes = sorted(set(merged[a.key].universes) | set(a.universes))
                merged[a.key].sector = merged[a.key].sector or a.sector
            else:
                merged[a.key] = a
    # Drop the refreshed universes' memberships, then re-add current members (keeps other universes intact).
    retagged = _tag_top(merged, which)
    refreshed = set(which) | retagged
    existing = load_assets()
    for a in existing:
        kept = [u for u in a.universes if u not in refreshed]
        if a.key in merged:
            merged[a.key].universes = sorted(set(merged[a.key].universes) | set(kept))
            if merged[a.key].rank is None:
                merged[a.key].rank = a.rank  # ranking skipped this time: keep the previous one
        else:
            a.universes = kept
            merged[a.key] = a
    now = datetime.utcnow()
    df = pd.DataFrame([{
        "key": a.key, "symbol": a.symbol, "name": a.name, "asset_class": a.asset_class,
        "coingecko_id": a.coingecko_id, "sector": a.sector, "universes": ",".join(a.universes),
        "rank": a.rank, "updated_at": now,
    } for a in merged.values()])
    db.upsert_df("assets", df)
    return counts


def _market_caps(assets: list[Asset]) -> dict[str, float]:
    import yfinance as yf

    def cap(a: Asset) -> tuple[str, float]:
        for attempt in range(2):
            try:
                tk = yf.Ticker(a.yf_symbol)
                value = tk.fast_info["market_cap"] if attempt == 0 else (tk.info or {}).get("marketCap")
                if value:
                    return a.key, float(value)
            except Exception:  # noqa: BLE001 - yfinance raises many types
                pass
        return a.key, 0.0

    with ThreadPoolExecutor(max_workers=8) as ex:
        caps = dict(ex.map(cap, assets))
    # Throttled lookups return nothing: fall back to the last known cap so ranks don't jump.
    previous = db.load_snapshot("universe:stock", "market_caps") or {}
    missing = [k for k, v in caps.items() if not v]
    for k in missing:
        caps[k] = float(previous.get(k, 0.0))
    if missing:
        log.warning("market cap unavailable for %d stocks (%s...); using last known values",
                    len(missing), ", ".join(k.split(":")[1] for k in missing[:8]))
    fresh = {k: v for k, v in caps.items() if k not in missing}
    db.save_snapshot("universe:stock", datetime.utcnow().date(), "market_caps", {**previous, **fresh})
    return caps


def _company(a: Asset) -> str:
    """Company identity across share classes and lists (GOOG/GOOGL, 'Alphabet Inc. (Class A)')."""
    return "".join(ch for ch in a.short_name.lower() if ch.isalnum())


def _tag_top(merged: dict[str, Asset], which: list[str]) -> set[str]:
    """Rank stocks by market cap and tag the observed top-N members of each list (one line per company).
    Returns the top tags that were recomputed (the others keep their previous members)."""
    cfg = config.settings()["universe"]["observed"]
    retagged: set[str] = set()
    stocks = [a for a in merged.values() if a.asset_class == "stock"]
    if stocks and ("sp500" in which or "nasdaq100" in which):
        caps = _market_caps(stocks)
        coverage = sum(1 for v in caps.values() if v > 0) / len(caps)
        if coverage < 0.8:
            log.error("market caps known for only %.0f%% of stocks (Yahoo rate limit?); keeping the previous "
                      "stock top lists. Retry `src universe` later.", coverage * 100)
            stocks = []
    if stocks:
        for rank, a in enumerate(sorted(stocks, key=lambda a: -caps[a.key]), start=1):
            a.rank = rank
        taken: set[str] = set()
        for index in ("nasdaq100", "sp500"):  # Nasdaq first: its top names are also S&P members
            if index not in which:
                continue
            picked: list[Asset] = []
            seen: set[str] = set(taken) if cfg.get("distinct_stocks", True) else set()
            for a in sorted((a for a in stocks if index in a.universes), key=lambda a: a.rank):
                if len(picked) >= cfg[index]:
                    break
                if _company(a) in seen:
                    continue
                seen.add(_company(a))
                picked.append(a)
            for a in picked:
                a.universes = sorted(set(a.universes) | {TOP_TAGS[index]})
                taken.add(_company(a))
            retagged.add(TOP_TAGS[index])
    if "crypto100" in which:
        retagged.add(TOP_TAGS["crypto"])
        for a in merged.values():
            if a.asset_class == "crypto" and "crypto100" in a.universes and a.rank and a.rank <= cfg["crypto"]:
                a.universes = sorted(set(a.universes) | {TOP_TAGS["crypto"]})
    return retagged


def _row_to_asset(r) -> Asset:
    return Asset(symbol=r["symbol"], name=r["name"], asset_class=r["asset_class"],
                 coingecko_id=r["coingecko_id"] or None, sector=r["sector"] or None,
                 universes=[u for u in (r["universes"] or "").split(",") if u],
                 rank=None if pd.isna(r["rank"]) else int(r["rank"]))


def load_assets(universe: str | None = None) -> list[Asset]:
    df = db.query_df("SELECT * FROM assets ORDER BY asset_class, rank NULLS LAST, symbol")
    assets = [_row_to_asset(r) for _, r in df.iterrows()]
    if universe == "stocks":
        return [a for a in assets if a.asset_class == "stock" and a.universes]
    if universe == "observed":
        from . import watchlist
        observed = [a for a in assets if set(a.universes) & set(TOP_TAGS.values())]
        keys = {a.key for a in observed}
        for a in watchlist.assets():
            if a.key not in keys:
                observed.append(a)
                keys.add(a.key)
        return observed
    if universe:
        return [a for a in assets if universe in a.universes]
    return assets


def resolve(symbol: str, asset_class: str | None = None) -> Asset:
    """Find an asset by ticker (from the stored universes, else look it up ad hoc)."""
    symbol = symbol.upper().strip()
    df = db.query_df("SELECT * FROM assets WHERE upper(symbol) = ?", [symbol])
    if asset_class:
        df = df[df["asset_class"] == asset_class]
    if len(df) >= 1:
        # A ticker in both classes (rare) defaults to the stock unless the class is given.
        df = df.sort_values("asset_class", key=lambda s: s.map({"stock": 0, "crypto": 1}))
        return _row_to_asset(df.iloc[0])
    asset = _adhoc_lookup(symbol, asset_class)
    db.upsert_df("assets", pd.DataFrame([{
        "key": asset.key, "symbol": asset.symbol, "name": asset.name, "asset_class": asset.asset_class,
        "coingecko_id": asset.coingecko_id, "sector": asset.sector, "universes": "", "rank": asset.rank,
        "updated_at": datetime.utcnow(),
    }]))
    return asset


def _adhoc_lookup(symbol: str, asset_class: str | None) -> Asset:
    if asset_class != "crypto":
        import yfinance as yf
        try:
            info = yf.Ticker(symbol.replace(".", "-")).info or {}
        except Exception:  # noqa: BLE001 - yfinance raises many types
            info = {}
        if info.get("quoteType") == "EQUITY":
            return Asset(symbol=symbol, name=info.get("longName") or info.get("shortName") or symbol,
                         asset_class="stock", sector=info.get("sector"))
    if asset_class != "stock":
        res = http.get(f"{COINGECKO}/search", params={"query": symbol}, headers=coingecko_headers(), ttl=86400)
        coins = [c for c in res.get("coins", []) if c["symbol"].upper() == symbol]
        if coins:
            best = min(coins, key=lambda c: c.get("market_cap_rank") or 10**9)
            return Asset(symbol=symbol, name=best["name"], asset_class="crypto", coingecko_id=best["id"],
                         rank=best.get("market_cap_rank"))
    raise ValueError(f"Could not resolve asset '{symbol}' as a stock or crypto asset")
