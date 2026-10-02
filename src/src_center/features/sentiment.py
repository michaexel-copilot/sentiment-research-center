"""Sentiment features from scored texts: level, momentum, z-score vs own history, attention, narratives."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

import numpy as np
import pandas as pd

from ..timeutil import utcnow

KIND_WEIGHT = {"news": 1.0, "social": 0.8}
HALF_LIFE_DAYS = 3.0
MIN_TEXTS = 3


def prepare(df: pd.DataFrame, now: pd.Timestamp) -> pd.DataFrame:
    df = df[df["relevant"].fillna(False).astype(bool)].copy()
    if df.empty:
        return df
    df["published_at"] = pd.to_datetime(df["published_at"])
    df = df[df["published_at"] <= now]
    df["age_days"] = (now - df["published_at"]).dt.total_seconds() / 86400
    sarcasm = df["sarcasm"].fillna(False).astype(bool)
    df["w"] = (df["confidence"].fillna(0.5)
               * np.where(sarcasm, 0.5, 1.0)
               * (1 + np.log1p(df["engagement"].fillna(0).clip(lower=0)) / 3)
               * df["kind"].map(KIND_WEIGHT).fillna(0.8))
    return df[df["w"] > 0]


def _wmean(df: pd.DataFrame, decay: bool = False) -> float | None:
    if len(df) < MIN_TEXTS:
        return None
    w = df["w"] * (0.5 ** (df["age_days"] / HALF_LIFE_DAYS) if decay else 1.0)
    if w.sum() <= 0:
        return None
    return float((df["sentiment"] * w).sum() / w.sum())


def daily_series(df: pd.DataFrame) -> pd.DataFrame:
    """Per-day weighted mean sentiment and count (for charts and the z-score)."""
    if df.empty:
        return pd.DataFrame(columns=["sentiment", "count"])
    d = df.assign(day=df["published_at"].dt.normalize(), ws=df["sentiment"] * df["w"])
    g = d.groupby("day").agg(ws=("ws", "sum"), w=("w", "sum"), count=("sentiment", "size"))
    return pd.DataFrame({"sentiment": g["ws"] / g["w"], "count": g["count"]})


def _rolling_levels(df: pd.DataFrame, now: pd.Timestamp, days: int = 90) -> list[float]:
    """7-day weighted mean sentiment evaluated at each past day (the history the current level is compared to)."""
    levels = []
    for back in range(7, days, 1):
        end = now - pd.Timedelta(days=back)
        window = df[(df["published_at"] <= end) & (df["published_at"] > end - pd.Timedelta(days=7))]
        if len(window) >= MIN_TEXTS:
            levels.append(float((window["sentiment"] * window["w"]).sum() / window["w"].sum()))
    return levels


def compute(texts: pd.DataFrame, now: pd.Timestamp | None = None) -> dict[str, Any]:
    now = now or pd.Timestamp(utcnow())
    total = len(texts)
    scored = texts[texts["sentiment"].notna()] if total else texts
    df = prepare(scored, now) if len(scored) else scored
    out: dict[str, Any] = {
        "texts_total_90d": int(total),
        "texts_scored_90d": int(len(scored)),
        "relevance_rate": round(len(df) / len(scored), 3) if len(scored) else None,
    }
    if df.empty:
        out.update({"n_relevant_7d": 0, "n_relevant_30d": 0})
        return out
    last7 = df[df["age_days"] <= 7]
    prev = df[(df["age_days"] > 7) & (df["age_days"] <= 30)]
    last30 = df[df["age_days"] <= 30]
    out["n_relevant_7d"] = int(len(last7))
    out["n_relevant_30d"] = int(len(last30))
    out["level_7d"] = _wmean(last7, decay=True)
    out["level_news_7d"] = _wmean(last7[last7["kind"] == "news"], decay=True)
    out["level_social_7d"] = _wmean(last7[last7["kind"] == "social"], decay=True)
    out["level_30d"] = _wmean(last30)
    mean7, mean_prev = _wmean(last7), _wmean(prev)
    out["momentum"] = (mean7 - mean_prev) if mean7 is not None and mean_prev is not None else None
    if len(last30) >= MIN_TEXTS:
        baseline_per_day = len(last30) / 30
        out["attention_ratio"] = round((len(last7) / 7) / baseline_per_day, 3) if baseline_per_day else None
    # Attention trend versus the older (30-90d) history, when available
    older = df[(df["age_days"] > 30) & (df["age_days"] <= 90)]
    if len(older) >= 10:
        out["attention_ratio_90d"] = round((len(last7) / 7) / (len(older) / 60), 3)
    levels = _rolling_levels(df, now)
    out["history_points"] = len(levels)
    if len(levels) >= 20 and out["level_7d"] is not None and np.std(levels) > 1e-6:
        out["z"] = float((out["level_7d"] - np.mean(levels)) / np.std(levels))
    stances = last7["stance"].value_counts()
    directional = stances.get("bullish", 0) + stances.get("bearish", 0)
    if directional:
        out["bull_ratio_7d"] = round(stances.get("bullish", 0) / directional, 3)
    tags = Counter(t for raw in last7["tags"].dropna() for t in json.loads(raw))
    out["top_tags_7d"] = [t for t, _ in tags.most_common(5)]
    out["sarcasm_share_7d"] = round(float(last7["sarcasm"].fillna(False).astype(bool).mean()), 3) if len(last7) else None
    out["source_mix_7d"] = last7["source"].value_counts().to_dict()
    return {k: v for k, v in out.items() if v is not None}


def top_texts(texts: pd.DataFrame, n: int = 5, days: int = 7) -> dict[str, list[dict[str, Any]]]:
    """Most influential bullish and bearish texts of the last `days` days (for the report and the narrative)."""
    now = pd.Timestamp(utcnow())
    df = prepare(texts[texts["sentiment"].notna()], now) if len(texts) else texts
    if df.empty:
        return {"bullish": [], "bearish": []}
    df = df[df["age_days"] <= days].assign(impact=lambda d: d["sentiment"] * d["w"])
    cols = ["source", "kind", "title", "body", "url", "published_at", "sentiment", "confidence", "tags"]

    def rows(sub: pd.DataFrame) -> list[dict[str, Any]]:
        recs = sub[cols].to_dict("records")
        for r in recs:
            r["published_at"] = pd.Timestamp(r["published_at"]).isoformat()
            r["tags"] = json.loads(r["tags"]) if isinstance(r["tags"], str) else []
            r["title"] = r["title"] or (r["body"] or "")[:140]
        return recs

    return {"bullish": rows(df[df["impact"] > 0].nlargest(n, "impact")),
            "bearish": rows(df[df["impact"] < 0].nsmallest(n, "impact"))}
