"""Exercises the OpenRouter scoring + narrative plumbing against a fake OpenAI-compatible client, without network."""

import json
import os
import re
from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd

from src_center import config
from src_center.models import Asset
from src_center.narrative import writer
from src_center.nlp import llm, scorer
from src_center.storage import db


def _response(params, cost=0.0004, finish="stop", fenced=False):
    user = params["messages"][1]["content"]
    ids = [int(i) for i in re.findall(r'<text id="(\d+)">', user)]
    if ids:
        payload = {"items": [{"id": i, "relevant": i % 2 == 1, "sentiment": 0.5, "confidence": 0.9,
                              "stance": "bullish", "horizon": "short", "catalyst_tags": ["earnings"],
                              "sarcasm_or_meme": False} for i in ids]}
    else:
        payload = {"headline": "BUY on improving sentiment", "thesis": "Price $123.45 rising.", "bull_points": ["a"],
                   "bear_points": ["b"], "dominant_narratives": ["c"], "upcoming_catalysts": [], "key_risks": ["d"]}
    text = json.dumps(payload)
    if fenced:
        text = f"```json\n{text}\n```"
    usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=200, cost=cost,
                            prompt_tokens_details=SimpleNamespace(cached_tokens=100))
    message = SimpleNamespace(content=text, refusal=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)], usage=usage)


class FakeClient:
    def __init__(self, **kw):
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.kw = kw

    def _create(self, **params):
        self.calls.append(params)
        return _response(params, **self.kw)


def _seed_texts(asset, n):
    now = datetime.utcnow()
    db.upsert_df("texts", pd.DataFrame([{
        "text_id": f"{asset.symbol}{i}", "asset_key": asset.key, "source": "google_news", "kind": "news",
        "title": f"{asset.name} headline number {i}", "body": "", "url": f"https://x/{asset.symbol}/{i}",
        "published_at": now - timedelta(hours=i), "engagement": 0.0, "author_label": None, "fetched_at": now,
    } for i in range(n)]))


def test_scoring(monkeypatch):
    os.environ["SRC_LLM_PROVIDER"] = "openrouter"
    fake = FakeClient()
    monkeypatch.setattr(llm, "_client", fake)
    assets = [Asset("NVDA", "NVIDIA Corporation", "stock"), Asset("BTC", "Bitcoin", "crypto")]
    for a in assets:
        _seed_texts(a, 25)  # 25 texts -> 2 requests per asset at 20 texts/request
    n = scorer.score(assets)
    scores = db.query_df("SELECT * FROM text_scores")
    usage = db.query_df("SELECT * FROM llm_usage")
    assert n == 50 and len(scores) == 50
    assert scores["relevant"].sum() == 26  # odd ids in chunks of 20 and 5 -> (10+3) per asset
    assert set(scores["model"]) == {config.settings()["llm"]["scoring_model"]}
    assert len(usage) == 4
    assert abs(usage["cost_usd"].sum() - 4 * 0.0004) < 1e-12      # OpenRouter-reported cost is logged as is
    assert usage["cache_read_tokens"].sum() == 400
    req = fake.calls[0]
    assert req["model"] == config.settings()["llm"]["scoring_model"]
    assert req["response_format"]["json_schema"]["strict"] is True
    assert req["extra_body"]["provider"] == {"require_parameters": True}
    assert req["extra_body"]["reasoning"] == {"enabled": False}  # scoring_reasoning: off


def test_fenced_json_and_truncation(monkeypatch):
    params = llm.structured_params("m", "low", "sys", '<text id="1">x</text>', {"type": "object"}, "t")
    assert llm._parse(_response(params, fenced=True), "x")["items"][0]["id"] == 1
    assert llm._parse(_response(params, finish="length"), "x") is None
    assert params["extra_body"]["reasoning"] == {"effort": "low", "exclude": True}


def test_cost_fallback_to_configured_prices():
    model = config.settings()["llm"]["scoring_model"]
    price = config.settings()["llm"]["prices"][model]
    usage = SimpleNamespace(prompt_tokens=1_000_000, completion_tokens=1_000_000)  # no .cost reported
    assert abs(llm._cost(model, usage) - (price["input"] + price["output"])) < 1e-9


def test_narrative_flags_unverified_numbers(monkeypatch):
    os.environ["SRC_LLM_PROVIDER"] = "openrouter"
    monkeypatch.setattr(llm, "_client", FakeClient())
    asset = Asset("NVDA", "NVIDIA Corporation", "stock")
    sig = {"signal": "BUY", "rule": "composite >= 65", "composite": 70.0, "confidence": 0.8,
           "subscores": {"trend": 70}, "reasons": {"trend": ["up"]}, "flags": [], "drivers": ["Trend 70"]}
    out = writer.write_many([(asset, {}, {"Price": {"Price": "$120.00"}}, sig, {"bullish": [], "bearish": []})])
    assert out[asset.key]["headline"].startswith("BUY")
    assert out[asset.key]["source"] == config.settings()["llm"]["narrative_model"]
    assert out[asset.key]["unverified_numbers"] == ["$123.45"]


def test_missing_key_fails_fast(monkeypatch):
    monkeypatch.setattr(llm, "_client", None)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    try:
        llm.run({"a": {"model": "m"}}, "score")
    except RuntimeError as exc:
        assert "OPENROUTER_API_KEY" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")
