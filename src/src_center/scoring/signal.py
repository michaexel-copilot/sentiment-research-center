"""Combine sub-scores into a composite and a BUY / HOLD / SELL / NO SIGNAL decision with gates and flags."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

import numpy as np

from .. import config
from . import subscores as ss


@dataclass
class SignalResult:
    signal: str
    composite: float | None
    confidence: float
    subscores: dict[str, float | None]
    reasons: dict[str, list[str]]
    drivers: list[str]
    flags: list[str] = field(default_factory=list)
    rule: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_subscores(asset_class: str, symbol: str, facts: dict[str, Any]) -> tuple[dict, dict]:
    s, t = facts.get("sentiment", {}), facts.get("technical", {})
    ctx = facts.get("context", {})
    results: dict[str, ss.Result] = {
        "sentiment_level": ss.sentiment_level(s),
        "sentiment_momentum": ss.sentiment_momentum(s),
        "attention": ss.attention(s),
        "trend": ss.trend(t),
    }
    if asset_class == "stock":
        meta = facts.get("stock_meta", {})
        results["positioning"] = ss.positioning_stock(meta, s)
        results["fundamentals"] = ss.fundamentals_stock(meta)
        results["macro"] = ss.macro_stock(ctx)
    else:
        results["positioning"] = ss.positioning_crypto(facts.get("derivatives", {}), t.get("change_7d"))
        results["fundamentals"] = ss.fundamentals_crypto(facts.get("crypto_meta", {}))
        results["macro"] = ss.macro_crypto(ctx, is_btc=symbol == "BTC")
    scores = {k: (round(v[0], 1) if v[0] is not None else None) for k, v in results.items()}
    reasons = {k: v[1] for k, v in results.items()}
    return scores, reasons


def decide(asset_class: str, symbol: str, facts: dict[str, Any], today: date | None = None) -> SignalResult:
    wcfg = config.weights()
    th, gates = wcfg["thresholds"], wcfg["gates"]
    weights = wcfg["weights"][asset_class]
    scores, reasons = compute_subscores(asset_class, symbol, facts)
    s, t = facts.get("sentiment", {}), facts.get("technical", {})
    flags: list[str] = []

    available = {k: v for k, v in scores.items() if v is not None}
    total_w = sum(weights[k] for k in available)
    composite = sum(v * weights[k] for k, v in available.items()) / total_w if total_w else None
    coverage = total_w / sum(weights.values())
    agreement = 1 - min(1.0, float(np.std(list(available.values()))) / 30) if len(available) > 1 else 0.5
    confidence = round(max(0.0, min(1.0, 0.6 * coverage + 0.4 * agreement)), 2)

    # Top drivers: largest weighted deviations from neutral.
    contrib = sorted(((k, (v - 50) * weights[k]) for k, v in available.items()), key=lambda kv: -abs(kv[1]))
    drivers = [f"{k.replace('_', ' ').capitalize()} {scores[k]:.0f} ({'supportive' if c > 0 else 'negative'}): "
               f"{'; '.join(reasons[k][:2])}" for k, c in contrib[:3] if abs(c) > 0]

    # --- data gates ---
    min_texts = gates["min_relevant_texts_7d"][asset_class]
    stale_days = config.settings()["run"]["stale_price_days"][asset_class]
    price_date = t.get("price_date")
    today = today or date.today()
    if s.get("n_relevant_7d", 0) < min_texts:
        flags.append(f"only {s.get('n_relevant_7d', 0)} relevant texts in 7d (min {min_texts})")
    if not price_date or (today - date.fromisoformat(price_date)).days > stale_days:
        flags.append("price data missing or stale")
    if len(available) < gates["min_subscores"] or flags:
        return SignalResult("NO SIGNAL", round(composite, 1) if composite is not None else None, confidence, scores, reasons, drivers,
                            flags, rule="insufficient data")

    # --- warnings (do not change the signal) ---
    next_earnings = facts.get("stock_meta", {}).get("next_earnings")
    if next_earnings:
        days = (date.fromisoformat(next_earnings) - today).days
        if 0 <= days <= th["earnings_warning_days"]:
            flags.append(f"earnings in {days} day(s) ({next_earnings}) - event risk")

    # --- decision rules ---
    positioning = scores.get("positioning")
    momentum = scores.get("sentiment_momentum")
    euphoric = (s.get("z") is not None and s["z"] > th["euphoria_z"]) or \
               (s.get("z") is None and s.get("level_7d", 0) > th["euphoria_level"])
    crowded = positioning is not None and positioning < th["crowded_positioning"]
    near_high = t.get("drawdown_from_52w_high") is not None and \
        t["drawdown_from_52w_high"] > -th["divergence_near_high_pct"] / 100

    if (near_high and momentum is not None and momentum <= th["divergence_momentum_max"]
            and positioning is not None and positioning <= th["divergence_positioning_max"]):
        flags.append("bearish divergence: price near highs, sentiment fading, positioning crowded")
        return SignalResult("SELL", round(composite, 1), confidence, scores, reasons, drivers, flags,
                            rule="bearish divergence")
    if composite <= th["sell"]:
        return SignalResult("SELL", round(composite, 1), confidence, scores, reasons, drivers, flags,
                            rule=f"composite <= {th['sell']}")
    if composite >= th["buy"]:
        trend_ok = t.get("above_sma200", True) or (momentum is not None and momentum >= th["momentum_override_buy"])
        if euphoric and crowded:
            flags.append("euphoria + crowded positioning: contrarian cap at HOLD")
            return SignalResult("HOLD", round(composite, 1), confidence, scores, reasons, drivers, flags,
                                rule="euphoria cap")
        if not trend_ok:
            flags.append("below SMA200 without strong sentiment momentum: waiting for trend confirmation")
            return SignalResult("HOLD", round(composite, 1), confidence, scores, reasons, drivers, flags,
                                rule="trend filter")
        return SignalResult("BUY", round(composite, 1), confidence, scores, reasons, drivers, flags,
                            rule=f"composite >= {th['buy']}")
    return SignalResult("HOLD", round(composite, 1), confidence, scores, reasons, drivers, flags,
                        rule="between thresholds")
