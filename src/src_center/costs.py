"""LLM (OpenRouter) cost estimate for an asset list, built from the real prompts the pipeline would send.

Input tokens are estimated from the actual prompt text: stored texts formatted exactly as the scorer formats them,
and narrative prompts rebuilt from stored signals. Output and reasoning tokens are assumptions (see ASSUMPTIONS).
Prices come live from OpenRouter's public model list, with settings.yaml as fallback.
"""

from __future__ import annotations

import json
import math
from typing import Any

import pandas as pd

from . import config, http
from .models import Asset
from .narrative import writer
from .nlp import scorer
from .nlp.schemas import NARRATIVE_SCHEMA, SCORE_SCHEMA
from .storage import db

# Output-side assumptions (not measurable without real calls; replace with `src costs` data once live).
ASSUMPTIONS = {
    "score_output_tokens_per_text": 60,       # one JSON item per text
    "score_thinking_tokens_per_request": (0, 300),    # reasoning off for scoring: (low, high) allowance
    "narrative_output_tokens": 700,           # headline, thesis, 3+3 points, lists
    "narrative_thinking_tokens": (300, 2000),  # reasoning effort=low
    "chars_per_token": 3.6,                   # DeepSeek tokenizer, English news + JSON
    "schema_overhead_tokens": 300,            # structured-output grammar/system overhead per request
    # for assets without stored texts yet; measured 2026-09-28: stocks ~12/day Google News + ~5-10 Yahoo news,
    # crypto 4-13/day (Google News + crypto RSS), both before Reddit/Finnhub keys and the extra crypto feeds were added
    "default_texts_per_day": {"stock": 20, "crypto": 8},
    "stock_runs_per_month": 21.7,             # Mon-Fri
    "crypto_runs_per_month": 30.4,
}


def _count_tokens(model: str, system: str, user: str, schema: dict) -> int:
    return int((len(system) + len(user) + len(json.dumps(schema))) / ASSUMPTIONS["chars_per_token"]) \
        + ASSUMPTIONS["schema_overhead_tokens"]


def texts_per_day(asset: Asset) -> float:
    df = db.query_df("SELECT count(*) AS n FROM texts WHERE asset_key = ? AND published_at >= now() - INTERVAL 7 DAY",
                     [asset.key])
    n = float(df.iloc[0]["n"]) / 7
    return n if n > 0 else ASSUMPTIONS["default_texts_per_day"][asset.asset_class]


def _sample_texts(asset: Asset, n: int) -> pd.DataFrame:
    df = db.query_df("SELECT * FROM texts WHERE asset_key = ? ORDER BY published_at DESC LIMIT ?", [asset.key, n])
    if df.empty:  # asset never ingested: borrow typical texts from any asset
        df = db.query_df("SELECT * FROM texts ORDER BY published_at DESC LIMIT ?", [n])
    return df


def per_asset_tokens(assets: list[Asset], cap_texts: int | None = None, clamp: bool = True) -> pd.DataFrame:
    """Per-run requests and input tokens per asset for scoring and narrative.
    cap_texts: fixed texts per asset instead of the measured 7-day rate; clamp: apply the daily LLM cap."""
    lcfg = config.settings()["llm"]
    per_req = lcfg["texts_per_request"]
    system = scorer._system_prompt()
    narrative_system = writer.PROMPT_PATH.read_text(encoding="utf-8")
    rows = []
    for a in assets:
        n = cap_texts if cap_texts is not None else texts_per_day(a)
        if clamp:
            n = min(n, config.settings()["texts"]["max_per_asset_per_day"])
        n = max(1, math.ceil(n))
        sample = _sample_texts(a, per_req)
        full = _count_tokens(lcfg["scoring_model"], system, scorer._format_texts(a, sample), SCORE_SCHEMA)
        base = _count_tokens(lcfg["scoring_model"], system, scorer._format_texts(a, sample.iloc[:0]), SCORE_SCHEMA)
        per_text = (full - base) / max(len(sample), 1)
        requests = math.ceil(n / per_req)
        score_in = requests * base + n * per_text
        narrative_in = _narrative_input(a, narrative_system, lcfg["narrative_model"])
        rows.append({"asset": a.key, "texts_per_day": n, "score_requests": requests, "score_input": score_in,
                     "narrative_input": narrative_in, "runs_per_month": ASSUMPTIONS[f"{a.asset_class}_runs_per_month"]})
    return pd.DataFrame(rows)


def _narrative_input(a: Asset, system: str, model: str) -> int:
    df = db.query_df("SELECT facts, narrative FROM signals WHERE asset_key = ? ORDER BY date DESC LIMIT 1", [a.key])
    if df.empty:
        df = db.query_df("SELECT facts FROM signals WHERE asset_key LIKE ? ORDER BY date DESC LIMIT 1",
                         [f"{a.asset_class}:%"])
    if df.empty:
        return 2500
    facts = json.loads(df.iloc[0]["facts"])
    prompt = writer.build_prompt(a, facts["display"], facts["signal"], facts["top"])
    return _count_tokens(model, system, prompt, NARRATIVE_SCHEMA)


def live_prices() -> dict[str, dict[str, float]]:
    """USD per 1M tokens for every OpenRouter model (public endpoint, cached 1 day)."""
    try:
        data = http.get("https://openrouter.ai/api/v1/models", ttl=86400)["data"]
    except (http.SourceUnavailable, KeyError, TypeError):
        return {}
    out = {}
    for m in data:
        p = m.get("pricing") or {}
        try:
            out[m["id"]] = {"input": float(p["prompt"]) * 1e6, "output": float(p["completion"]) * 1e6}
        except (KeyError, TypeError, ValueError):
            continue
    return out


def price(model: str, live: dict[str, dict[str, float]] | None = None) -> dict[str, float]:
    if live and model in live:
        return live[model]
    configured = config.settings()["llm"]["prices"].get(model)
    if configured is None:
        raise KeyError(f"no price for {model}: not on OpenRouter's model list and not in settings.yaml llm.prices")
    return configured


def estimate(tokens: pd.DataFrame, scoring_model: str, narrative_model: str, per: str = "day",
             live: dict[str, dict[str, float]] | None = None) -> dict[str, Any]:
    """Cost for one pipeline run of all assets (per='day') or a month of scheduled runs (per='month')."""
    a = ASSUMPTIONS
    mult = tokens["runs_per_month"] if per == "month" else 1.0
    n_texts = (tokens["texts_per_day"] * mult).sum()
    n_req = (tokens["score_requests"] * mult).sum()
    n_assets = mult.sum() if per == "month" else len(tokens)
    s_in = (tokens["score_input"] * mult).sum()
    n_in = (tokens["narrative_input"] * mult).sum()
    s_out = [n_texts * a["score_output_tokens_per_text"] + n_req * t for t in a["score_thinking_tokens_per_request"]]
    n_out = [n_assets * (a["narrative_output_tokens"] + t) for t in a["narrative_thinking_tokens"]]
    ps, pn = price(scoring_model, live), price(narrative_model, live)
    score_cost = [(s_in * ps["input"] + o * ps["output"]) / 1e6 for o in s_out]
    narr_cost = [(n_in * pn["input"] + o * pn["output"]) / 1e6 for o in n_out]
    return {
        "texts": n_texts, "score_requests": n_req, "score_input_tokens": s_in, "score_output_tokens": s_out,
        "narrative_input_tokens": n_in, "narrative_output_tokens": n_out,
        "score_cost": score_cost, "narrative_cost": narr_cost,
        "total": [score_cost[0] + narr_cost[0], score_cost[1] + narr_cost[1]],
    }
