"""Crypto fundamentals-lite (CoinGecko, DefiLlama) and derivatives positioning (Binance USDⓈ-M futures)."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from .. import http
from ..models import Asset
from ..storage import db
from ..universe.builders import COINGECKO, coingecko_headers
from ..timeutil import utcnow

log = logging.getLogger(__name__)

BINANCE_FAPI = "https://fapi.binance.com"
LLAMA = "https://api.llama.fi"


def coingecko(asset: Asset) -> dict[str, Any]:
    if not asset.coingecko_id:
        return {}
    c = http.get(f"{COINGECKO}/coins/{asset.coingecko_id}", headers=coingecko_headers(), ttl=6 * 3600, params={
        "localization": "false", "tickers": "false", "market_data": "true",
        "community_data": "true", "developer_data": "true", "sparkline": "false"})
    md = c.get("market_data") or {}
    usd = lambda k: (md.get(k) or {}).get("usd")  # noqa: E731
    dev = c.get("developer_data") or {}
    out = {
        "market_cap": usd("market_cap"), "fdv": usd("fully_diluted_valuation"), "volume_24h": usd("total_volume"),
        "market_cap_rank": c.get("market_cap_rank"), "ath": usd("ath"), "ath_change_pct": usd("ath_change_percentage"),
        "circulating_supply": md.get("circulating_supply"), "total_supply": md.get("total_supply"),
        "max_supply": md.get("max_supply"),
        "sentiment_votes_up_pct": c.get("sentiment_votes_up_percentage"),
        "watchlist_users": c.get("watchlist_portfolio_users"),
        "dev_commits_4w": dev.get("commit_count_4_weeks"), "dev_stars": dev.get("stars"),
        "categories": (c.get("categories") or [])[:6],
        "subreddit": ((c.get("links") or {}).get("subreddit_url") or "").rstrip("/").split("/")[-1] or None,
        "description": ((c.get("description") or {}).get("en") or "")[:400],
    }
    if out["fdv"] and out["market_cap"]:
        out["fdv_mc_ratio"] = round(out["fdv"] / out["market_cap"], 3)
    return {k: v for k, v in out.items() if v is not None}


def defillama(asset: Asset) -> dict[str, Any]:
    if not asset.coingecko_id:
        return {}
    try:
        chains = http.get(f"{LLAMA}/v2/chains", ttl=6 * 3600)
        match = next((c for c in chains if c.get("gecko_id") == asset.coingecko_id), None)
        if match:
            hist = http.get(f"{LLAMA}/v2/historicalChainTvl/{match['name']}", ttl=6 * 3600)
            return _tvl_stats([h["tvl"] for h in hist], "chain")
        protocols = http.get(f"{LLAMA}/protocols", ttl=6 * 3600)
        match = next((p for p in protocols if p.get("gecko_id") == asset.coingecko_id), None)
        if match:
            out = {"tvl": match.get("tvl"), "tvl_change_7d_pct": match.get("change_7d"), "tvl_kind": "protocol"}
            if match.get("change_1d") is not None and match.get("mcap"):
                out["mcap_tvl_ratio"] = round(match["mcap"] / match["tvl"], 3) if match.get("tvl") else None
            return {k: v for k, v in out.items() if v is not None}
    except http.SourceUnavailable as exc:
        log.info("DefiLlama unavailable: %s", exc)
    return {}


def _tvl_stats(series: list[float], kind: str) -> dict[str, Any]:
    if len(series) < 31:
        return {}
    now, w, m = series[-1], series[-8], series[-31]
    return {"tvl": now, "tvl_kind": kind,
            "tvl_change_7d_pct": round((now / w - 1) * 100, 2) if w else None,
            "tvl_change_30d_pct": round((now / m - 1) * 100, 2) if m else None}


def derivatives(asset: Asset) -> dict[str, Any]:
    """Perp funding (7d avg), open interest change, global long/short account ratio."""
    symbol = f"{asset.symbol}USDT"
    out: dict[str, Any] = {}
    try:
        funding = http.get(f"{BINANCE_FAPI}/fapi/v1/fundingRate", params={"symbol": symbol, "limit": 21})
    except http.SourceUnavailable:
        return out  # no perp market (or Binance futures blocked in this region)
    if not funding:
        return out
    from .prices import _reference_price
    ref = _reference_price(asset)
    try:
        mark = float(http.get(f"{BINANCE_FAPI}/fapi/v1/premiumIndex", params={"symbol": symbol})["markPrice"])
    except (http.SourceUnavailable, KeyError, TypeError, ValueError):
        mark = None
    if ref and mark and abs(mark / ref - 1) > 0.1:
        log.warning("binance perp %s mark %.4g != CoinGecko %.4g; skipping derivatives", symbol, mark, ref)
        return out
    rates = [float(f["fundingRate"]) for f in funding]
    out["funding_rate_last"] = rates[-1]
    out["funding_rate_7d_avg"] = sum(rates) / len(rates)
    try:
        oi = http.get(f"{BINANCE_FAPI}/futures/data/openInterestHist",
                      params={"symbol": symbol, "period": "1d", "limit": 30})
        if len(oi) >= 8:
            vals = [float(x["sumOpenInterestValue"]) for x in oi]
            out["open_interest_usd"] = vals[-1]
            out["oi_change_7d_pct"] = round((vals[-1] / vals[-8] - 1) * 100, 2)
            out["oi_change_30d_pct"] = round((vals[-1] / vals[0] - 1) * 100, 2)
        ls = http.get(f"{BINANCE_FAPI}/futures/data/globalLongShortAccountRatio",
                      params={"symbol": symbol, "period": "1d", "limit": 7})
        if ls:
            out["long_short_ratio"] = float(ls[-1]["longShortRatio"])
            out["long_short_ratio_7d_avg"] = sum(float(x["longShortRatio"]) for x in ls) / len(ls)
    except http.SourceUnavailable as exc:
        log.info("Binance futures data partial for %s: %s", asset.symbol, exc)
    return out


def update(asset: Asset) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    for part in (coingecko, defillama):
        try:
            meta.update(part(asset))
        except http.SourceUnavailable as exc:
            log.warning("%s failed for %s: %s", part.__name__, asset.symbol, exc)
    meta["fetched_at"] = utcnow().isoformat()
    db.save_snapshot(asset.key, date.today(), "crypto_meta", meta)
    deriv = derivatives(asset)
    if deriv:
        db.save_snapshot(asset.key, date.today(), "derivatives", deriv)
    return {**meta, **deriv}
