"""Settings and weights loading. Config files live in <project>/config, secrets in env / .env."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(os.environ.get("SRC_HOME", Path(__file__).resolve().parents[2]))
CONFIG_DIR = PROJECT_ROOT / "config"


def _load_dotenv() -> None:
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if value and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


@lru_cache
def settings() -> dict[str, Any]:
    return yaml.safe_load((CONFIG_DIR / "settings.yaml").read_text(encoding="utf-8"))


@lru_cache
def weights() -> dict[str, Any]:
    return yaml.safe_load((CONFIG_DIR / "weights.yaml").read_text(encoding="utf-8"))


def path(key: str) -> Path:
    p = PROJECT_ROOT / settings()["paths"][key]
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def env(key: str) -> str | None:
    return os.environ.get(key) or None


def user_agent() -> str:
    return env("SRC_USER_AGENT") or "sentiment-research-center/0.1 (research)"


def source_enabled(name: str, single_asset: bool = False) -> bool:
    value = settings()["sources"].get(name, False)
    if value == "auto":
        if name == "finnhub":
            return env("FINNHUB_API_KEY") is not None
        reddit_keys = env("REDDIT_CLIENT_ID") is not None and env("REDDIT_CLIENT_SECRET") is not None
        if name == "reddit_pool":
            return reddit_keys
        if name == "reddit_per_asset_search":
            return single_asset and reddit_keys
        return False
    return bool(value)


def llm_provider() -> str:
    provider = settings()["llm"]["provider"]
    if os.environ.get("SRC_LLM_PROVIDER"):
        provider = os.environ["SRC_LLM_PROVIDER"]
    return provider
