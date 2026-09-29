"""User watchlist (config/watchlist.yaml): extra assets observed on top of the top-N lists."""

from __future__ import annotations

import logging

import yaml

from .. import config
from ..models import Asset

log = logging.getLogger(__name__)

SECTIONS = {"stock": "stocks", "crypto": "crypto"}


def _file():
    return config.CONFIG_DIR / "watchlist.yaml"


def load() -> dict[str, list[str]]:
    path = _file()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None
    data = data or {}
    return {sec: [str(s).upper() for s in (data.get(sec) or [])] for sec in SECTIONS.values()}


def _save(data: dict[str, list[str]]) -> None:
    header = ("# Extra assets observed in addition to the top lists (settings.yaml -> universe.observed).\n"
              "# Edit by hand or use:  uv run src watch add PLTR   /   uv run src watch add SUI --class crypto\n")
    body = "".join(f"{sec}: [{', '.join(data.get(sec, []))}]\n" for sec in SECTIONS.values())
    _file().write_text(header + body, encoding="utf-8")


def add(symbol: str, asset_class: str | None = None) -> Asset:
    from .builders import resolve
    asset = resolve(symbol, asset_class)  # validates the ticker (raises ValueError if unknown)
    data = load()
    sec = SECTIONS[asset.asset_class]
    if asset.symbol not in data[sec]:
        data[sec].append(asset.symbol)
        _save(data)
    return asset


def remove(symbol: str, asset_class: str | None = None) -> bool:
    data = load()
    removed = False
    for cls, sec in SECTIONS.items():
        if asset_class and cls != asset_class:
            continue
        if symbol.upper() in data[sec]:
            data[sec].remove(symbol.upper())
            removed = True
    if removed:
        _save(data)
    return removed


def assets() -> list[Asset]:
    from .builders import resolve
    out = []
    for cls, sec in SECTIONS.items():
        for sym in load()[sec]:
            try:
                out.append(resolve(sym, cls))
            except ValueError as exc:
                log.warning("watchlist entry %s skipped: %s", sym, exc)
    return out
