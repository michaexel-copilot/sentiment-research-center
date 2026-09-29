"""Human-readable fact sheet (preformatted strings). The one-pager, the report and the LLM all read the same values,
so any number the narrative quotes can be checked against it."""

from __future__ import annotations

from typing import Any

from ..models import Asset


def pct(v: float | None, digits: int = 1, signed: bool = True) -> str | None:
    if v is None:
        return None
    return f"{v * 100:+.{digits}f}%" if signed else f"{v * 100:.{digits}f}%"


def money(v: float | None) -> str | None:
    if v is None:
        return None
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return f"${v / div:.2f}{unit}"
    return f"${v:,.2f}"


def price(v: float | None) -> str | None:
    if v is None:
        return None
    if v >= 1000:
        return f"${v:,.0f}"
    if v >= 1:
        return f"${v:,.2f}"
    return f"${v:.6f}".rstrip("0")


def num(v: float | None, digits: int = 2) -> str | None:
    return None if v is None else f"{v:.{digits}f}"


def build(asset: Asset, facts: dict[str, Any]) -> dict[str, dict[str, str]]:
    t, s = facts.get("technical", {}), facts.get("sentiment", {})
    ctx = facts.get("context", {})
    sections: dict[str, dict[str, str | None]] = {
        "Price": {
            "Price": price(t.get("price")), "Change 1d": pct(t.get("change_1d")), "Change 7d": pct(t.get("change_7d")),
            "Change 30d": pct(t.get("change_30d")), "Change 90d": pct(t.get("change_90d")),
            "Change 1y": pct(t.get("change_1y")),
            "vs SMA200": pct(t.get("pct_vs_sma200")), "RSI 14": num(t.get("rsi14"), 0),
            "From 52w high": pct(t.get("drawdown_from_52w_high")),
            "Volatility 30d (ann.)": pct(t.get("volatility_30d"), 0, signed=False),
        },
        "Sentiment": {
            "Sentiment 7d (-1..+1)": num(s.get("level_7d")), "News sentiment 7d": num(s.get("level_news_7d")),
            "Social sentiment 7d": num(s.get("level_social_7d")), "Sentiment 30d": num(s.get("level_30d")),
            "Momentum (7d vs prior)": num(s.get("momentum")),
            "Z-score vs 90d": num(s.get("z"), 1),
            "Bullish share 7d": pct(s.get("bull_ratio_7d"), 0, signed=False),
            "Relevant texts 7d": str(s["n_relevant_7d"]) if "n_relevant_7d" in s else None,
            "Mention rate vs baseline": f"x{s['attention_ratio']:.1f}" if s.get("attention_ratio") else None,
            "Top themes": ", ".join(s.get("top_tags_7d", [])) or None,
        },
    }
    if asset.asset_class == "stock":
        m = facts.get("stock_meta", {})
        sections["Fundamentals"] = {
            "Market cap": money(m.get("market_cap")), "P/E": num(m.get("pe"), 1),
            "Forward P/E": num(m.get("forward_pe"), 1), "Revenue growth": pct(m.get("revenue_growth"), 0),
            "Profit margin": pct(m.get("profit_margin"), 0, signed=False),
            "Analyst rating": f"{m['analyst_rec_mean']:.1f} ({m.get('analyst_rec', '')})" if m.get("analyst_rec_mean") else None,
            "Target upside": pct(m.get("target_upside"), 0),
            "Last EPS surprise": f"{m['last_eps_surprise_pct']:+.1f}%" if m.get("last_eps_surprise_pct") is not None else None,
            "Next earnings": m.get("next_earnings"),
        }
        sections["Positioning"] = {
            "Short interest (float)": pct(m.get("short_pct_float"), 1, signed=False),
            "Days to cover": num(m.get("short_ratio_days"), 1),
            "Put/call (volume)": num(m.get("put_call_volume")), "Put/call (OI)": num(m.get("put_call_oi")),
            "ATM implied vol": pct(m.get("atm_iv"), 0, signed=False),
            "Insider buys 90d": money(m.get("insider_buy_value_90d")),
            "Insider sells 90d": money(m.get("insider_sell_value_90d")),
            "Upgrades/downgrades 90d": f"{m['analyst_upgrades_90d']}/{m['analyst_downgrades_90d']}"
            if m.get("analyst_upgrades_90d") is not None else None,
        }
        sections["Market"] = {
            "CNN Fear & Greed": f"{ctx['fear_greed']:.0f} ({ctx.get('fear_greed_rating', '')})" if ctx.get("fear_greed") is not None else None,
            "VIX": num(ctx.get("vix"), 1), "US 10Y": f"{ctx['us10y']:.2f}%" if ctx.get("us10y") else None,
            "S&P 500 trend": ("above SMA200" if ctx["spy_above_sma200"] else "below SMA200")
            if ctx.get("spy_above_sma200") is not None else None,
        }
    else:
        m, d = facts.get("crypto_meta", {}), facts.get("derivatives", {})
        sections["Fundamentals"] = {
            "Market cap": money(m.get("market_cap")), "Rank": str(m["market_cap_rank"]) if m.get("market_cap_rank") else None,
            "FDV": money(m.get("fdv")), "FDV / MC": num(m.get("fdv_mc_ratio")),
            "24h volume": money(m.get("volume_24h")), "From ATH": f"{m['ath_change_pct']:+.0f}%" if m.get("ath_change_pct") is not None else None,
            "TVL": money(m.get("tvl")),
            "TVL 30d": f"{m['tvl_change_30d_pct']:+.1f}%" if m.get("tvl_change_30d_pct") is not None else None,
            "Dev commits 4w": str(m["dev_commits_4w"]) if m.get("dev_commits_4w") is not None else None,
        }
        sections["Positioning"] = {
            "Funding 7d avg (per 8h)": f"{d['funding_rate_7d_avg'] * 100:.4f}%" if d.get("funding_rate_7d_avg") is not None else None,
            "Open interest": money(d.get("open_interest_usd")),
            "OI change 7d": f"{d['oi_change_7d_pct']:+.1f}%" if d.get("oi_change_7d_pct") is not None else None,
            "Long/short accounts": num(d.get("long_short_ratio")),
        }
        sections["Market"] = {
            "Crypto Fear & Greed": f"{ctx['fear_greed']} ({ctx.get('fear_greed_rating', '')})" if ctx.get("fear_greed") is not None else None,
            "BTC dominance": f"{ctx['btc_dominance']:.1f}%" if ctx.get("btc_dominance") else None,
            "Stablecoin supply 30d": f"{ctx['stablecoin_supply_change_30d_pct']:+.1f}%"
            if ctx.get("stablecoin_supply_change_30d_pct") is not None else None,
        }
    return {name: {k: v for k, v in rows.items() if v is not None} for name, rows in sections.items()}
