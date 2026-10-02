"""Text sentiment scoring: LLM (structured outputs, ~20 texts per request) or the offline VADER fallback."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pandas as pd

from .. import config
from ..models import Asset
from ..storage import db
from . import llm
from .schemas import SCORE_SCHEMA, normalize_score_item
from ..timeutil import utcnow

log = logging.getLogger(__name__)

PROMPT_VERSION = "score_v1"
PROMPT_DIR = Path(__file__).parent / "prompts"


def _system_prompt() -> str:
    return (PROMPT_DIR / f"{PROMPT_VERSION}.md").read_text(encoding="utf-8")


def _format_texts(asset: Asset, rows: pd.DataFrame) -> str:
    kind = "stock" if asset.asset_class == "stock" else "crypto asset"
    lines = [f"Asset: {asset.name} (ticker {asset.symbol}), a {kind}.", "", "Texts:"]
    for i, (_, r) in enumerate(rows.iterrows(), start=1):
        header = f"[{i}] source={r['source']} date={pd.Timestamp(r['published_at']).date()}"
        if r.get("author_label"):
            header += f" author_tag={r['author_label']}"
        body = f"{r['title']}\n{r['body']}".strip()
        lines.append(f"<text id=\"{i}\">\n{header}\n{body}\n</text>")
    return "\n".join(lines)


def _custom_id(asset: Asset, chunk: int) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", f"{asset.asset_class}-{asset.symbol}")[:56] + f"-{chunk}"


def _store(asset_key: str, rows: pd.DataFrame, items: list[dict], model: str) -> int:
    by_id = {it["id"]: it for it in (normalize_score_item(x) for x in items)}
    now = utcnow()
    records = []
    for i, (_, r) in enumerate(rows.iterrows(), start=1):
        it = by_id.get(i)
        if it is None:
            continue
        records.append({
            "text_id": r["text_id"], "asset_key": asset_key, "relevant": it["relevant"],
            "sentiment": it["sentiment"], "confidence": it["confidence"], "stance": it["stance"],
            "horizon": it["horizon"], "tags": json.dumps(it["catalyst_tags"]), "sarcasm": it["sarcasm_or_meme"],
            "model": model, "prompt_version": PROMPT_VERSION, "scored_at": now,
        })
    return db.upsert_df("text_scores", pd.DataFrame(records))


def score_vader(assets: list[Asset], pending: pd.DataFrame) -> int:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    sia = SentimentIntensityAnalyzer()
    n = 0
    for asset in assets:
        rows = pending[pending["asset_key"] == asset.key].reset_index(drop=True)
        items = []
        for i, (_, r) in enumerate(rows.iterrows(), start=1):
            compound = sia.polarity_scores(f"{r['title']}. {r['body']}")["compound"]
            if r.get("author_label") in ("Bullish", "Bearish"):  # StockTwits self-declared stance
                compound = 0.5 * compound + 0.5 * (0.6 if r["author_label"] == "Bullish" else -0.6)
            stance = "bullish" if compound >= 0.15 else "bearish" if compound <= -0.15 else "neutral"
            items.append({"id": i, "relevant": True, "sentiment": compound, "confidence": 0.4, "stance": stance,
                          "horizon": "short", "catalyst_tags": [], "sarcasm_or_meme": False})
        n += _store(asset.key, rows, items, "vader")
    return n


def score_llm(assets: list[Asset], pending: pd.DataFrame) -> int:
    lcfg = config.llm()
    per_request = lcfg["texts_per_request"]
    system = _system_prompt()
    requests: dict[str, dict] = {}
    chunks: dict[str, tuple[Asset, pd.DataFrame]] = {}
    for asset in assets:
        rows = pending[pending["asset_key"] == asset.key].reset_index(drop=True)
        for c, start in enumerate(range(0, len(rows), per_request)):
            chunk = rows.iloc[start:start + per_request].reset_index(drop=True)
            cid = _custom_id(asset, c)
            chunks[cid] = (asset, chunk)
            requests[cid] = llm.structured_params(
                lcfg["scoring_model"], lcfg["scoring_reasoning"], system, _format_texts(asset, chunk), SCORE_SCHEMA,
                "text_scores",
                max_tokens=8000)
    log.info("scoring %d texts in %d requests", len(pending), len(requests))
    results = llm.run(requests, stage="score")
    n = 0
    for cid, parsed in results.items():
        asset, chunk = chunks[cid]
        n += _store(asset.key, chunk, parsed.get("items", []), lcfg["scoring_model"])
    return n


def score(assets: list[Asset]) -> int:
    """Score every not-yet-scored text of the given assets (capped per asset). Returns the number scored."""
    tcfg = config.settings()["texts"]
    scored = set(db.query_df("SELECT DISTINCT asset_key FROM text_scores")["asset_key"])
    # Assets scored for the first time get a larger backfill budget so momentum has history to compare against.
    new = [a.key for a in assets if a.key not in scored]
    old = [a.key for a in assets if a.key in scored]
    pending = pd.concat([db.unscored_texts(new, tcfg["backfill_cap"]),
                         db.unscored_texts(old, tcfg["max_per_asset_per_day"])], ignore_index=True)
    if pending.empty:
        return 0
    if config.llm_provider() == "none":
        return score_vader(assets, pending)
    return score_llm(assets, pending)
