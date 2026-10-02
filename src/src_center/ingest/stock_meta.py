"""Stock fundamentals, positioning (short interest, options), insiders, analysts, earnings (yfinance)."""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from ..models import Asset
from ..storage import db
from ..timeutil import utcnow

log = logging.getLogger(__name__)

INFO_FIELDS = {
    "marketCap": "market_cap", "trailingPE": "pe", "forwardPE": "forward_pe", "priceToBook": "pb",
    "revenueGrowth": "revenue_growth", "earningsGrowth": "earnings_growth", "profitMargins": "profit_margin",
    "grossMargins": "gross_margin", "returnOnEquity": "roe", "debtToEquity": "debt_to_equity",
    "shortPercentOfFloat": "short_pct_float", "shortRatio": "short_ratio_days",
    "sharesShortPriorMonth": "shares_short_prior", "sharesShort": "shares_short",
    "recommendationMean": "analyst_rec_mean", "recommendationKey": "analyst_rec",
    "numberOfAnalystOpinions": "analyst_count", "targetMeanPrice": "target_mean",
    "currentPrice": "price", "fiftyTwoWeekHigh": "high_52w", "fiftyTwoWeekLow": "low_52w",
    "beta": "beta", "dividendYield": "dividend_yield", "sector": "sector", "industry": "industry",
}


def _clean(v: Any) -> Any:
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if hasattr(v, "item"):
        return v.item()
    return v


def _options(tk) -> dict[str, Any]:
    """Put/call volume & OI ratio over the next 3 expiries within 60 days, plus ATM implied vol."""
    out: dict[str, Any] = {}
    try:
        expiries = [e for e in tk.options if (date.fromisoformat(e) - date.today()).days <= 60][:3]
    except Exception:  # noqa: BLE001
        return out
    call_vol = put_vol = call_oi = put_oi = 0.0
    atm_ivs: list[float] = []
    spot = None
    try:
        spot = float(tk.fast_info["last_price"])
    except Exception:  # noqa: BLE001
        pass
    for exp in expiries:
        try:
            chain = tk.option_chain(exp)
        except Exception:  # noqa: BLE001
            continue
        call_vol += chain.calls["volume"].fillna(0).sum()
        put_vol += chain.puts["volume"].fillna(0).sum()
        call_oi += chain.calls["openInterest"].fillna(0).sum()
        put_oi += chain.puts["openInterest"].fillna(0).sum()
        if spot:
            calls = chain.calls.assign(dist=(chain.calls["strike"] - spot).abs()).sort_values("dist")
            if not calls.empty and calls.iloc[0]["impliedVolatility"] > 0.01:
                atm_ivs.append(float(calls.iloc[0]["impliedVolatility"]))
    if call_vol > 0:
        out["put_call_volume"] = round(put_vol / call_vol, 3)
    if call_oi > 0:
        out["put_call_oi"] = round(put_oi / call_oi, 3)
    if atm_ivs:
        out["atm_iv"] = round(sum(atm_ivs) / len(atm_ivs), 4)
    out["options_expiries"] = expiries
    return out


def _insiders(tk) -> dict[str, Any]:
    try:
        df = tk.insider_transactions
    except Exception:  # noqa: BLE001
        return {}
    if df is None or df.empty or "Start Date" not in df.columns:
        return {}
    df = df.copy()
    df["Start Date"] = pd.to_datetime(df["Start Date"], errors="coerce")
    df = df[df["Start Date"] >= pd.Timestamp.now() - pd.Timedelta(days=90)]
    text = df.get("Text", pd.Series("", index=df.index)).fillna("").str.lower()
    value = pd.to_numeric(df.get("Value"), errors="coerce").fillna(0)
    buys = value[text.str.contains("purchase")].sum()
    sells = value[text.str.contains("sale")].sum()
    return {"insider_buy_value_90d": float(buys), "insider_sell_value_90d": float(sells),
            "insider_buy_count_90d": int(text.str.contains("purchase").sum()),
            "insider_sell_count_90d": int(text.str.contains("sale").sum())}


def _earnings(tk) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        ed = tk.get_earnings_dates(limit=8)
    except Exception:  # noqa: BLE001
        ed = None
    if ed is not None and not ed.empty:
        ed = ed.copy()
        ed.index = pd.to_datetime(ed.index).tz_localize(None)
        now = pd.Timestamp.now()
        future = ed[ed.index > now]
        past = ed[ed.index <= now].dropna(subset=["Reported EPS"]) if "Reported EPS" in ed.columns else ed.iloc[0:0]
        if not future.empty:
            out["next_earnings"] = future.index.min().date().isoformat()
        if not past.empty:
            last = past.sort_index().iloc[-1]
            out["last_earnings"] = past.index.max().date().isoformat()
            surprise = last.get("Surprise(%)")
            if surprise is not None and not pd.isna(surprise):
                out["last_eps_surprise_pct"] = float(surprise)
    if "next_earnings" not in out:
        try:
            cal = tk.calendar or {}
            dates = cal.get("Earnings Date") or []
            upcoming = [d for d in dates if d >= date.today()]
            if upcoming:
                out["next_earnings"] = min(upcoming).isoformat()
        except Exception:  # noqa: BLE001
            pass
    return out


def _analyst_drift(tk) -> dict[str, Any]:
    """Net upgrades minus downgrades in the last 90 days."""
    try:
        ud = tk.upgrades_downgrades
    except Exception:  # noqa: BLE001
        return {}
    if ud is None or ud.empty:
        return {}
    ud = ud.copy()
    ud.index = pd.to_datetime(ud.index).tz_localize(None)
    recent = ud[ud.index >= pd.Timestamp.now() - pd.Timedelta(days=90)]
    action = recent.get("Action", pd.Series(dtype=str)).fillna("")
    return {"analyst_upgrades_90d": int((action == "up").sum()),
            "analyst_downgrades_90d": int((action == "down").sum())}


def update(asset: Asset) -> dict[str, Any]:
    import yfinance as yf
    tk = yf.Ticker(asset.yf_symbol)
    try:
        info = tk.info or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("yfinance info failed for %s: %s", asset.symbol, exc)
        info = {}
    meta: dict[str, Any] = {v: _clean(info.get(k)) for k, v in INFO_FIELDS.items() if info.get(k) is not None}
    if meta.get("target_mean") and meta.get("price"):
        meta["target_upside"] = round(meta["target_mean"] / meta["price"] - 1, 4)
    meta.update(_options(tk))
    meta.update(_insiders(tk))
    meta.update(_earnings(tk))
    meta.update(_analyst_drift(tk))
    meta["fetched_at"] = utcnow().isoformat()
    db.save_snapshot(asset.key, date.today(), "stock_meta", meta)
    return meta


def news(asset: Asset, since: datetime) -> list[dict[str, Any]]:
    """yfinance news items (title, summary, link, time)."""
    import yfinance as yf
    try:
        items = yf.Ticker(asset.yf_symbol).news or []
    except Exception:  # noqa: BLE001
        return []
    out = []
    for it in items:
        c = it.get("content", it)
        title = c.get("title")
        link = (c.get("canonicalUrl") or {}).get("url") or c.get("link") or ""
        ts = c.get("pubDate") or c.get("providerPublishTime")
        if isinstance(ts, (int, float)):
            published = datetime.utcfromtimestamp(ts)
        elif ts:
            published = pd.Timestamp(ts).tz_localize(None).to_pydatetime() if pd.Timestamp(ts).tzinfo is None \
                else pd.Timestamp(ts).tz_convert(None).to_pydatetime()
        else:
            continue
        if title and published >= since - timedelta(days=0):
            out.append({"title": title, "body": c.get("summary") or "", "url": link, "published_at": published,
                        "publisher": (c.get("provider") or {}).get("displayName")})
    return out
