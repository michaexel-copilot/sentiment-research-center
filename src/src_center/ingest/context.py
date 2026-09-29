"""Market-wide context per asset class: fear & greed, volatility, rates, dominance, stablecoin liquidity."""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

import pandas as pd

from .. import http
from ..storage import db
from ..universe.builders import COINGECKO, coingecko_headers

log = logging.getLogger(__name__)

STOCK_KEY = "market:stock"
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*", "Origin": "https://edition.cnn.com", "Referer": "https://edition.cnn.com/",
}
CRYPTO_KEY = "market:crypto"


def _trend(series: pd.Series, days: int) -> float | None:
    s = series.dropna()
    if len(s) <= days:
        return None
    return float(s.iloc[-1] - s.iloc[-1 - days])


def stock_context() -> dict[str, Any]:
    import yfinance as yf
    out: dict[str, Any] = {}
    try:
        cnn = http.get("https://production.dataviz.cnn.io/index/fearandgreed/graphdata", ttl=3 * 3600,
                       headers=BROWSER_HEADERS)
        out["fear_greed"] = round(float(cnn["fear_and_greed"]["score"]), 1)
        out["fear_greed_rating"] = cnn["fear_and_greed"]["rating"]
        out["fear_greed_1w_ago"] = round(float(cnn["fear_and_greed"]["previous_1_week"]), 1)
    except (http.SourceUnavailable, KeyError, TypeError) as exc:
        log.info("CNN fear & greed unavailable: %s", exc)
    try:
        vix = yf.Ticker("^VIX").history(period="3mo")["Close"]
        out["vix"] = round(float(vix.iloc[-1]), 2)
        out["vix_20d_avg"] = round(float(vix.tail(20).mean()), 2)
        spy = yf.Ticker("SPY").history(period="1y")["Close"]
        out["spy_above_sma200"] = bool(spy.iloc[-1] > spy.tail(200).mean())
        out["spy_return_30d"] = round(float(spy.iloc[-1] / spy.iloc[-22] - 1), 4)
    except Exception as exc:  # noqa: BLE001
        log.info("VIX/SPY unavailable: %s", exc)
    try:
        tnx = yf.Ticker("^TNX").history(period="3mo")["Close"]
        out["us10y"] = round(float(tnx.iloc[-1]), 3)
        out["us10y_change_30d"] = _trend(tnx, 21)
    except Exception as exc:  # noqa: BLE001
        log.info("10Y yield unavailable: %s", exc)
    return out


def crypto_context() -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        fng = http.get("https://api.alternative.me/fng/", params={"limit": 30}, ttl=3 * 3600)["data"]
        out["fear_greed"] = int(fng[0]["value"])
        out["fear_greed_rating"] = fng[0]["value_classification"]
        out["fear_greed_1w_ago"] = int(fng[7]["value"]) if len(fng) > 7 else None
        out["fear_greed_30d_avg"] = round(sum(int(x["value"]) for x in fng) / len(fng), 1)
    except (http.SourceUnavailable, KeyError, IndexError) as exc:
        log.info("crypto fear & greed unavailable: %s", exc)
    try:
        g = http.get(f"{COINGECKO}/global", headers=coingecko_headers(), ttl=3600)["data"]
        out["btc_dominance"] = round(g["market_cap_percentage"]["btc"], 2)
        out["total_mcap_change_24h_pct"] = round(g["market_cap_change_percentage_24h_usd"], 2)
    except (http.SourceUnavailable, KeyError) as exc:
        log.info("CoinGecko global unavailable: %s", exc)
    try:
        chart = http.get("https://stablecoins.llama.fi/stablecoincharts/all", ttl=6 * 3600)
        caps = [float(p["totalCirculatingUSD"]["peggedUSD"]) for p in chart if p.get("totalCirculatingUSD")]
        if len(caps) > 31:
            out["stablecoin_supply_usd"] = caps[-1]
            out["stablecoin_supply_change_30d_pct"] = round((caps[-1] / caps[-31] - 1) * 100, 2)
    except (http.SourceUnavailable, KeyError) as exc:
        log.info("stablecoin supply unavailable: %s", exc)
    # BTC dominance trend from our own snapshot history
    hist = db.load_snapshot_history(CRYPTO_KEY, "context", days=40)
    if not hist.empty and "btc_dominance" in hist and out.get("btc_dominance") is not None:
        old = hist[pd.to_datetime(hist["date"]) <= pd.Timestamp.now() - pd.Timedelta(days=28)]
        if not old.empty and pd.notna(old.iloc[-1]["btc_dominance"]):
            out["btc_dominance_change_30d"] = round(out["btc_dominance"] - float(old.iloc[-1]["btc_dominance"]), 2)
    return out


def update(asset_class: str) -> dict[str, Any]:
    key = STOCK_KEY if asset_class == "stock" else CRYPTO_KEY
    ctx = stock_context() if asset_class == "stock" else crypto_context()
    ctx["fetched_at"] = datetime.utcnow().isoformat()
    db.save_snapshot(key, date.today(), "context", ctx)
    return ctx


def load(asset_class: str) -> dict[str, Any]:
    return db.load_snapshot(STOCK_KEY if asset_class == "stock" else CRYPTO_KEY, "context") or {}
