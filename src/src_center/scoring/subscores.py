"""Map raw facts to 0-100 sub-scores (50 = neutral). Each function returns (score or None, reasons)."""

from __future__ import annotations

import math
from typing import Any

Result = tuple[float | None, list[str]]


def _clip(x: float) -> float:
    return max(0.0, min(100.0, x))


def _mean(parts: list[float]) -> float | None:
    return sum(parts) / len(parts) if parts else None


def _interp(x: float, x0: float, y0: float, x1: float, y1: float) -> float:
    """Linear interpolation clamped to [y0, y1] at the ends."""
    if x0 == x1:
        return y0
    t = max(0.0, min(1.0, (x - x0) / (x1 - x0)))
    return y0 + t * (y1 - y0)


def sentiment_level(s: dict[str, Any]) -> Result:
    level = s.get("level_7d")
    if level is None:
        return None, ["too few relevant texts in the last 7 days"]
    score = 50 + 50 * math.tanh(2.0 * level)
    reasons = [f"7d sentiment {level:+.2f}"]
    if s.get("z") is not None:
        z_score = _clip(50 + 15 * s["z"])
        score = 0.5 * score + 0.5 * z_score
        reasons.append(f"z-score {s['z']:+.1f} vs own 90d history")
    return _clip(score), reasons


def sentiment_momentum(s: dict[str, Any]) -> Result:
    m = s.get("momentum")
    if m is None:
        return None, ["not enough history for momentum"]
    return _clip(50 + 50 * math.tanh(3.0 * m)), [f"7d vs prior 23d sentiment change {m:+.2f}"]


def attention(s: dict[str, Any]) -> Result:
    # Only the 7d-vs-older-history ratio is trusted: on a first run the sources return mostly recent items,
    # so a 7d-vs-30d ratio would look like an attention spike.
    ratio = s.get("attention_ratio_90d")
    level = s.get("level_7d")
    if ratio is None or level is None:
        return None, ["no attention baseline yet (needs 30-90d history)"]
    # Attention amplifies the prevailing sentiment direction; it is not a signal on its own.
    boost = max(-1.0, min(1.0, math.log2(max(ratio, 0.01)) / 2))
    direction = math.tanh(3.0 * level)
    return _clip(50 + 30 * boost * direction), [f"mention rate x{ratio:.1f} vs baseline"]


def positioning_stock(meta: dict[str, Any], s: dict[str, Any]) -> Result:
    parts, reasons = [], []
    pc = meta.get("put_call_volume") or meta.get("put_call_oi")
    if pc is not None:
        # Contrarian: heavy put buying = fear (bullish), very low put/call = complacency (bearish).
        parts.append(_interp(pc, 0.4, 30, 1.3, 70))
        reasons.append(f"put/call {pc:.2f}")
    si = meta.get("short_pct_float")
    if si is not None:
        momentum = s.get("momentum") or 0
        if si > 0.15 and momentum > 0.1:
            parts.append(65)
            reasons.append(f"short interest {si:.0%} + improving sentiment (squeeze setup)")
        else:
            parts.append(_interp(si, 0.03, 55, 0.25, 30))
            reasons.append(f"short interest {si:.1%} of float")
    buys, sells = meta.get("insider_buy_value_90d"), meta.get("insider_sell_value_90d")
    if buys is not None and sells is not None and (buys or sells):
        if buys > 0 and meta.get("insider_buy_count_90d", 0) >= 2:
            parts.append(75)
            reasons.append("insider buying in last 90d")
        elif sells > 0:
            mcap = meta.get("market_cap") or 0
            heavy = mcap and sells / mcap > 0.001
            parts.append(38 if heavy else 47)
            reasons.append("heavy insider selling" if heavy else "routine insider selling")
    return _mean(parts), reasons


def positioning_crypto(d: dict[str, Any], change_7d: float | None) -> Result:
    parts, reasons = [], []
    f = d.get("funding_rate_7d_avg")
    if f is not None:
        # Funding per 8h: ~0.01% neutral; >0.03% crowded longs; negative = shorts pay = contrarian bullish.
        parts.append(_interp(f, -0.0002, 72, 0.0006, 20))
        reasons.append(f"funding {f * 100:.3f}%/8h (7d avg)")
    ls = d.get("long_short_ratio")
    if ls is not None:
        parts.append(_interp(ls, 0.8, 68, 3.0, 30))
        reasons.append(f"long/short accounts {ls:.2f}")
    oi = d.get("oi_change_7d_pct")
    if oi is not None and change_7d is not None:
        if oi > 15 and change_7d < 0:
            parts.append(40)
            reasons.append(f"OI +{oi:.0f}% while price falls (shorts pressing)")
        elif oi > 25 and change_7d > 0:
            parts.append(42)
            reasons.append(f"OI +{oi:.0f}% leverage build-up into rally")
        elif oi < -15 and change_7d < 0:
            parts.append(58)
            reasons.append(f"OI {oi:.0f}% deleveraging flush")
        else:
            parts.append(52 if change_7d > 0 else 48)
    return _mean(parts), reasons


def trend(t: dict[str, Any]) -> Result:
    if "price" not in t:
        return None, ["no price data"]
    score, reasons = 50.0, []
    if "above_sma200" in t:
        score += 18 if t["above_sma200"] else -18
        reasons.append(f"{'above' if t['above_sma200'] else 'below'} SMA200 ({t['pct_vs_sma200']:+.1%})")
    if "sma50_slope_20d" in t:
        score += 12 * math.tanh(t["sma50_slope_20d"] * 15)
        reasons.append(f"SMA50 slope {t['sma50_slope_20d']:+.1%}/20d")
    if t.get("change_90d") is not None:
        score += 10 * math.tanh(t["change_90d"] * 3)
    r = t.get("rsi14")
    if r is not None:
        if r > 75:
            score -= 8
            reasons.append(f"overbought RSI {r:.0f}")
        elif r < 25:
            score += 6
            reasons.append(f"oversold RSI {r:.0f}")
    dd = t.get("drawdown_from_52w_high")
    if dd is not None and dd < -0.4:
        score -= 6
        reasons.append(f"{dd:.0%} below 52w high")
    return _clip(score), reasons


def fundamentals_stock(m: dict[str, Any]) -> Result:
    parts, reasons = [], []
    g = m.get("revenue_growth")
    if g is not None:
        parts.append(_interp(g, -0.1, 25, 0.3, 80))
        reasons.append(f"revenue growth {g:+.0%}")
    rec = m.get("analyst_rec_mean")
    if rec is not None:
        parts.append(_interp(rec, 1.5, 75, 3.5, 25))
        reasons.append(f"analyst rating {rec:.1f} ({m.get('analyst_rec', '')})")
    up = m.get("target_upside")
    if up is not None:
        parts.append(_interp(up, -0.1, 30, 0.3, 75))
        reasons.append(f"target upside {up:+.0%}")
    s = m.get("last_eps_surprise_pct")
    if s is not None:
        parts.append(_interp(s, -10, 30, 10, 70))
        reasons.append(f"last EPS surprise {s:+.1f}%")
    ups, downs = m.get("analyst_upgrades_90d"), m.get("analyst_downgrades_90d")
    if ups is not None and downs is not None and ups + downs:
        parts.append(_interp(ups - downs, -3, 30, 3, 70))
        reasons.append(f"{ups} upgrades / {downs} downgrades (90d)")
    fpe = m.get("forward_pe")
    if fpe is not None and fpe > 0 and g is not None:
        # Growth-adjusted valuation: expensive only matters if growth doesn't justify it.
        peg_like = fpe / max(g * 100, 1)
        parts.append(_interp(peg_like, 1, 65, 4, 35))
    return _mean(parts), reasons


def fundamentals_crypto(m: dict[str, Any]) -> Result:
    parts, reasons = [], []
    ratio = m.get("fdv_mc_ratio")
    if ratio is not None:
        parts.append(_interp(ratio, 1.0, 62, 3.0, 25))
        reasons.append(f"FDV/MC {ratio:.2f}" + (" (unlock overhang)" if ratio > 1.5 else ""))
    tvl = m.get("tvl_change_30d_pct", m.get("tvl_change_7d_pct"))
    if tvl is not None:
        parts.append(_interp(tvl, -25, 30, 25, 70))
        reasons.append(f"TVL change {tvl:+.0f}%")
    commits = m.get("dev_commits_4w")
    if commits is not None:
        parts.append(_interp(commits, 0, 40, 100, 62))
        reasons.append(f"{commits} dev commits (4w)")
    return _mean(parts), reasons


def macro_stock(c: dict[str, Any]) -> Result:
    parts, reasons = [], []
    fg = c.get("fear_greed")
    if fg is not None:
        parts.append(_interp(fg, 15, 65, 85, 35))  # contrarian
        reasons.append(f"market Fear&Greed {fg:.0f} ({c.get('fear_greed_rating', '')})")
    vix = c.get("vix")
    if vix is not None:
        parts.append(_interp(vix, 14, 62, 32, 32))
        reasons.append(f"VIX {vix:.1f}")
    if c.get("spy_above_sma200") is not None:
        parts.append(62 if c["spy_above_sma200"] else 38)
        reasons.append("S&P 500 in uptrend" if c["spy_above_sma200"] else "S&P 500 below SMA200")
    rates = c.get("us10y_change_30d")
    if rates is not None:
        parts.append(_interp(rates, -0.3, 60, 0.4, 40))
    return _mean(parts), reasons


def macro_crypto(c: dict[str, Any], is_btc: bool) -> Result:
    parts, reasons = [], []
    fg = c.get("fear_greed")
    if fg is not None:
        parts.append(_interp(fg, 15, 68, 85, 32))
        reasons.append(f"crypto Fear&Greed {fg} ({c.get('fear_greed_rating', '')})")
    st = c.get("stablecoin_supply_change_30d_pct")
    if st is not None:
        parts.append(_interp(st, -3, 35, 5, 68))
        reasons.append(f"stablecoin supply {st:+.1f}% (30d)")
    dom = c.get("btc_dominance_change_30d")
    if dom is not None and not is_btc:
        parts.append(_interp(dom, 3, 38, -3, 62))  # rising BTC dominance hurts alts
        reasons.append(f"BTC dominance {dom:+.1f}pp (30d)")
    return _mean(parts), reasons
