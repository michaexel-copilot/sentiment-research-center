# Installation, setup and dashboard

This guide takes you from an empty machine to a daily-updating dashboard. The commands are written for Windows (PowerShell). They work the same on macOS/Linux, apart from the scheduler step at the end.

## 1. Requirements

| Requirement | Notes |
|---|---|
| Windows 10/11 (or macOS/Linux) | Scheduled runs (step 7) use Windows Task Scheduler |
| [uv](https://docs.astral.sh/uv/) | Python package and environment manager. It installs Python 3.12 by itself, so no separate Python install is needed |
| Internet access | All data sources are free web APIs and feeds |
| ~1 GB free disk space | Python environment, Chromium for PDFs, and the database, which grows slowly |
| A Claude subscription (Pro or Max) and [Claude Code](https://claude.com/claude-code) | Needed for LLM scoring and narratives. Log in once with `claude auth login`. Alternatively an [OpenRouter](https://openrouter.ai) account with a few dollars of credit (`llm.provider: openrouter`, about $4–6/month for 30 assets) |

Install uv if you don't have it (any one of these):

```powershell
winget install astral-sh.uv
# or
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Open a **new** terminal afterwards, so that `uv --version` works.

## 2. Install the project

```powershell
cd C:\work\research\sentiment-research-center    # the project folder (copy or clone it here)
uv sync                                          # creates .venv and installs all dependencies
uv run playwright install chromium               # headless browser used to render the PDF one-pagers
uv run pytest -q                                 # optional: all tests should pass
```

`uv sync` also installs the `src` command. Run every command through `uv run src ...` from the project folder.

## 3. API keys (`.env`)

Copy the template and fill in the keys you have:

```powershell
copy .env.example .env
notepad .env
```

| Variable | Needed? | What for | Where to get it |
|---|---|---|---|
| `OPENROUTER_API_KEY` | Only with `llm.provider: openrouter` | LLM text scoring and narratives (DeepSeek). The default provider `claude` uses your logged-in Claude Code instead | [openrouter.ai/keys](https://openrouter.ai/keys). Add credit at [openrouter.ai/settings/credits](https://openrouter.ai/settings/credits); $5 lasts about a month |
| `FINNHUB_API_KEY` | Recommended | Company news for stocks (adds ~60 articles per stock) | Free at [finnhub.io/register](https://finnhub.io/register) |
| `COINGECKO_DEMO_API_KEY` | Recommended | Crypto prices and metadata. Without it, requests are throttled to ~10/min | Free "Demo" key at [coingecko.com/en/developers/dashboard](https://www.coingecko.com/en/developers/dashboard) |
| `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` | Optional | Reddit posts. Access requires Reddit's approval under its Responsible Builder Policy | [reddit.com/prefs/apps](https://www.reddit.com/prefs/apps) once approved (type "script", redirect URI `http://localhost:8080`) |
| `SRC_USER_AGENT` | Optional | Contact string sent to public APIs | Put your own name or email in it |

Sources whose keys are missing switch off automatically. Bluesky, Google News, Yahoo and the crypto RSS feeds need no key.

`.env` is excluded from git (`.gitignore`), so keys never end up in the repository.

**No LLM yet?** Add `--no-llm` to any `run` command. Scoring then uses the offline VADER word-list and a templated summary. That's good for trying the pipeline out, but the signals are unreliable (VADER reads most finance headlines as positive).

## 4. Build the asset lists

```powershell
uv run src universe        # S&P 500, Nasdaq 100, crypto top 100 + market-cap ranking (~1 min)
uv run src watch list      # the observed list: top 10 crypto, top 10 Nasdaq 100, next 10 S&P 500
```

Add or remove extra assets at any time:

```powershell
uv run src watch add PLTR                 # stock
uv run src watch add SUI --class crypto   # coin (use --class when a ticker exists as both)
uv run src watch remove PLTR
```

The list sizes are set in `config/settings.yaml` → `universe.observed`, and extra assets live in `config/watchlist.yaml`.

## 5. First run

```powershell
uv run src estimate-costs        # optional: expected cost if you use OpenRouter instead of the subscription
uv run src run -u observed       # the full pipeline for all observed assets
```

The first run backfills 30 days of texts, about 3,600 texts for 30 assets. It takes about 5–10 minutes (roughly $0.30 on OpenRouter; included in a Claude subscription). Later daily runs are faster and cheaper.

Other ways to run:

```powershell
uv run src run -a NVDA -a BTC                  # any tickers, observed or not
uv run src run -u observed --class crypto      # only the coins (or --class stock)
uv run src run -u observed --skip-ingest       # recompute signals/reports from stored data, no downloads
uv run src run -u observed --no-pdf            # skip PDF rendering
```

Results land in `reports\<date>\<class>_<symbol>\`:
- `onepager.pdf` / `onepager.html`: the one-page summary with the BUY / HOLD / SELL signal.
- `report.md` / `report.html`: the detailed report with every sub-score, the facts, the sources and the most influential texts.

## 6. The dashboard

### Start

```powershell
uv run src dashboard
```

This starts the dashboard at **http://localhost:8501** and opens it in your browser. Stop it with **Ctrl+C** in the same terminal.

| Option | Effect |
|---|---|
| `--port 8502` | Use another port (e.g. if 8501 is taken) |
| `--address 127.0.0.1` | Only reachable from this PC (recommended; see below) |
| `--no-browser` | Don't open a browser tab automatically |

**Network access:** by default the dashboard listens on all network interfaces, so other devices on your network can open it. Its "Analyze ticker" tab starts pipeline runs on your PC. Unless you want access from other devices, start it with `--address 127.0.0.1`.

Run it in its own terminal window and leave that window open. If you start it from a tool that manages background processes (for example, Claude Code), that tool may stop the dashboard when the session ends or memory runs low.

### What's on it

| Tab | Content |
|---|---|
| **Screener** | The latest signal for each asset, with composite score, confidence and all seven sub-scores. Filter by class, signal, universe and minimum confidence |
| **Signal changes** | Assets whose signal differs from their previous run |
| **Asset** | The one-pager of the selected asset, PDF and Markdown downloads, composite-score history, and all scored texts with links |
| **Market** | Market context: CNN and crypto Fear & Greed, VIX, 10Y yield, BTC dominance, stablecoin supply |
| **LLM costs** | LLM tokens and actual OpenRouter spend per day and stage (Claude subscription calls show $0) |
| **Analyze ticker** | Run the pipeline for any tickers from the browser (20–90 s per ticker) |

### Where the data comes from

The dashboard reads a snapshot of the database (`data\src_dashboard.duckdb`). The pipeline publishes it at the end of every run and after `src universe`. On Windows, DuckDB lets no other process open the database while a run writes to it, so the dashboard never touches the live file. This means:

- The dashboard works at any time, including during a run, and it never blocks a run.
- It shows the last *completed* run. The time is shown under the title. Reload the page (F5) after a run to see the new results.
- Right after installation, before the first run has finished, the dashboard has no data and says so.

## 7. Daily runs (Windows Task Scheduler)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1
```

This registers three tasks for the current user:

| Task | When (local time) | Command |
|---|---|---|
| SRC Crypto Daily | daily 02:30 | `src run --universe observed --class crypto` |
| SRC Stocks Daily | Mon–Fri 23:45 | `src run --universe observed --class stock` |
| SRC Universe Weekly | Sunday 12:00 | `src universe` |

- **Times:** the defaults assume CET/CEST (after the crypto daily close and after the US market close). Change them with `-CryptoTime`, `-StocksTime` and `-UniverseDay`.
- **Logs:** go to `data\logs\`.
- **Remove the tasks:** run the script again with `-Remove`.
- **PC off:** the tasks only run while the PC is on. A missed run is simply skipped, and the next run catches up on the texts.

The dashboard is not part of the schedule. Start it whenever you want to look at the results.

## 8. Configuration reference

| File | What you can change |
|---|---|
| `config/settings.yaml` | LLM models and reasoning level (`llm.*`), texts per asset and run (`texts.*`), sources on/off (`sources.*`), crypto RSS feeds, observed-list sizes (`universe.observed`) |
| `config/weights.yaml` | Sub-score weights per asset class, and the BUY/SELL thresholds, gates and caps |
| `config/watchlist.yaml` | Extra observed assets (or use `src watch add/remove`) |
| `.env` | API keys |

With the default provider `claude`, models and effort live under `llm.claude` (e.g. `claude-sonnet-5-5` to use less of the subscription's usage limits). With `llm.provider: openrouter`, any OpenRouter model with structured-output support can replace the DeepSeek defaults (`llm.scoring_model`, `llm.narrative_model`); `uv run src estimate-costs` shows what a switch would cost.

## 9. Maintenance

| Task | Command |
|---|---|
| Update dependencies after changing the code or `pyproject.toml` | `uv sync` |
| Check LLM spend | `uv run src costs` |
| Evaluate signal quality (after a few weeks of runs) | `uv run src evaluate` |
| Start from scratch (deletes all stored data and scores) | Stop the dashboard, delete the `data\` folder, run `uv run src universe` again |

## 10. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Claude Code CLI not found` / `Claude Code is not logged in` | Install Claude Code and run `claude auth login` with your subscription account, or run with `--no-llm` |
| Many `request ... failed` lines with `llm.provider: claude` | The subscription's usage limit is reached. Wait for it to reset, lower `llm.claude.concurrency`, or switch `llm.claude` models to `claude-sonnet-5-5` |
| `OPENROUTER_API_KEY is not set` | Add the key to `.env` (only with `llm.provider: openrouter`), or run with `--no-llm` |
| `failed (402)` in the run log | OpenRouter credit is used up. Top up at openrouter.ai/settings/credits |
| `failed to remove file ... src.exe: Access denied` during `uv sync` | The dashboard or a pipeline run is still using the environment. Stop it (Ctrl+C) and retry |
| `Too Many Requests. Rate limited` for stocks | Yahoo throttles bursts. Runs retry automatically after 15/45/90 s. If stocks still fail, run again later |
| `market caps known for only x%` during `src universe` | Same Yahoo throttling. The previous observed list is kept; retry later |
| CoinGecko `429` errors | Add the free `COINGECKO_DEMO_API_KEY` |
| `stocktwits failed ... 403` | Normal: StockTwits blocks automated access, so the source switches itself off for the run |
| One-pager PDF missing, "PDF rendering failed" | Run `uv run playwright install chromium` |
| Dashboard is empty or shows old results | It shows the last completed run. Reload (F5) after the run finishes. If there's still nothing, run `uv run src run -u observed` once |
| Other `src` commands (`costs`, `evaluate`, `watch list`) fail with "Cannot open file ... used by another process" | A pipeline run is writing to the database. Wait until it finishes |
| Dashboard closes right after starting | Start it with `uv run src dashboard` (this skips Streamlit's first-launch email prompt) and keep the terminal open |
| A few `requests failed` warnings after scoring | Occasional provider errors. Those texts stay unscored and are picked up by the next run |
