"""One-pager narrative: LLM via OpenRouter (structured output, grounded in the fact sheet) or a templated fallback."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from .. import config
from ..models import Asset
from ..nlp import llm
from ..nlp.schemas import NARRATIVE_SCHEMA

log = logging.getLogger(__name__)

PROMPT_VERSION = "narrative_v1"
PROMPT_PATH = Path(__file__).resolve().parents[1] / "nlp" / "prompts" / f"{PROMPT_VERSION}.md"
NUMBER_RE = re.compile(r"[-+]?\$?\d[\d,]*(?:\.\d+)?[%KMBT]?")


def _texts_block(top: dict[str, list[dict]]) -> str:
    lines = []
    for side in ("bullish", "bearish"):
        lines.append(f"Most {side} texts (7d):")
        for t in top.get(side, []):
            lines.append(f"- [{t['source']}, {t['published_at'][:10]}, sentiment {t['sentiment']:+.2f}] "
                         f"{t['title']} {(t['body'] or '')[:240]}".strip())
        if not top.get(side):
            lines.append("- (none)")
    return "\n".join(lines)


def build_prompt(asset: Asset, display: dict, signal: dict, top: dict) -> str:
    payload = {
        "asset": {"symbol": asset.symbol, "name": asset.name, "class": asset.asset_class, "sector": asset.sector},
        "signal": signal["signal"], "rule": signal["rule"], "composite_score_0_100": signal["composite"],
        "confidence_0_1": signal["confidence"], "subscores_0_100": signal["subscores"],
        "subscore_reasons": signal["reasons"], "flags": signal["flags"], "fact_sheet": display,
    }
    return f"{json.dumps(payload, indent=1, ensure_ascii=False)}\n\n{_texts_block(top)}"


def request_params(asset: Asset, display: dict, signal: dict, top: dict) -> dict[str, Any]:
    lcfg = config.settings()["llm"]
    return llm.structured_params(lcfg["narrative_model"], lcfg["narrative_reasoning"],
                                 PROMPT_PATH.read_text(encoding="utf-8"), build_prompt(asset, display, signal, top),
                                 NARRATIVE_SCHEMA, "narrative", max_tokens=16000)


def unverified_numbers(narrative: dict[str, Any], grounding: str) -> list[str]:
    """Numbers in the narrative that do not appear verbatim in the prompt (fact sheet + texts)."""
    text = " ".join([narrative.get("headline", ""), narrative.get("thesis", "")]
                    + [x for k in ("bull_points", "bear_points", "dominant_narratives", "upcoming_catalysts",
                                   "key_risks") for x in narrative.get(k, [])])
    norm_ground = grounding.replace(",", "")
    bad = []
    for n in NUMBER_RE.findall(text):
        core = n.lstrip("+-$").rstrip("%KMBT").replace(",", "")
        if not core or len(core.replace(".", "")) <= 1 or re.fullmatch(r"(19|20)\d\d", core):
            continue  # single digits and years are fine
        if core not in norm_ground:
            bad.append(n)
    return sorted(set(bad))


def template(asset: Asset, signal: dict, facts: dict, top: dict) -> dict[str, Any]:
    reasons = signal["reasons"]
    scores = signal["subscores"]
    pos = [f"{k.replace('_', ' ').capitalize()}: {'; '.join(reasons[k][:2])}" for k, v in scores.items()
           if v is not None and v >= 55 and reasons.get(k)]
    neg = [f"{k.replace('_', ' ').capitalize()}: {'; '.join(reasons[k][:2])}" for k, v in scores.items()
           if v is not None and v <= 45 and reasons.get(k)]
    catalysts = []
    if facts.get("stock_meta", {}).get("next_earnings"):
        catalysts.append(f"Earnings on {facts['stock_meta']['next_earnings']}")
    label = {"HOLD": "HOLD (don't buy)"}.get(signal["signal"], signal["signal"])
    lead = signal["drivers"][0].split(" (")[0].lower() if signal["drivers"] else "no dominant factor"
    comp = f"composite {signal['composite']}" if signal["composite"] is not None else "insufficient data"
    return {
        "headline": f"{label} — {comp} ({signal['rule']}); strongest factor: {lead}",
        "thesis": " ".join(["Automated summary (LLM narrative disabled)."] + signal["drivers"]),
        "bull_points": pos[:3] or ["No clearly positive factor"],
        "bear_points": neg[:3] or ["No clearly negative factor"],
        "dominant_narratives": facts.get("sentiment", {}).get("top_tags_7d", [])[:4],
        "upcoming_catalysts": catalysts,
        "key_risks": signal["flags"][:3] or ["Sentiment regime shift", "Macro shock"],
        "source": "template",
    }


def write_many(jobs: list[tuple[Asset, dict, dict, dict, dict]]) -> dict[str, dict]:
    """jobs: (asset, facts, display_facts, signal, top_texts). Returns {asset_key: narrative}."""
    if config.llm_provider() == "none":
        return {a.key: template(a, sig, facts, top) for a, facts, _, sig, top in jobs}
    requests, by_cid = {}, {}
    for asset, facts, display, sig, top in jobs:
        cid = re.sub(r"[^A-Za-z0-9_-]", "_", f"n-{asset.asset_class}-{asset.symbol}")[:64]
        requests[cid] = request_params(asset, display, sig, top)
        by_cid[cid] = (asset, facts, display, sig, top)
    results = llm.run(requests, stage="narrative")
    out = {}
    for cid, (asset, facts, display, sig, top) in by_cid.items():
        narrative = results.get(cid)
        if narrative is None:
            log.warning("narrative failed for %s; using template", asset.symbol)
            out[asset.key] = template(asset, sig, facts, top)
            continue
        narrative["source"] = config.settings()["llm"]["narrative_model"]
        narrative["unverified_numbers"] = unverified_numbers(
            narrative, requests[cid]["messages"][0]["content"])
        out[asset.key] = narrative
    return out
