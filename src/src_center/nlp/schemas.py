"""JSON schemas for LLM structured outputs (response_format json_schema) and their validated Python forms."""

from __future__ import annotations

from typing import Any

CATALYST_TAGS = ["earnings", "guidance", "analyst", "product", "partnership", "m_and_a", "regulation", "legal",
                 "macro", "management", "insider", "buyback_dividend", "listing", "hack_exploit",
                 "tokenomics_unlock", "etf_flows", "onchain", "technical_analysis", "hype_meme", "other"]

SCORE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "relevant": {"type": "boolean"},
                    "sentiment": {"type": "number"},
                    "confidence": {"type": "number"},
                    "stance": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
                    "horizon": {"type": "string", "enum": ["short", "long"]},
                    "catalyst_tags": {"type": "array", "items": {"type": "string", "enum": CATALYST_TAGS}},
                    "sarcasm_or_meme": {"type": "boolean"},
                },
                "required": ["id", "relevant", "sentiment", "confidence", "stance", "horizon", "catalyst_tags",
                             "sarcasm_or_meme"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

NARRATIVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "thesis": {"type": "string"},
        "bull_points": {"type": "array", "items": {"type": "string"}},
        "bear_points": {"type": "array", "items": {"type": "string"}},
        "dominant_narratives": {"type": "array", "items": {"type": "string"}},
        "upcoming_catalysts": {"type": "array", "items": {"type": "string"}},
        "key_risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["headline", "thesis", "bull_points", "bear_points", "dominant_narratives", "upcoming_catalysts",
                 "key_risks"],
    "additionalProperties": False,
}


def clamp(v: Any, lo: float, hi: float, default: float = 0.0) -> float:
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return default


def normalize_score_item(item: dict[str, Any]) -> dict[str, Any]:
    """Schema guarantees shape; numeric ranges and the prompt's consistency rules are enforced here
    (schemas don't carry min/max, and models don't always follow 'irrelevant -> 0' or stance/sign agreement)."""
    relevant = bool(item["relevant"])
    sentiment = clamp(item["sentiment"], -1.0, 1.0) if relevant else 0.0
    stance = "bullish" if sentiment >= 0.15 else "bearish" if sentiment <= -0.15 else "neutral"
    return {
        "id": int(item["id"]),
        "relevant": relevant,
        "sentiment": sentiment,
        "confidence": clamp(item["confidence"], 0.0, 1.0, 0.5) if relevant else 0.0,
        "stance": stance,
        "horizon": item["horizon"],
        "catalyst_tags": [t for t in item.get("catalyst_tags", []) if t in CATALYST_TAGS],
        "sarcasm_or_meme": bool(item["sarcasm_or_meme"]),
    }
