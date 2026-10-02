"""Pipeline: context → ingest (parallel, isolated per asset) → LLM scoring → features/signal → narrative → render."""

from __future__ import annotations

import json
import logging
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd

from . import config, http
from .features import sentiment as sent_features
from .features import technical
from .ingest import context, crypto_meta, prices, stock_meta, texts
from .models import Asset
from .narrative import facts as fact_sheet
from .narrative import writer
from .nlp import llm, scorer
from .reports import charts, render
from .scoring import signal as signal_engine
from .storage import db
from .timeutil import utcnow

log = logging.getLogger(__name__)


@dataclass
class RunReport:
    run_id: str
    scope: str
    assets: int = 0
    signals: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    scored_texts: int = 0
    outputs: dict[str, dict[str, str]] = field(default_factory=dict)
    cost_usd: float = 0.0
    seconds: float = 0.0


def ingest_asset(asset: Asset, pool: texts.Pool, single_asset: bool) -> dict[str, Any]:
    """All network ingestion for one asset. Price failure is fatal for the asset; the rest degrades."""
    result: dict[str, Any] = {"prices": prices.update(asset)}
    try:
        meta = stock_meta.update(asset) if asset.asset_class == "stock" else crypto_meta.update(asset)
        result["meta_fields"] = len(meta)
    except Exception as exc:  # noqa: BLE001
        log.warning("meta ingestion failed for %s: %s", asset.symbol, exc)
    sub = None
    if asset.asset_class == "crypto":
        sub = (db.load_snapshot(asset.key, "crypto_meta") or {}).get("subreddit")
    result["texts"] = texts.collect(asset, pool, single_asset=single_asset, crypto_subreddit=sub)
    return result


def build_facts(asset: Asset) -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    px = db.prices(asset.key)
    tx = db.texts_with_scores(asset.key, days=90)
    facts: dict[str, Any] = {
        "technical": technical.compute(px),
        "sentiment": sent_features.compute(tx),
        "context": context.load(asset.asset_class),
    }
    if asset.asset_class == "stock":
        facts["stock_meta"] = db.load_snapshot(asset.key, "stock_meta") or {}
    else:
        facts["crypto_meta"] = db.load_snapshot(asset.key, "crypto_meta") or {}
        facts["derivatives"] = db.load_snapshot(asset.key, "derivatives") or {}
    now = pd.Timestamp(utcnow())
    prepared = sent_features.prepare(tx[tx["sentiment"].notna()], now) if len(tx) else tx
    daily = sent_features.daily_series(prepared) if len(prepared) else pd.DataFrame()
    return facts, px, tx, daily


def history(asset: Asset, limit: int = 15) -> list[dict[str, Any]]:
    df = db.query_df("SELECT date, signal, composite FROM signals WHERE asset_key = ? ORDER BY date DESC LIMIT ?",
                     [asset.key, limit])
    return [{"date": str(r["date"]), "signal": r["signal"],
             "composite": None if pd.isna(r["composite"]) else round(float(r["composite"]), 1)}
            for _, r in df.iterrows()]


def run(assets: list[Asset], scope: str, single_asset: bool = False, pdf: bool = True,
        skip_ingest: bool = False, workers: int | None = None) -> RunReport:
    t0 = time.time()
    report = RunReport(run_id=llm.RUN_ID, scope=scope, assets=len(assets))
    day = date.today().isoformat()
    http.reset_breakers()
    db.execute("INSERT OR REPLACE INTO runs VALUES (?, ?, NULL, ?, 'running', ?, 0, NULL)",
               [report.run_id, utcnow(), scope, len(assets)])

    # 1. market context (once per asset class)
    if not skip_ingest:
        for cls in sorted({a.asset_class for a in assets}):
            try:
                context.update(cls)
            except Exception as exc:  # noqa: BLE001
                log.warning("context update failed for %s: %s", cls, exc)
            # Benchmark prices for `src evaluate`
            bench = Asset("SPY", "SPDR S&P 500 ETF", "stock") if cls == "stock" else \
                Asset("BTC", "Bitcoin", "crypto", coingecko_id="bitcoin")
            try:
                prices.update(bench)
            except Exception as exc:  # noqa: BLE001
                log.warning("benchmark price update failed for %s: %s", bench.symbol, exc)

    # 2. ingestion, isolated per asset
    ok: list[Asset] = []
    if skip_ingest:
        ok = list(assets)
    else:
        pool = texts.Pool()
        workers = workers or config.settings()["run"]["workers"]
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(ingest_asset, a, pool, single_asset): a for a in assets}
            for i, fut in enumerate(as_completed(futures), start=1):
                a = futures[fut]
                try:
                    res = fut.result()
                    ok.append(a)
                    log.info("[%d/%d] ingested %s: %s", i, len(assets), a.symbol, res.get("texts"))
                except Exception as exc:  # noqa: BLE001
                    report.errors[a.key] = f"ingest: {exc}"
                    log.error("[%d/%d] ingest failed for %s: %s", i, len(assets), a.symbol, exc)
                    log.debug(traceback.format_exc())
        ok.sort(key=lambda a: assets.index(a))

    # 3. LLM scoring of new texts (concurrent requests across assets)
    try:
        report.scored_texts = scorer.score(ok)
    except Exception as exc:  # noqa: BLE001
        log.error("scoring failed: %s", exc)
        report.errors["scoring"] = str(exc)
        if isinstance(exc, llm.LLMSetupError):
            raise

    # 4. features + signal
    jobs, charts_svg = [], {}
    for a in ok:
        try:
            facts, px, tx, daily = build_facts(a)
            sig = signal_engine.decide(a.asset_class, a.symbol, facts).to_dict()
            display = fact_sheet.build(a, facts)
            top = sent_features.top_texts(tx) if len(tx) else {"bullish": [], "bearish": []}
            jobs.append((a, facts, display, sig, top))
            charts_svg[a.key] = charts.price_sentiment(px, daily)
        except Exception as exc:  # noqa: BLE001
            report.errors[a.key] = f"signal: {exc}"
            log.error("signal failed for %s: %s\n%s", a.symbol, exc, traceback.format_exc())

    # 5. narratives
    try:
        narratives = writer.write_many(jobs)
    except Exception as exc:  # noqa: BLE001
        log.error("narrative stage failed (%s); using templates", exc)
        narratives = {a.key: writer.template(a, sig, facts, top) for a, facts, _, sig, top in jobs}

    # 6. persist + render (sequential: the PDF browser is single-threaded)
    for a, facts, display, sig, top in jobs:
        narrative = narratives[a.key]
        db.execute("INSERT OR REPLACE INTO signals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
            a.key, date.today(), sig["signal"], sig["composite"], sig["confidence"], db.dumps(sig["subscores"]),
            db.dumps(sig["drivers"]), db.dumps(sig["flags"]), db.dumps({**facts, "signal": sig, "display": display,
                                                                         "top": top}),
            db.dumps(narrative), utcnow()])
        try:
            paths = render.render(a, day, {"facts": facts, "display": display, "signal": sig, "narrative": narrative,
                                           "top": top, "history": history(a), "chart_svg": charts_svg[a.key]},
                                  pdf=pdf)
            report.outputs[a.key] = {k: str(v) for k, v in paths.items()}
        except Exception as exc:  # noqa: BLE001
            report.errors[a.key] = f"render: {exc}"
            log.error("render failed for %s: %s", a.symbol, exc)
        report.signals[a.key] = sig["signal"]
    render.close_browser()

    cost = db.query_df("SELECT coalesce(sum(cost_usd), 0) AS c FROM llm_usage WHERE run_id = ?", [report.run_id])
    report.cost_usd = float(cost.iloc[0]["c"])
    report.seconds = round(time.time() - t0, 1)
    db.execute("UPDATE runs SET finished_at = ?, status = ?, n_failed = ?, errors = ? WHERE run_id = ?",
               [utcnow(), "ok" if not report.errors else "partial", len(report.errors),
                json.dumps(report.errors), report.run_id])
    try:
        db.publish_snapshot()  # the dashboard reads this copy, never the live database
    except Exception as exc:  # noqa: BLE001 - the run's results are stored either way
        log.error("dashboard snapshot not updated: %s", exc)
    return report
