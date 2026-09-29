"""Shared HTTP layer: one session, retries, per-host rate limiting, TTL file cache, circuit breaker."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import config

log = logging.getLogger(__name__)

# Minimum seconds between requests per host (free-tier friendly).
HOST_INTERVALS = {
    # keyless CoinGecko allows ~10 calls/min; a free demo key (COINGECKO_DEMO_API_KEY) allows 30/min
    "api.coingecko.com": 2.2 if config.env("COINGECKO_DEMO_API_KEY") else 6.5,
    "oauth.reddit.com": 0.7,
    "api.bsky.app": 0.5,
    "news.google.com": 1.0,
    "api.stocktwits.com": 1.5,
    "finnhub.io": 1.1,
    "en.wikipedia.org": 0.5,
}

_session: requests.Session | None = None
_host_locks: dict[str, threading.Lock] = {}
_host_last: dict[str, float] = {}
_failures: dict[str, int] = {}
_global_lock = threading.Lock()
BREAKER_THRESHOLD = 4


class SourceUnavailable(RuntimeError):
    pass


def session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        retry = Retry(total=3, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=("GET",), respect_retry_after_header=True)
        s.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=16))
        s.mount("http://", HTTPAdapter(max_retries=retry, pool_maxsize=16))
        s.headers.update({"User-Agent": config.user_agent()})
        _session = s
    return _session


def _throttle(host: str) -> None:
    interval = HOST_INTERVALS.get(host, 0.2)
    with _global_lock:
        lock = _host_locks.setdefault(host, threading.Lock())
    with lock:
        wait = _host_last.get(host, 0) + interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _host_last[host] = time.monotonic()


def _cache_file(url: str, params: dict | None) -> Path:
    key = hashlib.sha1((url + json.dumps(params or {}, sort_keys=True)).encode()).hexdigest()
    d = config.path("cache") / "http"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.json"


def get(url: str, params: dict | None = None, headers: dict | None = None, ttl: float = 0,
        as_json: bool = True, timeout: float = 20) -> Any:
    """GET with throttling and optional TTL cache (seconds). Raises SourceUnavailable when the host's breaker is open."""
    host = urlparse(url).netloc
    if _failures.get(host, 0) >= BREAKER_THRESHOLD:
        raise SourceUnavailable(f"{host} disabled for this run after repeated failures")

    cache_path = _cache_file(url, params) if ttl else None
    if cache_path and cache_path.exists() and time.time() - cache_path.stat().st_mtime < ttl:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        return payload["data"]

    _throttle(host)
    try:
        resp = session().get(url, params=params, headers=headers, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as exc:
        _failures[host] = _failures.get(host, 0) + 1
        raise SourceUnavailable(f"GET {url} failed: {exc}") from exc
    if as_json:
        try:
            data = resp.json()
        except ValueError as exc:  # HTML block/captcha pages served with status 200
            _failures[host] = _failures.get(host, 0) + 1
            raise SourceUnavailable(f"GET {url} returned non-JSON ({resp.headers.get('content-type')})") from exc
    else:
        data = resp.text
    _failures[host] = 0
    if cache_path:
        cache_path.write_text(json.dumps({"url": url, "data": data}), encoding="utf-8")
    return data


def reset_breakers() -> None:
    _failures.clear()
