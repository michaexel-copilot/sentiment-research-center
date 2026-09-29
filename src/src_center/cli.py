"""Command line: `uv run src --help`."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import typer

from . import config

app = typer.Typer(add_completion=False, help="Sentiment Research Center: sentiment-driven asset research pipeline.")


def _setup(verbose: bool, no_llm: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("urllib3", "yfinance", "peewee", "matplotlib", "httpx", "httpx2", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if no_llm:
        os.environ["SRC_LLM_PROVIDER"] = "none"


@app.command()
def universe(which: list[str] = typer.Argument(None, help="sp500 nasdaq100 crypto100 (default: all)")) -> None:
    """Refresh universe membership (S&P 500, Nasdaq 100, crypto top 100)."""
    _setup(False, False)
    from .universe import builders
    counts = builders.refresh(which or None)
    typer.echo(json.dumps(counts))


@app.command()
def run(
    asset: list[str] = typer.Option(None, "--asset", "-a", help="Ticker(s), e.g. -a NVDA -a BTC"),
    universe_name: Optional[str] = typer.Option(
        None, "--universe", "-u", help="observed (top lists + watchlist) | sp500 | nasdaq100 | stocks | crypto100"),
    asset_class: Optional[str] = typer.Option(
        None, "--class", help="stock or crypto: filters a universe / disambiguates a ticker"),
    limit: Optional[int] = typer.Option(None, help="Only the first N assets of the universe"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Offline mode: VADER scoring + template narrative"),
    no_pdf: bool = typer.Option(False, "--no-pdf", help="Skip PDF rendering"),
    skip_ingest: bool = typer.Option(False, "--skip-ingest", help="Recompute signals/reports from stored data"),
    verbose: bool = typer.Option(False, "-v"),
) -> None:
    """Run the full pipeline for single assets or a universe."""
    _setup(verbose, no_llm)
    from . import pipeline
    from .universe import builders
    if asset:
        assets = [builders.resolve(s, asset_class) for s in asset]
        scope = "assets:" + ",".join(a.symbol for a in assets)
    elif universe_name:
        assets = builders.load_assets(universe_name)
        if asset_class:
            assets = [a for a in assets if a.asset_class == asset_class]
        if not assets:
            typer.echo(f"Universe '{universe_name}' is empty. Run `src universe` first.", err=True)
            raise typer.Exit(1)
        assets = assets[:limit] if limit else assets
        scope = f"universe:{universe_name}" + (f":{limit}" if limit else "")
    else:
        typer.echo("Give --asset or --universe.", err=True)
        raise typer.Exit(1)
    report = pipeline.run(assets, scope, single_asset=bool(asset) and len(asset) <= 3, pdf=not no_pdf,
                          skip_ingest=skip_ingest)
    typer.echo(f"\nRun {report.run_id} ({scope}) finished in {report.seconds}s, "
               f"{report.scored_texts} texts scored, LLM cost ${report.cost_usd:.2f}")
    for key, sig in report.signals.items():
        out = report.outputs.get(key, {})
        typer.echo(f"  {key:<16} {sig:<10} {out.get('onepager_pdf') or out.get('onepager_html', '')}")
    for key, err in report.errors.items():
        typer.echo(f"  ! {key}: {err}", err=True)


watch_app = typer.Typer(help="Manage the watchlist (extra observed assets, config/watchlist.yaml).")
app.add_typer(watch_app, name="watch")


@watch_app.command("add")
def watch_add(symbols: list[str], asset_class: Optional[str] = typer.Option(None, "--class")) -> None:
    """Add ticker(s) to the observed list, e.g. `src watch add PLTR` or `src watch add SUI --class crypto`."""
    _setup(False, False)
    from .universe import watchlist
    for sym in symbols:
        try:
            a = watchlist.add(sym, asset_class)
            typer.echo(f"added {a.key} ({a.name})")
        except ValueError as exc:
            typer.echo(f"! {sym}: {exc}", err=True)


@watch_app.command("remove")
def watch_remove(symbols: list[str], asset_class: Optional[str] = typer.Option(None, "--class")) -> None:
    """Remove ticker(s) from the watchlist."""
    from .universe import watchlist
    for sym in symbols:
        typer.echo(f"{'removed' if watchlist.remove(sym, asset_class) else 'not on watchlist'}: {sym.upper()}")


@watch_app.command("list")
def watch_list() -> None:
    """Show the full observed list (top lists + watchlist)."""
    _setup(False, False)
    from .universe import builders
    from .universe.builders import TOP_TAGS
    tags = {v: k for k, v in TOP_TAGS.items()}
    for a in builders.load_assets("observed"):
        src = ", ".join(tags[u] for u in a.universes if u in tags) or "watchlist"
        typer.echo(f"  {a.asset_class:<6} {a.symbol:<7} {a.name[:34]:<34} {src}")


@app.command("estimate-costs")
def estimate_costs(
    universe_name: str = typer.Option("observed", "--universe", "-u"),
    cap: Optional[int] = typer.Option(None, help="Assume this many new texts per asset and run instead of the "
                                                 "measured 7-day rate (the pipeline caps at texts.max_per_asset_per_day)"),
) -> None:
    """Estimate OpenRouter LLM costs for a universe from the real prompt sizes."""
    _setup(False, False)
    from . import costs
    from .universe import builders
    assets = builders.load_assets(universe_name)
    if not assets:
        typer.echo(f"Universe '{universe_name}' is empty. Run `src universe` first.", err=True)
        raise typer.Exit(1)
    lcfg = config.settings()["llm"]
    live = costs.live_prices()
    daily = costs.per_asset_tokens(assets, cap_texts=cap)
    backfill = costs.per_asset_tokens(assets, cap_texts=config.settings()["texts"]["backfill_cap"], clamp=False)
    typer.echo(f"{len(assets)} assets ({(daily['asset'].str.startswith('stock')).sum()} stocks, "
               f"{(daily['asset'].str.startswith('crypto')).sum()} crypto); input tokens estimated at "
               f"~{costs.ASSUMPTIONS['chars_per_token']} chars/token; prices: "
               f"{'live OpenRouter list' if live else 'settings.yaml (OpenRouter list unavailable)'}")
    typer.echo(f"new texts per run: {daily['texts_per_day'].sum():.0f} "
               f"({'assumed ' + str(cap) if cap else 'measured 7-day rate'}, capped at "
               f"{config.settings()['texts']['max_per_asset_per_day']}/asset), "
               f"{daily['score_requests'].sum()} scoring + {len(daily)} narrative requests")
    typer.echo()
    combos = [(lcfg["scoring_model"], lcfg["narrative_model"], "configured")]
    for s_model, n_model in (("deepseek/deepseek-v4.1-flash", "deepseek/deepseek-v4.1-flash"),
                             ("deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-pro"),
                             ("deepseek/deepseek-v3.2", "deepseek/deepseek-v3.2")):
        if (s_model, n_model) != (lcfg["scoring_model"], lcfg["narrative_model"]):
            combos.append((s_model, n_model, ""))
    typer.echo(f"{'scoring / narrative model':<58} {'per run':>14} {'per month':>14} {'one-time backfill':>18}")
    for s_model, n_model, note in combos:
        try:
            run = costs.estimate(daily, s_model, n_model, live=live)["total"]
            month = costs.estimate(daily, s_model, n_model, per="month", live=live)["total"]
            bf = costs.estimate(backfill, s_model, n_model, live=live)["score_cost"]
        except KeyError as exc:
            typer.echo(f"  ! {exc}", err=True)
            continue
        label = f"{s_model.split('/')[-1]} / {n_model.split('/')[-1]}" + (f" ({note})" if note else "")
        typer.echo(f"{label:<58} {f'${run[0]:.3f}-{run[1]:.3f}':>14} {f'${month[0]:.2f}-{month[1]:.2f}':>14} "
                   f"{f'${bf[0]:.3f}-{bf[1]:.3f}':>18}")
    typer.echo()
    typer.echo("Ranges = low/high reasoning-token assumptions. Month = stocks Mon-Fri, crypto daily. "
               "Backfill = first run scoring up to texts.backfill_cap texts per asset.")


@app.command()
def evaluate() -> None:
    """Forward-return evaluation of stored signals (hit rates, excess returns, sub-score IC)."""
    _setup(False, False)
    from . import evaluate as ev
    import pandas as pd
    res = ev.summary()
    if not res:
        typer.echo("No signals stored yet.")
        return
    with pd.option_context("display.width", 200, "display.max_columns", 50):
        for name, df in res.items():
            typer.echo(f"\n== {name} ==\n{df}")


@app.command()
def costs(days: int = 30) -> None:
    """LLM token usage and cost per day and stage."""
    _setup(False, False)
    from .storage import db
    df = db.query_df("""SELECT CAST(ts AS DATE) AS day, stage, model, sum(input_tokens) AS input_tokens,
                        sum(output_tokens) AS output_tokens, round(sum(cost_usd), 3) AS cost_usd
                        FROM llm_usage WHERE ts >= now() - to_days(CAST(? AS INTEGER))
                        GROUP BY ALL ORDER BY day DESC, stage""", [days])
    typer.echo(df.to_string(index=False) if not df.empty else "No LLM usage logged.")


@app.command()
def dashboard(
    port: int = typer.Option(8501, help="Port to serve on"),
    address: Optional[str] = typer.Option(
        None, help="Interface to listen on, e.g. 127.0.0.1 for this PC only (default: all interfaces)"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Don't open a browser tab"),
) -> None:
    """Start the Streamlit dashboard."""
    import time
    import urllib.request
    import webbrowser

    app_path = Path(__file__).parent / "dashboard" / "app.py"
    # Headless skips Streamlit's first-run email prompt (it exits when stdin isn't a terminal);
    # the browser is opened here once the server answers.
    cmd = [sys.executable, "-m", "streamlit", "run", str(app_path), "--server.port", str(port),
           "--server.headless", "true", "--browser.gatherUsageStats", "false"]
    if address:
        cmd += ["--server.address", address]
    proc = subprocess.Popen(cmd, cwd=config.PROJECT_ROOT)
    host = "localhost" if address in (None, "0.0.0.0", "127.0.0.1", "localhost") else address
    url = f"http://{host}:{port}"
    for _ in range(60):
        try:
            urllib.request.urlopen(url, timeout=1)
            typer.echo(f"Dashboard running at {url} (Ctrl+C to stop)")
            if not no_browser:
                webbrowser.open(url)
            break
        except OSError:
            if proc.poll() is not None:
                raise typer.Exit(proc.returncode or 1)
            time.sleep(0.5)
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()


if __name__ == "__main__":
    app()
