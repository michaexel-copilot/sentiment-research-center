"""LLM access, concurrent requests, usage logging.

Two providers (`llm.provider` in settings.yaml):
- "claude": your Claude subscription, through the Claude Code CLI in headless mode (`claude -p` with a JSON
  schema). No API key; the CLI must be installed and logged in with your claude.ai account.
- "openrouter": OpenRouter's OpenAI-compatible Chat Completions API (needs OPENROUTER_API_KEY).

Every stage builds `{custom_id: params}` request dicts with `structured_params()` and calls `run()`. The result is
`{custom_id: parsed JSON}`. Requests that fail, are refused or get truncated are missing from it, and callers treat
them as unscored.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import lru_cache
from typing import Any

import openai
import pandas as pd

from .. import config
from ..storage import db

log = logging.getLogger(__name__)

BASE_URL = "https://openrouter.ai/api/v1"
_client: openai.OpenAI | None = None
RUN_ID = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
# Credentials that would make the CLI bill an API account instead of the subscription.
API_CREDENTIAL_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console pop-ups when started from the dashboard


class LLMSetupError(RuntimeError):
    """The configured provider can't be used at all (missing key, CLI not installed or not logged in)."""


def client() -> openai.OpenAI:
    global _client
    if _client is None:
        key = config.env("OPENROUTER_API_KEY")
        if not key:
            raise LLMSetupError("OPENROUTER_API_KEY is not set. Add it to .env, or run with --no-llm.")
        _client = openai.OpenAI(
            base_url=BASE_URL, api_key=key, max_retries=4, timeout=180,
            # Optional OpenRouter attribution headers (shown in their activity log).
            default_headers={"HTTP-Referer": "https://localhost/sentiment-research-center",
                             "X-Title": "Sentiment Research Center"})
    return _client


def claude_cli() -> str | None:
    return shutil.which("claude")


@lru_cache
def check_claude() -> None:
    """Fail fast when the Claude Code CLI is missing or not logged in."""
    exe = claude_cli()
    if not exe:
        raise LLMSetupError("Claude Code CLI not found on PATH. Install it (https://claude.com/claude-code), run "
                            "`claude` once to log in with your Claude subscription, or run with --no-llm.")
    proc = subprocess.run([exe, "auth", "status"], capture_output=True, text=True, encoding="utf-8",
                          env=_claude_env(), timeout=60, creationflags=_NO_WINDOW)
    try:
        status = json.loads(proc.stdout)
    except json.JSONDecodeError:
        status = {}
    if not status.get("loggedIn"):
        raise LLMSetupError("Claude Code is not logged in. Run `claude auth login` with your Claude subscription "
                            "account, or run with --no-llm.")
    if status.get("authMethod") != "claude.ai":
        log.warning("Claude Code is authenticated via %s, not a claude.ai subscription; calls may be billed "
                    "to an API account", status.get("authMethod"))


def available() -> bool:
    """Whether the configured provider looks usable (cheap check, no network)."""
    provider = config.llm_provider()
    if provider == "claude":
        return claude_cli() is not None
    if provider == "openrouter":
        return config.env("OPENROUTER_API_KEY") is not None
    return False


def _claude_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in API_CREDENTIAL_VARS}


def _cost(model: str, usage: Any) -> float:
    """OpenRouter reports the billed USD cost per call (usage.cost); fall back to the configured prices."""
    reported = getattr(usage, "cost", None)
    if reported is not None:
        return float(reported)
    price = config.settings()["llm"]["prices"].get(model)
    if not price:
        return 0.0
    return (usage.prompt_tokens * price["input"] + usage.completion_tokens * price["output"]) / 1e6


def _log_usage(stage: str, model: str, input_tokens: int, output_tokens: int, cache_read: int, cache_write: int,
               cost: float) -> None:
    db.upsert_df("llm_usage", pd.DataFrame([{
        "ts": datetime.utcnow(), "run_id": RUN_ID, "stage": stage, "model": model, "batch": False,
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "cache_read_tokens": cache_read, "cache_write_tokens": cache_write, "cost_usd": cost,
    }]))


FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$")


def _parse_text(text: str | None, custom_id: str) -> dict[str, Any] | None:
    text = FENCE_RE.sub("", (text or "").strip())
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        log.warning("request %s returned invalid JSON", custom_id)
        return None


# --- OpenRouter -------------------------------------------------------------------------------------------------

def openrouter_kwargs(params: dict[str, Any]) -> dict[str, Any]:
    """Chat Completions request with a strict JSON-schema response."""
    reasoning = params["reasoning"]
    extra: dict[str, Any] = {
        "usage": {"include": True},  # adds the billed cost to response.usage
        # Only route to providers that honour response_format/json_schema for this model.
        "provider": {"require_parameters": True},
        "reasoning": {"enabled": False} if reasoning == "off" else {"effort": reasoning, "exclude": True},
    }
    return {
        "model": params["model"],
        "max_tokens": params["max_tokens"],
        "messages": [{"role": "system", "content": params["system"]}, {"role": "user", "content": params["user"]}],
        "response_format": {"type": "json_schema", "json_schema": {"name": params["name"], "strict": True,
                                                                   "schema": params["schema"]}},
        "extra_body": extra,
    }


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
    return _parse_text(choice.message.content, custom_id)


def _one_openrouter(custom_id: str, params: dict[str, Any], stage: str) -> dict[str, Any] | None:
    try:
        response = client().chat.completions.create(**openrouter_kwargs(params))
    except openai.BadRequestError as exc:
        log.error("request %s rejected: %s", custom_id, exc.message)
        return None
    except (openai.RateLimitError, openai.InternalServerError, openai.APIConnectionError,
            openai.APITimeoutError) as exc:
        log.error("request %s failed after retries: %s", custom_id, exc)
        return None
    except openai.APIStatusError as exc:  # 402 no credits, 403 moderation, ...
        log.error("request %s failed (%s): %s", custom_id, exc.status_code, exc.message)
        return None
    usage = response.usage
    if usage is not None:
        details = getattr(usage, "prompt_tokens_details", None)
        _log_usage(stage, params["model"], usage.prompt_tokens, usage.completion_tokens,
                   (getattr(details, "cached_tokens", 0) or 0) if details else 0, 0, _cost(params["model"], usage))
    return _parse(response, custom_id)


# --- Claude subscription via the Claude Code CLI ----------------------------------------------------------------

def claude_command(params: dict[str, Any]) -> list[str]:
    # Opus 5.5 can't turn thinking off; low effort is the closest to "off".
    effort = "low" if params["reasoning"] == "off" else params["reasoning"]
    return [claude_cli() or "claude", "-p", "--output-format", "json",
            "--model", params["model"], "--effort", effort,
            "--system-prompt", params["system"], "--json-schema", json.dumps(params["schema"]),
            # A plain completion: no tools, no MCP servers, no user/project settings or hooks, no saved session.
            "--tools", "", "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence"]


def _one_claude(custom_id: str, params: dict[str, Any], stage: str) -> dict[str, Any] | None:
    timeout = config.llm().get("timeout", 300)
    try:
        # cwd outside the project so no CLAUDE.md is picked up; the prompt goes in on stdin.
        proc = subprocess.run(claude_command(params), input=params["user"], capture_output=True, text=True,
                              encoding="utf-8", env=_claude_env(), cwd=tempfile.gettempdir(), timeout=timeout,
                              creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        log.error("request %s timed out after %ss", custom_id, timeout)
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        log.error("request %s failed (exit %s): %s", custom_id, proc.returncode,
                  (proc.stderr or proc.stdout).strip()[-500:])
        return None
    usage = data.get("usage") or {}
    if usage:
        # Covered by the subscription, so nothing is billed per call (the CLI's total_cost_usd is a list-price
        # equivalent, not a charge).
        _log_usage(stage, params["model"], usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                   usage.get("cache_read_input_tokens", 0), usage.get("cache_creation_input_tokens", 0), 0.0)
    if data.get("is_error") or data.get("subtype") != "success":
        log.error("request %s failed (%s): %s", custom_id, data.get("api_error_status") or data.get("subtype"),
                  str(data.get("result", ""))[:500])
        return None
    if data.get("stop_reason") == "refusal":
        log.warning("request %s refused", custom_id)
        return None
    parsed = data.get("structured_output")
    return parsed if isinstance(parsed, dict) else _parse_text(data.get("result"), custom_id)


# --- shared ------------------------------------------------------------------------------------------------------

def run(requests: dict[str, dict[str, Any]], stage: str) -> dict[str, dict[str, Any]]:
    if not requests:
        return {}
    provider = config.llm_provider()
    # Fail fast on a missing key / CLI instead of once per request.
    if provider == "claude":
        check_claude()
        one = _one_claude
    elif provider == "openrouter":
        client()
        one = _one_openrouter
    else:
        raise LLMSetupError(f"unknown llm.provider {provider!r} (expected claude, openrouter or none)")
    workers = config.llm().get("concurrency", 8)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(lambda kv: (kv[0], one(kv[0], kv[1], stage)), requests.items())
    out = {cid: parsed for cid, parsed in results if parsed is not None}
    if len(out) < len(requests):
        log.warning("%s: %d of %d requests failed", stage, len(requests) - len(out), len(requests))
    return out


def structured_params(model: str, reasoning: str, system: str, user: str, schema: dict[str, Any],
                      name: str, max_tokens: int = 8000) -> dict[str, Any]:
    """Provider-neutral request for a JSON-schema response.
    reasoning: 'off' | 'low' | 'medium' | 'high' (OpenRouter's reasoning effort / Claude's effort level)."""
    return {"model": model, "reasoning": reasoning, "system": system, "user": user, "schema": schema,
            "name": name, "max_tokens": max_tokens}
