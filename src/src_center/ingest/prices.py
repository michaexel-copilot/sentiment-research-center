"""Daily OHLCV: yfinance for stocks, Binance spot klines (fallback CoinGecko) for crypto."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

from .. import http
from ..models import Asset
from ..storage import db
from ..universe.builders import COINGECKO, coingecko_headers, market_snapshot
from ..timeutil import utcnow

log = logging.getLogger(__name__)

BINANCE = "https://api.binance.com"
HISTORY_DAYS = 730


def _last_date(asset: Asset) -> pd.Timestamp | None:
    df = db.query_df("SELECT max(date) AS d FROM prices WHERE asset_key = ?", [asset.key])
    d = df.iloc[0]["d"]
    return None if pd.isna(d) else pd.Timestamp(d)


def _store(asset: Asset, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    df = df.reset_index().rename(columns=str.lower)
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize().dt.date
    df["asset_key"] = asset.key
    df = df[["asset_key", "date", "open", "high", "low", "close", "volume"]].dropna(subset=["close"])
    return db.upsert_df("prices", df.drop_duplicates(subset=["date"], keep="last"))


def update_stock(asset: Asset) -> int:
    import yfinance as yf
    from yfinance.exceptions import YFRateLimitError
    last = _last_date(asset)
    start = (last - timedelta(days=7)) if last is not None else datetime.now() - timedelta(days=HISTORY_DAYS)
    for attempt in range(4):  # Yahoo throttles bursts; back off 15s, 45s, 90s
        try:
            hist = yf.Ticker(asset.yf_symbol).history(start=start.strftime("%Y-%m-%d"), interval="1d",
                                                      auto_adjust=True)
            break
        except YFRateLimitError:
            if attempt == 3:
                raise
            wait = (15, 45, 90)[attempt]
            log.info("Yahoo rate limit for %s; retrying in %ss", asset.symbol, wait)
            time.sleep(wait)
    if hist.empty:
        raise RuntimeError(f"yfinance returned no prices for {asset.symbol}")
    hist.index.name = "date"
    return _store(asset, hist[["Open", "High", "Low", "Close", "Volume"]])


def _binance_klines(symbol: str, start: datetime) -> pd.DataFrame:
    rows: list[list] = []
    start_ms = int(start.replace(tzinfo=timezone.utc).timestamp() * 1000)
    while True:
        batch = http.get(f"{BINANCE}/api/v3/klines",
                         params={"symbol": f"{symbol}USDT", "interval": "1d", "startTime": start_ms, "limit": 1000})
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 1000:
            break
        start_ms = batch[-1][0] + 86_400_000
    df = pd.DataFrame(rows, columns=["t", "open", "high", "low", "close", "volume", "ct", "qv", "n", "tb", "tq", "i"])
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["t"], unit="ms")
    out = df.set_index("date")[["open", "high", "low", "close"]].astype(float)
    out["volume"] = df.set_index("date")["qv"].astype(float)  # quote volume in USDT
    return out


def _coingecko_chart(coin_id: str, days: int) -> pd.DataFrame:
    data = http.get(f"{COINGECKO}/coins/{coin_id}/market_chart", headers=coingecko_headers(),
                    params={"vs_currency": "usd", "days": min(days, 365), "interval": "daily"})
    px = pd.DataFrame(data["prices"], columns=["t", "close"])
    vol = pd.DataFrame(data["total_volumes"], columns=["t", "volume"])
    df = px.merge(vol, on="t")
    df["date"] = pd.to_datetime(df["t"], unit="ms")
    df = df.set_index("date")
    df["open"] = df["high"] = df["low"] = df["close"]  # CoinGecko free gives closes only
    return df[["open", "high", "low", "close", "volume"]]


def _reference_price(asset: Asset) -> float | None:
    if not asset.coingecko_id:
        return None
    try:
        coin = next((c for c in market_snapshot() if c["id"] == asset.coingecko_id), None)
        if coin is None:
            coin = http.get(f"{COINGECKO}/simple/price", headers=coingecko_headers(), ttl=3600,
                            params={"ids": asset.coingecko_id, "vs_currencies": "usd"}).get(asset.coingecko_id, {})
            return coin.get("usd")
        return coin.get("current_price")
    except http.SourceUnavailable:
        return None


def update_crypto(asset: Asset) -> int:
    last = _last_date(asset)
    start = (last - timedelta(days=3)).to_pydatetime() if last is not None else utcnow() - timedelta(days=HISTORY_DAYS)
    df = pd.DataFrame()
    try:
        df = _binance_klines(asset.symbol, start)
    except http.SourceUnavailable as exc:
        log.info("binance klines unavailable for %s (%s)", asset.symbol, exc)
    ref = _reference_price(asset)
    if not df.empty and ref and abs(df["close"].iloc[-1] / ref - 1) > 0.1:
        # Same ticker, different token on Binance (or a broken pair): trust CoinGecko instead.
        log.warning("binance %sUSDT price %.4g != CoinGecko %.4g; using CoinGecko", asset.symbol,
                    df["close"].iloc[-1], ref)
        df = pd.DataFrame()
    if asset.coingecko_id and (df.empty or (last is None and len(df) < 300)):
        cg = _coingecko_chart(asset.coingecko_id, (utcnow() - start).days + 1)
        # Recently listed on Binance: CoinGecko history before the first Binance candle, Binance OHLC after.
        df = cg if df.empty else pd.concat([cg[cg.index < df.index[0]], df])
    if df.empty:
        raise RuntimeError(f"no price source for {asset.symbol}")
    return _store(asset, df)


def update(asset: Asset) -> int:
    return update_stock(asset) if asset.asset_class == "stock" else update_crypto(asset)
