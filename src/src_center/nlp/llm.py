"""LLM access via OpenRouter (OpenAI-compatible Chat Completions), concurrent requests, cost logging.

Every stage builds `{custom_id: params}` request dicts and calls `run()`. The result is `{custom_id: parsed JSON}`.
Requests that fail, are refused or get truncated are missing from it, and callers treat them as unscored.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

import openai
import pandas as pd

from .. import config
from ..storage import db

log = logging.getLogger(__name__)

BASE_URL = "https://openrouter.ai/api/v1"
_client: openai.OpenAI | None = None
RUN_ID = datetime.utcnow().strftime("%Y%m%dT%H%M%S")


def client() -> openai.OpenAI:
    global _client
    if _client is None:
        key = config.env("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY is not set. Add it to .env, or run with --no-llm.")
        _client = openai.OpenAI(
            base_url=BASE_URL, api_key=key, max_retries=4, timeout=180,
            # Optional OpenRouter attribution headers (shown in their activity log).
            default_headers={"HTTP-Referer": "https://localhost/sentiment-research-center",
                             "X-Title": "Sentiment Research Center"})
    return _client


def _cost(model: str, usage: Any) -> float:
    """OpenRouter reports the billed USD cost per call (usage.cost); fall back to the configured prices."""
    reported = getattr(usage, "cost", None)
    if reported is not None:
        return float(reported)
    price = config.settings()["llm"]["prices"].get(model)
    if not price:
        return 0.0
    return (usage.prompt_tokens * price["input"] + usage.completion_tokens * price["output"]) / 1e6


def _log_usage(stage: str, model: str, usage: Any) -> None:
    if usage is None:
        return
    details = getattr(usage, "prompt_tokens_details", None)
    db.upsert_df("llm_usage", pd.DataFrame([{
        "ts": datetime.utcnow(), "run_id": RUN_ID, "stage": stage, "model": model, "batch": False,
        "input_tokens": usage.prompt_tokens, "output_tokens": usage.completion_tokens,
        "cache_read_tokens": (getattr(details, "cached_tokens", 0) or 0) if details else 0,
        "cache_write_tokens": 0,
        "cost_usd": _cost(model, usage),
    }]))


FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$")


def _parse(response: Any, custom_id: str) -> dict[str, Any] | None:
    if not response.choices:
        log.warning("request %s returned no choices", custom_id)
        return None
    choice = response.choices[0]
    if choice.finish_reason == "length":
        log.warning("request %s hit max_tokens; output dropped", custom_id)
        return None
    if getattr(choice.message, "refusal", None):
        log.warning("request %s refused: %s", custom_id, choice.message.refusal)
        return None
    text = FENCE_RE.sub("", (choice.message.content or "").strip())
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        log.warning("request %s returned invalid JSON", custom_id)
        return None


def _one(custom_id: str, params: dict[str, Any], stage: str) -> tuple[str, dict[str, Any] | None]:
    try:
        response = client().chat.completions.create(**params)
    except openai.BadRequestError as exc:
        log.error("request %s rejected: %s", custom_id, exc.message)
        return custom_id, None
    except (openai.RateLimitError, openai.InternalServerError, openai.APIConnectionError,
            openai.APITimeoutError) as exc:
        log.error("request %s failed after retries: %s", custom_id, exc)
        return custom_id, None
    except openai.APIStatusError as exc:  # 402 no credits, 403 moderation, ...
        log.error("request %s failed (%s): %s", custom_id, exc.status_code, exc.message)
        return custom_id, None
    _log_usage(stage, params["model"], response.usage)
    return custom_id, _parse(response, custom_id)


def run(requests: dict[str, dict[str, Any]], stage: str) -> dict[str, dict[str, Any]]:
    if not requests:
        return {}
    client()  # fail fast on a missing key instead of once per request
    workers = config.settings()["llm"].get("concurrency", 8)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(lambda kv: _one(kv[0], kv[1], stage), requests.items())
    out = {cid: parsed for cid, parsed in results if parsed is not None}
    if len(out) < len(requests):
        log.warning("%s: %d of %d requests failed", stage, len(requests) - len(out), len(requests))
    return out


def structured_params(model: str, reasoning: str, system: str, user: str, schema: dict[str, Any],
                      name: str, max_tokens: int = 8000) -> dict[str, Any]:
    """Chat Completions request with a strict JSON-schema response.
    reasoning: 'off' | 'low' | 'medium' | 'high' (OpenRouter's unified reasoning parameter)."""
    extra: dict[str, Any] = {
        "usage": {"include": True},  # adds the billed cost to response.usage
        # Only route to providers that honour response_format/json_schema for this model.
        "provider": {"require_parameters": True},
        "reasoning": {"enabled": False} if reasoning == "off" else {"effort": reasoning, "exclude": True},
    }
    return {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}},
        "extra_body": extra,
    }
