"""Text sources for sentiment: news (Google News RSS, yfinance, Finnhub, crypto RSS) and social
(Reddit, StockTwits). Shared pools (subreddits, RSS) are fetched once per run and matched per asset."""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any, Callable
from urllib.parse import quote_plus

import feedparser
import pandas as pd

from .. import config, http
from ..models import Asset, TextItem
from ..storage import db
from . import stock_meta
from .textmatch import filter_and_dedupe, mentions

log = logging.getLogger(__name__)


def _utc_naive(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _strip_html(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def _since(asset: Asset) -> datetime:
    """Incremental window: from the newest stored text (minus overlap) or the configured lookback."""
    df = db.query_df("SELECT max(published_at) AS m FROM texts WHERE asset_key = ?", [asset.key])
    lookback = datetime.utcnow() - timedelta(days=config.settings()["texts"]["lookback_days"])
    last = df.iloc[0]["m"]
    if pd.isna(last):
        return lookback
    return max(lookback, pd.Timestamp(last).to_pydatetime() - timedelta(days=2))


# --- per-asset sources ---------------------------------------------------------------------------

def google_news(asset: Asset, since: datetime) -> list[TextItem]:
    days = max(1, min(30, (datetime.utcnow() - since).days + 1))
    q = f'"{asset.short_name}" stock' if asset.asset_class == "stock" else f'"{asset.short_name}" ({asset.symbol}) crypto'
    url = f"https://news.google.com/rss/search?q={quote_plus(q + f' when:{days}d')}&hl=en-US&gl=US&ceid=US:en"
    feed = feedparser.parse(http.get(url, as_json=False))
    items = []
    for e in feed.entries:
        published = datetime(*e.published_parsed[:6]) if getattr(e, "published_parsed", None) else None
        if not published or published < since:
            continue
        source = getattr(getattr(e, "source", None), "title", None) or ""
        title = re.sub(rf"\s+-\s+{re.escape(source)}$", "", e.title) if source else e.title
        # Google News RSS descriptions only repeat the headline and source, so the body stays empty.
        items.append(TextItem(asset.key, "google_news", "news", title, "", e.link, published))
    return items


def yfinance_news(asset: Asset, since: datetime) -> list[TextItem]:
    if asset.asset_class != "stock":
        return []
    return [TextItem(asset.key, "yfinance_news", "news", n["title"], n["body"], n["url"], _utc_naive(n["published_at"]))
            for n in stock_meta.news(asset, since)]


def finnhub_news(asset: Asset, since: datetime) -> list[TextItem]:
    if asset.asset_class != "stock":
        return []
    data = http.get("https://finnhub.io/api/v1/company-news", params={
        "symbol": asset.symbol, "from": since.date().isoformat(), "to": datetime.utcnow().date().isoformat(),
        "token": config.env("FINNHUB_API_KEY")})
    return [TextItem(asset.key, "finnhub", "news", d.get("headline", ""), d.get("summary", ""), d.get("url", ""),
                     datetime.utcfromtimestamp(d["datetime"])) for d in data[:60] if d.get("datetime")]



def stocktwits(asset: Asset, since: datetime) -> list[TextItem]:
    sym = asset.symbol if asset.asset_class == "stock" else f"{asset.symbol}.X"
    data = http.get(f"https://api.stocktwits.com/api/2/streams/symbol/{sym}.json",
                    headers={"User-Agent": "Mozilla/5.0"})
    out = []
    for m in data.get("messages", []):
        published = _utc_naive(pd.Timestamp(m["created_at"]).to_pydatetime())
        if published < since:
            continue
        label = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
        likes = (m.get("likes") or {}).get("total", 0)
        out.append(TextItem(asset.key, "stocktwits", "social", "", m.get("body", ""),
                            f"https://stocktwits.com/message/{m['id']}", published, engagement=float(likes),
                            author_label=label))
    return out


BLUESKY_SEARCH = "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"  # public.api.bsky.app search is 403
CASHTAG_RE = re.compile(r"\$[A-Za-z][A-Za-z0-9.]{0,9}\b")
SPAM_RE = re.compile(
    r"\b(dm me|message me|inbox me|whats\s?app|telegram group|join (my|our) (group|channel)|"
    r"trading opportunity|investment opportunity|guaranteed (profit|returns?)|signals? group|"
    r"account manager|interested in (earning|investing|making)|free (signals|airdrop)|"
    r"claim (your|now)|giveaway)\b", re.IGNORECASE)
CRYPTO_CONTEXT_RE = re.compile(
    r"\$|\b(crypto\w*|coins?|tokens?|blockchain|defi|price|market|etf|bull\w*|bear\w*|pump|dump|hodl|"
    r"bitcoin|btc|eth\w*|altcoin\w*|staking|exchange|wallet|on-?chain|trading|chart|ath)\b",
    re.IGNORECASE)


def _bluesky_relevant(asset: Asset, text: str) -> bool:
    """Bluesky search ignores the '$' ($LINK also finds 'link'), so require the exact cashtag locally.
    Crypto posts may also match on the full coin name (e.g. 'Bitcoin'); stock names are too ambiguous
    ('Apple', 'Visa'), so stocks need the cashtag."""
    if re.search(rf"\${re.escape(asset.symbol)}\b", text, re.IGNORECASE):
        return True
    # Name-only matches need crypto context: "chainlink fence" or "Tron: Legacy" are not about the coin.
    if asset.asset_class != "crypto" or len(asset.short_name) < 4:
        return False
    name_re = re.compile(rf"\b{re.escape(asset.short_name)}\b", re.IGNORECASE)
    return name_re.search(text) is not None and CRYPTO_CONTEXT_RE.search(name_re.sub("", text)) is not None


def bluesky(asset: Asset, since: datetime) -> list[TextItem]:
    """Bluesky posts: the most-engaged plus the newest ones in the window, per query."""
    queries = [f"${asset.symbol}"]
    if asset.asset_class == "crypto" and asset.short_name.upper() != asset.symbol:
        queries.append(asset.short_name)
    posts: dict[str, dict[str, Any]] = {}
    for q in queries:
        for sort in ("top", "latest"):
            data = http.get(BLUESKY_SEARCH, params={"q": q, "sort": sort, "limit": 100, "lang": "en",
                                                   "since": since.strftime("%Y-%m-%dT%H:%M:%SZ")})
            for p in data.get("posts", []):
                posts[p["uri"]] = p
    out = []
    for p in posts.values():
        text = (p.get("record") or {}).get("text", "")
        if not _bluesky_relevant(asset, text) or len(CASHTAG_RE.findall(text)) > 5 or SPAM_RE.search(text):
            continue  # off-topic, cashtag-stuffed, or a signals/"investment opportunity" promo
        published = _utc_naive(pd.Timestamp(p["record"]["createdAt"]).floor("s").to_pydatetime())
        if published < since:
            continue
        handle = (p.get("author") or {}).get("handle", "")
        engagement = (p.get("likeCount", 0) + 2 * p.get("repostCount", 0) + p.get("replyCount", 0)
                      + p.get("quoteCount", 0))
        url = f"https://bsky.app/profile/{handle}/post/{p['uri'].rsplit('/', 1)[-1]}"
        out.append(TextItem(asset.key, "bluesky", "social", "", text, url, published, engagement=float(engagement)))
    return out


_reddit_token: dict[str, Any] = {}
_reddit_lock = Lock()


def reddit_get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    """Reddit API via application-only OAuth (free 'script' app; unauthenticated JSON is blocked)."""
    with _reddit_lock:
        if not _reddit_token or _reddit_token["expires"] < datetime.utcnow():
            resp = http.session().post(
                "https://www.reddit.com/api/v1/access_token", data={"grant_type": "client_credentials"},
                auth=(config.env("REDDIT_CLIENT_ID"), config.env("REDDIT_CLIENT_SECRET")), timeout=20)
            if resp.status_code != 200:
                raise http.SourceUnavailable(f"reddit token request failed: {resp.status_code} {resp.text[:120]}")
            tok = resp.json()
            _reddit_token.update(token=tok["access_token"],
                                 expires=datetime.utcnow() + timedelta(seconds=int(tok.get("expires_in", 3600)) - 60))
        token = _reddit_token["token"]
    return http.get(f"https://oauth.reddit.com{path}", params={**params, "raw_json": 1},
                    headers={"Authorization": f"bearer {token}"})


def reddit_search(asset: Asset, since: datetime) -> list[TextItem]:
    q = f'"{asset.short_name}" OR "${asset.symbol}"'
    data = reddit_get("/search", {"q": q, "sort": "new", "t": "month", "limit": 100, "type": "link"})
    return [it for it in _reddit_items(data, asset.key, since) if mentions(asset, f"{it.title} {it.body}")]


# --- shared pools --------------------------------------------------------------------------------

def _reddit_items(listing: dict[str, Any], asset_key: str, since: datetime) -> list[TextItem]:
    out = []
    for child in listing.get("data", {}).get("children", []):
        d = child.get("data", {})
        published = datetime.utcfromtimestamp(d.get("created_utc", 0))
        if published < since:
            continue
        out.append(TextItem(asset_key, "reddit", "social", d.get("title", ""), d.get("selftext", "")[:2000],
                            "https://www.reddit.com" + d.get("permalink", ""), published,
                            engagement=float(d.get("score", 0) + d.get("num_comments", 0))))
    return out


class Pool:
    """Run-scoped cache of subreddit listings and RSS feeds, matched against each asset."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._items: dict[str, list[TextItem]] = {}

    def _get(self, key: str, loader: Callable[[], list[TextItem]]) -> list[TextItem]:
        with self._lock:
            if key not in self._items:
                try:
                    self._items[key] = loader()
                except Exception as exc:  # noqa: BLE001 - a dead pool source must not fail assets
                    log.warning("pool source %s unavailable: %s", key, exc)
                    self._items[key] = []
            return self._items[key]

    def reddit(self, asset_class: str) -> list[TextItem]:
        subs = config.settings()["reddit"]["stock_subs" if asset_class == "stock" else "crypto_subs"]

        def load() -> list[TextItem]:
            items: list[TextItem] = []
            since = datetime.utcnow() - timedelta(days=config.settings()["texts"]["lookback_days"])
            for sub in subs:
                for listing in ("new", "hot", "top"):
                    params = {"limit": 100, **({"t": "week"} if listing == "top" else {})}
                    data = reddit_get(f"/r/{sub}/{listing}", params)
                    items.extend(_reddit_items(data, "", since))
            return items

        return self._get(f"reddit:{asset_class}", load)

    def subreddit(self, sub: str) -> list[TextItem]:
        def load() -> list[TextItem]:
            since = datetime.utcnow() - timedelta(days=config.settings()["texts"]["lookback_days"])
            items = []
            for listing in ("hot", "top"):
                data = reddit_get(f"/r/{sub}/{listing}",
                                  {"limit": 50, **({"t": "week"} if listing == "top" else {})})
                items.extend(_reddit_items(data, "", since))
            return items
        return self._get(f"sub:{sub.lower()}", load)

    def crypto_rss(self) -> list[TextItem]:
        def load() -> list[TextItem]:
            items = []
            for url in config.settings()["crypto_rss_feeds"]:
                try:
                    feed = feedparser.parse(http.get(url, as_json=False, ttl=1800))
                except http.SourceUnavailable as exc:
                    log.info("rss %s unavailable: %s", url, exc)
                    continue
                for e in feed.entries:
                    ts = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
                    if not ts:
                        continue
                    items.append(TextItem("", "crypto_rss", "news", e.get("title", ""),
                                          _strip_html(e.get("summary", ""))[:1500], e.get("link", ""), datetime(*ts[:6])))
            return items
        return self._get("crypto_rss", load)


def _rebind(items: list[TextItem], asset: Asset, since: datetime, require_mention: bool = True) -> list[TextItem]:
    out = []
    for it in items:
        if it.published_at < since:
            continue
        if require_mention and not mentions(asset, f"{it.title} {it.body}"):
            continue
        out.append(TextItem(asset.key, it.source, it.kind, it.title, it.body, it.url, it.published_at,
                            it.engagement, it.author_label))
    return out


# --- orchestration -------------------------------------------------------------------------------

def collect(asset: Asset, pool: Pool, single_asset: bool = False, crypto_subreddit: str | None = None) -> dict[str, int]:
    """Fetch all enabled text sources for one asset, clean them, and store new items. Returns counts per source."""
    since = _since(asset)
    per_asset: list[tuple[str, Callable[[Asset, datetime], list[TextItem]]]] = [
        ("google_news", google_news), ("yfinance_news", yfinance_news), ("bluesky", bluesky), ("stocktwits", stocktwits),
        ("finnhub", finnhub_news), ("reddit_per_asset_search", reddit_search),
    ]
    items: list[TextItem] = []
    counts: dict[str, int] = {}
    for name, fn in per_asset:
        if not config.source_enabled(name, single_asset):
            continue
        try:
            got = fn(asset, since)
        except (http.SourceUnavailable, Exception) as exc:  # noqa: BLE001 - one source must not fail the asset
            log.info("%s failed for %s: %s", name, asset.symbol, exc)
            got = []
        counts[name] = len(got)
        items.extend(got)
    if config.source_enabled("reddit_pool"):
        got = _rebind(pool.reddit(asset.asset_class), asset, since)
        if crypto_subreddit:
            got += _rebind(pool.subreddit(crypto_subreddit), asset, since, require_mention=False)
        counts["reddit"] = len(got)
        items.extend(got)
    if asset.asset_class == "crypto" and config.source_enabled("crypto_rss"):
        got = _rebind(pool.crypto_rss(), asset, since)
        counts["crypto_rss"] = len(got)
        items.extend(got)

    tcfg = config.settings()["texts"]
    clean = filter_and_dedupe(items, tcfg["min_chars"], tcfg["body_chars"])
    now = datetime.utcnow()
    df = pd.DataFrame([{
        "text_id": it.text_id, "asset_key": asset.key, "source": it.source, "kind": it.kind, "title": it.title,
        "body": it.body, "url": it.url, "published_at": it.published_at, "engagement": it.engagement,
        "author_label": it.author_label, "fetched_at": now,
    } for it in clean])
    if not df.empty:
        df = df.drop_duplicates(subset=["text_id"])
        db.upsert_df("texts", df)
    counts["stored"] = len(df)
    return counts
