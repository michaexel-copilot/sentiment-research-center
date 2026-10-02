"""Render the one-pager (HTML + PDF) and the detailed report (Markdown + HTML) for one asset."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import markdown
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .. import config
from ..models import Asset

log = logging.getLogger(__name__)

TEMPLATES = Path(__file__).parent / "templates"
_env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "j2"]),
                   trim_blocks=False, lstrip_blocks=False)
_md_env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=False)

REPORT_CSS = """
body { font: 15px/1.55 "Segoe UI", "Helvetica Neue", Arial, sans-serif; color: #111827; background: #fff;
       max-width: 900px; margin: 24px auto; padding: 0 16px; }
table { border-collapse: collapse; width: 100%; margin: 8px 0 16px; }
th, td { border-bottom: 1px solid #e5e7eb; padding: 4px 8px; text-align: left; }
td:nth-child(2) { font-variant-numeric: tabular-nums; }
h1 { font-size: 26px; } h2 { margin-top: 28px; border-bottom: 2px solid #111827; padding-bottom: 4px; }
a { color: #1d4ed8; }
@media (prefers-color-scheme: dark) { body { background: #0b0f17; color: #e5e7eb; }
  th, td { border-color: #1f2937; } h2 { border-color: #e5e7eb; } a { color: #93c5fd; } }
"""


def output_dir(asset: Asset, day: str) -> Path:
    d = config.path("reports") / day / f"{asset.asset_class}_{asset.symbol.replace('/', '_')}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def render(asset: Asset, day: str, ctx: dict[str, Any], pdf: bool = True) -> dict[str, Path]:
    """ctx keys: facts, display, signal, narrative, top, history, chart_svg."""
    out = output_dir(asset, day)
    weights = config.weights()["weights"][asset.asset_class]
    common = {"asset": asset, "day": day, "weights": weights, **ctx}

    onepager_html = _env.get_template("onepager.html.j2").render(**common)
    paths = {"onepager_html": out / "onepager.html"}
    paths["onepager_html"].write_text(onepager_html, encoding="utf-8")

    md = _md_env.get_template("report.md.j2").render(**common)
    paths["report_md"] = out / "report.md"
    paths["report_md"].write_text(md, encoding="utf-8")
    body = markdown.markdown(md, extensions=["tables"])
    paths["report_html"] = out / "report.html"
    paths["report_html"].write_text(
        f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{asset.symbol} report {day}</title><style>{REPORT_CSS}</style></head><body>{body}</body></html>",
        encoding="utf-8")

    if pdf:
        try:
            paths["onepager_pdf"] = html_to_pdf(paths["onepager_html"], out / "onepager.pdf")
        except Exception as exc:  # noqa: BLE001 - PDF is optional (browser may be missing)
            log.warning("PDF rendering failed for %s: %s (run `uv run playwright install chromium`)", asset.symbol, exc)
    return paths


_browser = None
_pw = None


def html_to_pdf(html_path: Path, pdf_path: Path) -> Path:
    global _browser, _pw
    from playwright.sync_api import sync_playwright
    if _browser is None:
        _pw = sync_playwright().start()
        _browser = _pw.chromium.launch()
    page = _browser.new_page()
    try:
        page.goto(html_path.resolve().as_uri())
        page.pdf(path=str(pdf_path), format="A4", print_background=True, prefer_css_page_size=True)
    finally:
        page.close()
    return pdf_path


def close_browser() -> None:
    global _browser, _pw
    if _browser is not None:
        _browser.close()
        _pw.stop()
        _browser = _pw = None
