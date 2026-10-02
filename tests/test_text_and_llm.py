from datetime import datetime
from types import SimpleNamespace

from src_center.ingest.textmatch import filter_and_dedupe, mentions
from src_center.models import Asset, TextItem
from src_center.narrative import writer
from src_center.nlp import llm
from src_center.nlp.schemas import normalize_score_item
from src_center.timeutil import utcnow


def test_mentions_handles_ambiguous_tickers():
    cost = Asset("COST", "Costco Wholesale Corporation", "stock")
    assert mentions(cost, "Costco beats on comps")
    assert mentions(cost, "loading up on $COST")
    assert not mentions(cost, "the COST of living keeps rising")
    nvda = Asset("NVDA", "NVIDIA Corporation", "stock")
    assert mentions(nvda, "NVDA calls printing") and mentions(nvda, "Nvidia unveils new GPU")
    link = Asset("LINK", "Chainlink", "crypto")
    assert not mentions(link, "click the link below")
    assert mentions(link, "$LINK breaks out") and mentions(link, "LINKUSDT funding flips")


def test_filter_and_dedupe():
    now = datetime(2026, 9, 28)
    items = [
        TextItem("k", "google_news", "news", "Nvidia shares jump after record earnings beat", "", "u1", now, 5),
        TextItem("k", "google_news", "news", "Nvidia Shares Jump After Record Earnings Beat!", "", "u2", now, 1),
        TextItem("k", "reddit", "social", "short", "", "u3", now),
        TextItem("k", "reddit", "social", "Different story about Nvidia supply chain", "details", "u1", now),
    ]
    out = filter_and_dedupe(items, min_chars=25, body_chars=600)
    assert [i.url for i in out] == ["u1"]


def test_normalize_score_item_clamps():
    it = normalize_score_item({"id": 1, "relevant": True, "sentiment": 3, "confidence": -1, "stance": "bullish",
                               "horizon": "short", "catalyst_tags": ["earnings", "bogus"], "sarcasm_or_meme": False})
    assert it["sentiment"] == 1.0 and it["confidence"] == 0.0 and it["catalyst_tags"] == ["earnings"]


def _resp(finish_reason, text, refusal=None):
    message = SimpleNamespace(content=text, refusal=refusal)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish_reason)])


def test_llm_parse_guards():
    assert llm._parse(_resp("stop", '{"items": []}'), "x") == {"items": []}
    assert llm._parse(_resp("stop", "{}", refusal="can't help"), "x") is None
    assert llm._parse(_resp("length", '{"items": ['), "x") is None
    assert llm._parse(_resp("stop", "not json"), "x") is None
    assert llm._parse(SimpleNamespace(choices=[]), "x") is None


def test_structured_params_shape():
    p = llm.openrouter_kwargs(llm.structured_params("deepseek/deepseek-v4.1-flash", "off", "sys", "user",
                                                    {"type": "object"}, "t", max_tokens=100))
    assert p["response_format"]["type"] == "json_schema"
    assert p["response_format"]["json_schema"] == {"name": "t", "strict": True, "schema": {"type": "object"}}
    assert p["messages"][0] == {"role": "system", "content": "sys"}
    assert p["extra_body"]["usage"] == {"include": True}
    assert p["max_tokens"] == 100


def test_unverified_numbers():
    grounding = '{"Price": "$225.07", "Change 30d": "+7.5%"} texts: record 2026 guidance'
    narrative = {"headline": "Up +7.5% in 30d at $225.07", "thesis": "Target of $300 and 12% upside in 2026.",
                 "bull_points": [], "bear_points": [], "dominant_narratives": [], "upcoming_catalysts": [],
                 "key_risks": ["Q3 miss"]}
    assert writer.unverified_numbers(narrative, grounding) == ["$300", "12%"]


def test_bluesky_relevance_filter():
    from src_center.ingest.texts import _bluesky_relevant
    link = Asset("LINK", "Chainlink", "crypto")
    assert _bluesky_relevant(link, "$LINK looks strong")
    assert _bluesky_relevant(link, "Chainlink price breaks out")
    assert not _bluesky_relevant(link, "climbed the chainlink fence")
    assert not _bluesky_relevant(link, "click the link below")
    visa = Asset("V", "Visa Inc.", "stock")
    assert _bluesky_relevant(visa, "$V earnings beat")
    assert not _bluesky_relevant(visa, "my visa application got approved")  # stocks need the cashtag


def test_unscored_texts_alternates_news_and_social():
    import pandas as pd
    from src_center.storage import db
    now = utcnow()
    rows = [{"text_id": f"s{i}", "asset_key": "crypto:BTC", "source": "bluesky", "kind": "social", "title": "",
             "body": f"post {i}", "url": f"s{i}", "published_at": now, "engagement": float(100 - i),
             "author_label": None, "fetched_at": now} for i in range(30)]
    rows += [{"text_id": f"n{i}", "asset_key": "crypto:BTC", "source": "google_news", "kind": "news",
              "title": f"news {i}", "body": "", "url": f"n{i}", "published_at": now, "engagement": 0.0,
              "author_label": None, "fetched_at": now} for i in range(3)]
    db.upsert_df("texts", pd.DataFrame(rows))
    picked = db.unscored_texts(["crypto:BTC"], 10)
    assert len(picked) == 10
    assert (picked["kind"] == "news").sum() == 3          # all news kept despite 30 busy social posts
    assert set(picked[picked["kind"] == "social"]["text_id"]) == {f"s{i}" for i in range(7)}  # most engaged


def test_bluesky_spam_pattern():
    from src_center.ingest.texts import SPAM_RE
    assert SPAM_RE.search("I'm a professional Bitcoin investor offering a 5-hour trading opportunity. DM me")
    assert SPAM_RE.search("Join our Telegram group for free signals")
    assert not SPAM_RE.search("Bitcoin ETFs extend winning streak with nearly $3B in inflows")


def test_normalize_enforces_consistency():
    irrelevant = normalize_score_item({"id": 3, "relevant": False, "sentiment": 0.25, "confidence": 0.05,
                                       "stance": "bullish", "horizon": "short", "catalyst_tags": [],
                                       "sarcasm_or_meme": False})
    assert irrelevant["sentiment"] == 0.0 and irrelevant["confidence"] == 0.0 and irrelevant["stance"] == "neutral"
    mismatch = normalize_score_item({"id": 2, "relevant": True, "sentiment": 0.35, "confidence": 0.55,
                                     "stance": "neutral", "horizon": "long", "catalyst_tags": [],
                                     "sarcasm_or_meme": True})
    assert mismatch["stance"] == "bullish"
