import json

import numpy as np
import pandas as pd

from src_center.features import sentiment, technical


def _prices(n=300, drift=0.001):
    idx = pd.date_range(end="2026-09-26", periods=n, freq="D")
    close = 100 * np.exp(np.cumsum(np.full(n, drift)))
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99, "close": close,
                         "volume": np.linspace(1e6, 2e6, n)}, index=idx)


def test_technical_uptrend():
    t = technical.compute(_prices())
    assert t["above_sma200"] is True
    assert t["sma50_slope_20d"] > 0
    assert t["rsi14"] > 70  # monotonic rise
    assert abs(t["change_30d"] - (np.exp(0.03) - 1)) < 1e-6
    assert t["drawdown_from_52w_high"] > -0.02


def test_rsi_bounds():
    down = _prices(drift=-0.002)
    assert technical.rsi(down["close"]) < 30


def _texts(now, specs):
    rows = []
    for i, (age_days, sent, kind, relevant) in enumerate(specs):
        rows.append({"text_id": str(i), "asset_key": "stock:X", "source": "google_news" if kind == "news" else "reddit",
                     "kind": kind, "title": f"t{i}", "body": "", "url": f"u{i}",
                     "published_at": now - pd.Timedelta(days=age_days), "engagement": 0.0, "author_label": None,
                     "relevant": relevant, "sentiment": sent, "confidence": 1.0, "stance": "bullish" if sent > 0.15
                     else "bearish" if sent < -0.15 else "neutral", "horizon": "short", "tags": json.dumps(["earnings"]),
                     "sarcasm": False})
    return pd.DataFrame(rows)


def test_sentiment_level_momentum_and_relevance():
    now = pd.Timestamp("2026-09-28")
    specs = [(d, 0.6, "news", True) for d in (0.5, 1, 2, 3)] \
        + [(d, -0.2, "news", True) for d in (10, 15, 20, 25)] \
        + [(1, -1.0, "news", False)]  # irrelevant: must be ignored
    s = sentiment.compute(_texts(now, specs), now=now)
    assert s["n_relevant_7d"] == 4
    assert abs(s["level_7d"] - 0.6) < 1e-9
    assert abs(s["momentum"] - 0.8) < 1e-9
    assert s["relevance_rate"] == round(8 / 9, 3)
    assert s["top_tags_7d"] == ["earnings"]


def test_sentiment_needs_minimum_texts():
    now = pd.Timestamp("2026-09-28")
    s = sentiment.compute(_texts(now, [(1, 0.5, "news", True), (2, 0.5, "news", True)]), now=now)
    assert "level_7d" not in s


def test_zscore_with_history():
    now = pd.Timestamp("2026-09-28")
    rng = np.random.default_rng(0)
    specs = [(d + 0.1 * k, float(rng.normal(0, 0.2)), "news", True) for d in range(8, 90) for k in range(2)]
    specs += [(d, 0.8, "news", True) for d in (0.5, 1, 2, 3, 4)]
    s = sentiment.compute(_texts(now, specs), now=now)
    assert s["history_points"] >= 20
    assert s["z"] > 2
