"""Signal validation: forward returns per signal vs benchmark, hit rates, sub-score information coefficients."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from .storage import db

HORIZONS = (7, 30, 90)
BENCHMARK = {"stock": "stock:SPY", "crypto": "crypto:BTC"}


def _forward_return(px: pd.Series, start: pd.Timestamp, days: int) -> float | None:
    after = px[px.index >= start]
    end = px[px.index >= start + pd.Timedelta(days=days)]
    if after.empty or end.empty:
        return None
    return float(end.iloc[0] / after.iloc[0] - 1)


def signal_returns() -> pd.DataFrame:
    sig = db.query_df("SELECT asset_key, date, signal, composite, subscores FROM signals")
    if sig.empty:
        return sig
    sig["date"] = pd.to_datetime(sig["date"])
    closes: dict[str, pd.Series] = {}

    def close(key: str) -> pd.Series:
        if key not in closes:
            p = db.prices(key)
            closes[key] = p["close"] if not p.empty else pd.Series(dtype=float)
        return closes[key]

    rows = []
    for _, r in sig.iterrows():
        cls = r["asset_key"].split(":")[0]
        rec = {"asset_key": r["asset_key"], "date": r["date"], "signal": r["signal"], "composite": r["composite"],
               "asset_class": cls, **{f"sub_{k}": v for k, v in json.loads(r["subscores"]).items()}}
        for h in HORIZONS:
            ret = _forward_return(close(r["asset_key"]), r["date"], h)
            bench = _forward_return(close(BENCHMARK[cls]), r["date"], h)
            rec[f"ret_{h}d"] = ret
            rec[f"excess_{h}d"] = ret - bench if ret is not None and bench is not None else None
        rows.append(rec)
    return pd.DataFrame(rows)


def summary() -> dict[str, pd.DataFrame]:
    df = signal_returns()
    if df.empty:
        return {}
    out = {}
    agg = {}
    for h in HORIZONS:
        col = f"excess_{h}d"
        g = df.dropna(subset=[col]).groupby(["asset_class", "signal"])[col]
        agg[f"n_{h}d"] = g.size()
        agg[f"avg_excess_{h}d"] = g.mean()
        # Hit = BUY beats benchmark / SELL underperforms it / HOLD: n/a
        hit = df.dropna(subset=[col]).assign(hit=lambda d: np.where(
            d["signal"] == "BUY", d[col] > 0, np.where(d["signal"] == "SELL", d[col] < 0, np.nan)))
        agg[f"hit_rate_{h}d"] = hit.groupby(["asset_class", "signal"])["hit"].mean()
    out["by_signal"] = pd.DataFrame(agg).round(4)
    ic = {}
    for col in [c for c in df.columns if c.startswith("sub_")] + ["composite"]:
        for h in HORIZONS:
            sub = df[[col, f"excess_{h}d"]].dropna()
            if len(sub) >= 20:
                ic[(col, f"{h}d")] = sub[col].corr(sub[f"excess_{h}d"], method="spearman")
    if ic:
        out["information_coefficient"] = pd.Series(ic).unstack().round(3)
    return out
