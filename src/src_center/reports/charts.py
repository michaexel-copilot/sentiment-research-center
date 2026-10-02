"""Static SVG charts for the one-pager and report (matplotlib, no JS so the PDF renders offline)."""

from __future__ import annotations

import io

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

INK, MUTED, GRID = "#1f2937", "#6b7280", "#e5e7eb"
POS, NEG, LINE = "#15803d", "#b91c1c", "#1d4ed8"


def _svg(fig) -> str:
    buf = io.StringIO()
    fig.savefig(buf, format="svg", bbox_inches="tight", transparent=True)
    plt.close(fig)
    svg = buf.getvalue()
    return svg[svg.find("<svg"):]


def price_sentiment(prices: pd.DataFrame, daily: pd.DataFrame, days: int = 90) -> str:
    """Price line (left axis) with daily sentiment bars (right axis) for the last `days` days."""
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 8, "svg.fonttype": "none"})
    fig, ax = plt.subplots(figsize=(7.2, 2.3))
    if not prices.empty:
        p = prices["close"][prices.index >= prices.index[-1] - pd.Timedelta(days=days)]
        ax.plot(p.index, p.values, color=LINE, linewidth=1.6, label="Price")
        if len(prices) >= 50:
            sma = prices["close"].rolling(50).mean()
            sma = sma[sma.index >= p.index[0]]
            ax.plot(sma.index, sma.values, color=MUTED, linewidth=0.9, linestyle="--", label="SMA50")
    ax.set_ylabel("Price", color=MUTED)
    ax.tick_params(colors=MUTED)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_color(GRID)
    ax.spines["bottom"].set_color(GRID)
    ax.grid(axis="y", color=GRID, linewidth=0.6)
    if daily is not None and not daily.empty:
        d = daily[daily.index >= pd.Timestamp.now().normalize() - pd.Timedelta(days=days)]
        ax2 = ax.twinx()
        colors = [POS if v >= 0 else NEG for v in d["sentiment"]]
        ax2.bar(d.index, d["sentiment"], width=0.8, color=colors, alpha=0.35, label="Daily sentiment")
        ax2.set_ylim(-1, 1)
        ax2.axhline(0, color=GRID, linewidth=0.8)
        ax2.set_ylabel("Sentiment", color=MUTED)
        ax2.tick_params(colors=MUTED)
        for side in ("top", "left"):
            ax2.spines[side].set_visible(False)
        ax2.spines["right"].set_color(GRID)
        ax2.spines["bottom"].set_color(GRID)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=7))
    return _svg(fig)


def sentiment_volume(daily: pd.DataFrame, days: int = 90) -> str:
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 8, "svg.fonttype": "none"})
    fig, ax = plt.subplots(figsize=(7.2, 1.6))
    if daily is not None and not daily.empty:
        d = daily[daily.index >= pd.Timestamp.now().normalize() - pd.Timedelta(days=days)]
        ax.bar(d.index, d["count"], width=0.8, color=LINE, alpha=0.6)
    ax.set_ylabel("Relevant texts/day", color=MUTED)
    ax.tick_params(colors=MUTED)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.grid(axis="y", color=GRID, linewidth=0.6)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    return _svg(fig)
