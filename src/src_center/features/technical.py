"""Price/volume features from daily OHLCV."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def rsi(close: pd.Series, period: int = 14) -> float | None:
    if len(close) <= period:
        return None
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    last_loss = loss.iloc[-1]
    if last_loss == 0:
        return 100.0
    return float(100 - 100 / (1 + gain.iloc[-1] / last_loss))


def _ret(close: pd.Series, days: int) -> float | None:
    """Return over `days` calendar days (robust to weekends / missing bars)."""
    if close.empty:
        return None
    target = close.index[-1] - pd.Timedelta(days=days)
    past = close[close.index <= target]
    if past.empty:
        return None
    return float(close.iloc[-1] / past.iloc[-1] - 1)


def compute(prices: pd.DataFrame) -> dict[str, Any]:
    if prices.empty:
        return {}
    close = prices["close"].astype(float)
    out: dict[str, Any] = {
        "price": float(close.iloc[-1]),
        "price_date": close.index[-1].date().isoformat(),
        "change_1d": float(close.iloc[-1] / close.iloc[-2] - 1) if len(close) > 1 else None,
        "change_7d": _ret(close, 7),
        "change_30d": _ret(close, 30),
        "change_90d": _ret(close, 90),
        "change_1y": _ret(close, 365),
        "rsi14": rsi(close),
    }
    if len(close) >= 50:
        sma50 = close.rolling(50).mean()
        out["sma50"] = float(sma50.iloc[-1])
        if len(sma50.dropna()) > 20:
            out["sma50_slope_20d"] = float(sma50.iloc[-1] / sma50.iloc[-21] - 1)
    if len(close) >= 200:
        out["sma200"] = float(close.tail(200).mean())
        out["above_sma200"] = bool(close.iloc[-1] > out["sma200"])
        out["pct_vs_sma200"] = float(close.iloc[-1] / out["sma200"] - 1)
    year = close[close.index >= close.index[-1] - pd.Timedelta(days=365)]
    high = float(prices.loc[year.index, "high"].max()) if "high" in prices else float(year.max())
    out["high_52w"] = high
    out["drawdown_from_52w_high"] = float(close.iloc[-1] / high - 1) if high else None
    rets = np.log(close).diff().dropna()
    if len(rets) >= 30:
        periods = 365 if (close.index.dayofweek >= 5).any() else 252
        out["volatility_30d"] = float(rets.tail(30).std() * np.sqrt(periods))
    vol = prices["volume"].astype(float)
    if len(vol) >= 90 and vol.tail(90).std() > 0:
        out["volume_z"] = float((vol.tail(5).mean() - vol.tail(90).mean()) / vol.tail(90).std())
    return {k: v for k, v in out.items() if v is not None and not (isinstance(v, float) and np.isnan(v))}
