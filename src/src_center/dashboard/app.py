"""Streamlit dashboard: screener, signal changes, asset drill-down, market context, LLM costs, ad-hoc analysis.

Start with `uv run src dashboard`.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

from src_center import config
from src_center.nlp import llm
from src_center.storage import db

st.set_page_config(page_title="Sentiment Research Center", layout="wide")

SIGNAL_COLORS = {"BUY": "#15803d", "HOLD": "#b45309", "SELL": "#b91c1c", "NO SIGNAL": "#6b7280"}


class DataUnavailable(Exception):
    pass


def data_path() -> Path:
    """The snapshot each pipeline run publishes. The live database is only a fallback before the first
    snapshot exists: on Windows it can't be opened at all while a pipeline run is writing to it."""
    snapshot = db.snapshot_path()
    return snapshot if snapshot.exists() else config.path("db")


@st.cache_data(ttl=300)
def _query(sql: str, params: tuple, path: str, version: float) -> pd.DataFrame:
    # `version` (the file's mtime) is part of the cache key, so a newly published snapshot is read at once.
    for _ in range(10):  # the snapshot is swapped atomically; retry if we hit that instant
        try:
            con = duckdb.connect(path, read_only=True)
            break
        except duckdb.IOException:
            time.sleep(0.3)
    else:
        raise DataUnavailable(path)
    try:
        return con.execute(sql, list(params)).df()
    except duckdb.CatalogException:
        return pd.DataFrame()
    finally:
        con.close()


def q(sql: str, params: tuple = ()) -> pd.DataFrame:
    path = data_path()
    if not path.exists():
        return pd.DataFrame()
    try:
        return _query(sql, params, str(path), path.stat().st_mtime)
    except DataUnavailable:
        if not st.session_state.get("_unavailable_shown"):
            st.session_state["_unavailable_shown"] = True
            st.info("No dashboard data yet: the first pipeline run is still writing. The dashboard reads a "
                    "snapshot that each run publishes when it finishes; reload the page after the run.")
        return pd.DataFrame()


def latest_signals() -> pd.DataFrame:
    df = q("""
        WITH ranked AS (
          SELECT s.*, row_number() OVER (PARTITION BY asset_key ORDER BY date DESC) AS rn FROM signals s)
        SELECT cur.asset_key, a.symbol, a.name, a.asset_class, a.sector, a.universes, cur.date, cur.signal,
               cur.composite, cur.confidence, cur.subscores, cur.flags, prev.signal AS prev_signal,
               prev.composite AS prev_composite, prev.date AS prev_date
        FROM ranked cur LEFT JOIN ranked prev ON prev.asset_key = cur.asset_key AND prev.rn = 2
        LEFT JOIN assets a ON a.key = cur.asset_key
        WHERE cur.rn = 1""")
    if df.empty:
        return df
    subs = pd.DataFrame([json.loads(s) for s in df["subscores"]])
    df = pd.concat([df.drop(columns=["subscores"]), subs], axis=1)
    df["changed"] = df["prev_signal"].notna() & (df["signal"] != df["prev_signal"])
    return df


def signal_badge(sig: str) -> str:
    return f"<span style='background:{SIGNAL_COLORS.get(sig, '#6b7280')};color:#fff;padding:2px 10px;" \
           f"border-radius:4px;font-weight:700'>{sig}</span>"


st.title("Sentiment Research Center")
if db.snapshot_path().exists():
    stamp = datetime.fromtimestamp(db.snapshot_path().stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    st.caption(f"Data as of the last completed run: {stamp}. Reload (F5) after a run to see new results.")
sig_df = latest_signals()

tab_screen, tab_changes, tab_asset, tab_market, tab_costs, tab_run = st.tabs(
    ["Screener", "Signal changes", "Asset", "Market", "LLM costs", "Analyze ticker"])

with tab_screen:
    if sig_df.empty:
        st.info("No signals yet. Run `uv run src run -a NVDA` or use the 'Analyze ticker' tab.")
    else:
        c1, c2, c3, c4 = st.columns(4)
        classes = c1.multiselect("Class", ["stock", "crypto"], default=["stock", "crypto"])
        signals = c2.multiselect("Signal", list(SIGNAL_COLORS), default=["BUY", "HOLD", "SELL"])
        universes = c3.multiselect("Universe", ["sp500", "nasdaq100", "crypto100"])
        min_conf = c4.slider("Min confidence", 0.0, 1.0, 0.0, 0.05)
        view = sig_df[sig_df["asset_class"].isin(classes) & sig_df["signal"].isin(signals)
                      & (sig_df["confidence"] >= min_conf)]
        if universes:
            view = view[view["universes"].fillna("").apply(lambda u: any(x in u.split(",") for x in universes))]
        counts = view["signal"].value_counts()
        m = st.columns(4)
        for col, s in zip(m, ["BUY", "HOLD", "SELL", "NO SIGNAL"]):
            col.metric(s, int(counts.get(s, 0)))
        cols = ["symbol", "name", "asset_class", "signal", "composite", "confidence", "sentiment_level",
                "sentiment_momentum", "attention", "positioning", "trend", "fundamentals", "macro",
                "prev_signal", "date"]
        st.dataframe(
            view.sort_values("composite", ascending=False)[[c for c in cols if c in view.columns]],
            hide_index=True, use_container_width=True,
            column_config={
                "composite": st.column_config.ProgressColumn("composite", min_value=0, max_value=100, format="%.1f"),
                "confidence": st.column_config.NumberColumn(format="%.2f"),
            })

with tab_changes:
    if sig_df.empty:
        st.info("No signals yet.")
    else:
        ch = sig_df[sig_df["changed"]].sort_values("date", ascending=False)
        st.caption("Assets whose signal differs from the previous run")
        if ch.empty:
            st.write("No signal changes in the latest runs.")
        for _, r in ch.iterrows():
            st.markdown(f"**{r['symbol']}** {r['name']} &nbsp; {signal_badge(r['prev_signal'])} → "
                        f"{signal_badge(r['signal'])} &nbsp; composite {r['prev_composite']:.1f} → {r['composite']:.1f}",
                        unsafe_allow_html=True)

with tab_asset:
    if sig_df.empty:
        st.info("No signals yet.")
    else:
        options = sig_df.sort_values("symbol")["asset_key"].tolist()
        key = st.selectbox("Asset", options, format_func=lambda k: f"{k.split(':')[1]} ({k.split(':')[0]})")
        row = sig_df[sig_df["asset_key"] == key].iloc[0]
        d = str(pd.Timestamp(row["date"]).date())
        folder = config.path("reports") / d / key.replace(":", "_")
        c1, c2 = st.columns([3, 1])
        with c2:
            st.markdown(signal_badge(row["signal"]), unsafe_allow_html=True)
            st.metric("Composite", f"{row['composite']:.1f}" if pd.notna(row["composite"]) else "–")
            st.metric("Confidence", f"{row['confidence']:.0%}")
            pdf = folder / "onepager.pdf"
            if pdf.exists():
                st.download_button("Download one-pager PDF", pdf.read_bytes(), file_name=f"{row['symbol']}_{d}.pdf")
            md = folder / "report.md"
            if md.exists():
                st.download_button("Download report (Markdown)", md.read_bytes(), file_name=f"{row['symbol']}_{d}.md")
            hist = q("SELECT date, signal, composite FROM signals WHERE asset_key = ? ORDER BY date", (key,))
            if len(hist) > 1:
                fig = go.Figure(go.Scatter(x=hist["date"], y=hist["composite"], mode="lines+markers",
                                           marker=dict(color=[SIGNAL_COLORS[s] for s in hist["signal"]])))
                fig.add_hrect(y0=65, y1=100, fillcolor="#15803d", opacity=0.07, line_width=0)
                fig.add_hrect(y0=0, y1=35, fillcolor="#b91c1c", opacity=0.07, line_width=0)
                fig.update_layout(height=260, margin=dict(l=0, r=0, t=20, b=0), yaxis_range=[0, 100],
                                  title="Composite history")
                st.plotly_chart(fig, use_container_width=True)
        with c1:
            html = folder / "onepager.html"
            if html.exists():
                components.html(html.read_text(encoding="utf-8"), height=1150, scrolling=True)
            else:
                st.warning(f"One-pager not found in {folder}")
        texts = q("""SELECT t.published_at, t.source, s.sentiment, s.confidence, s.stance, s.relevant, t.title, t.url
                     FROM texts t JOIN text_scores s USING (text_id, asset_key)
                     WHERE t.asset_key = ? ORDER BY t.published_at DESC LIMIT 200""", (key,))
        with st.expander(f"Scored texts ({len(texts)})"):
            st.dataframe(texts, hide_index=True, use_container_width=True,
                         column_config={"url": st.column_config.LinkColumn()})

with tab_market:
    for key, label in (("market:stock", "Stocks"), ("market:crypto", "Crypto")):
        ctx = q("SELECT date, data FROM snapshots WHERE asset_key = ? AND kind = 'context' ORDER BY date DESC LIMIT 1",
                (key,))
        st.subheader(label)
        if ctx.empty:
            st.write("No context yet.")
            continue
        data = {k: v for k, v in json.loads(ctx.iloc[0]["data"]).items() if k != "fetched_at"}
        cols = st.columns(min(len(data), 5) or 1)
        for i, (k, v) in enumerate(data.items()):
            cols[i % len(cols)].metric(k.replace("_", " "), f"{v:,.2f}" if isinstance(v, float) else str(v))

with tab_costs:
    usage = q("""SELECT CAST(ts AS DATE) AS day, stage, model, sum(input_tokens) AS input_tokens,
                 sum(output_tokens) AS output_tokens, sum(cost_usd) AS cost_usd
                 FROM llm_usage GROUP BY ALL ORDER BY day""")
    if usage.empty:
        st.write("No LLM usage logged yet.")
    else:
        st.metric("Total cost (USD)", f"${usage['cost_usd'].sum():.2f}")
        fig = go.Figure([go.Bar(x=g["day"], y=g["cost_usd"], name=stage) for stage, g in usage.groupby("stage")])
        fig.update_layout(barmode="stack", height=300, margin=dict(l=0, r=0, t=10, b=0), yaxis_title="USD")
        st.plotly_chart(fig, use_container_width=True)
        st.dataframe(usage.sort_values("day", ascending=False), hide_index=True, use_container_width=True)

with tab_run:
    st.write("Run the full pipeline for one or more tickers (stocks or crypto).")
    tickers = st.text_input("Tickers (space separated)", placeholder="NVDA BTC")
    c1, c2 = st.columns(2)
    no_llm = c1.checkbox("Offline mode (no LLM)", value=not llm.available())
    forced = c2.selectbox("Class", ["auto", "stock", "crypto"])
    if st.button("Analyze", type="primary", disabled=not tickers.strip()):
        cmd = [sys.executable, "-m", "src_center.cli", "run"]
        for t in tickers.split():
            cmd += ["-a", t]
        if no_llm:
            cmd.append("--no-llm")
        if forced != "auto":
            cmd += ["--class", forced]
        with st.spinner("Running pipeline… (typically 20-90 s per ticker)"):
            proc = subprocess.run(cmd, capture_output=True, text=True, cwd=Path(config.PROJECT_ROOT))
        st.code((proc.stdout or "")[-3000:] + (proc.stderr or "")[-3000:])
        st.cache_data.clear()
