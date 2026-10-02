# Sentiment Research Center

A pipeline for sentiment-driven research on any stock or crypto asset. For each asset it collects market, positioning and fundamental data plus news and social texts. An LLM (Claude via your Claude subscription by default, or any model on OpenRouter) scores every text for sentiment. The pipeline then combines everything into sub-scores and a **BUY / HOLD (don't buy) / SELL** signal, and renders a one-page PDF, a detailed report and a dashboard.

The pipeline watches an **observed list of 30 assets**, plus anything you add yourself:
- **Crypto:** the top 10 by market cap. Stablecoins, wrapped/staked tokens and tokenized treasuries are filtered out.
- **Nasdaq 100:** the 10 largest by market cap, counting share classes such as GOOG/GOOGL once.
- **S&P 500:** the 10 largest members that are not already in the Nasdaq top 10.

The sizes are set in `settings.yaml` under `universe.observed`. With `distinct_stocks: false`, the S&P top 10 may overlap the Nasdaq top 10. The full index lists (S&P 500, Nasdaq 100, crypto top 100) stay available through `-u sp500`, `-u nasdaq100` and `-u crypto100`.

## Quick start

For the full installation and setup guide (requirements, API keys, first run, dashboard, scheduling, troubleshooting), see **[docs/SETUP.md](docs/SETUP.md)**. The server deployment on Proxmox (container 108), and the commit → deploy → push workflow, are described in **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**.

```powershell
uv sync
uv run playwright install chromium        # for PDF one-pagers
claude auth login                         # once: log Claude Code in with your Claude subscription
copy .env.example .env                    # optional free data-source keys
uv run src universe                       # build index lists + rank by market cap -> observed top 10s
uv run src watch list                     # the 30 observed assets (+ watchlist)
uv run src watch add PLTR                 # add assets; `--class crypto` for coins, `watch remove X`
uv run src run -u observed                # daily run of the observed list
uv run src run -a NVDA -a BTC             # ad-hoc analysis of any ticker
uv run src estimate-costs                 # OpenRouter cost estimate (only relevant with llm.provider: openrouter)
uv run src dashboard --address 127.0.0.1 # http://localhost:8501 (this PC only)
```

Without an LLM, add `--no-llm`. This uses VADER lexicon scoring and a templated narrative, which is fine for testing but much weaker on finance and crypto language.

## Pipeline

```
universe → ingest (parallel, per-asset isolation) → dedupe/cap → LLM scoring → features → signal → LLM narrative → MD/HTML/PDF → dashboard
```

| Stage | Module |
|---|---|
| Universes | `universe/builders.py` (Wikipedia, CoinGecko + stable-value filter) |
| Prices | `ingest/prices.py` (yfinance; Binance klines → CoinGecko fallback) |
| Stock meta | `ingest/stock_meta.py`: fundamentals, short interest, options put/call + IV, insiders, analyst drift, earnings |
| Crypto meta | `ingest/crypto_meta.py`: CoinGecko (FDV/MC, dev activity), DefiLlama TVL, Binance perp funding / OI / long-short |
| Market context | `ingest/context.py`: CNN & crypto Fear/Greed, VIX, 10Y, SPY trend, BTC dominance, stablecoin supply |
| Texts | `ingest/texts.py`: Google News RSS, yfinance news, crypto RSS, Bluesky, Reddit (OAuth), StockTwits, Finnhub |
| Scoring | `nlp/scorer.py` + `nlp/prompts/score_v1.md`: structured output per text (relevance, sentiment, confidence, stance, horizon, catalysts, sarcasm) |
| Features | `features/technical.py`, `features/sentiment.py`: level, momentum, z-score vs own 90d, attention |
| Signal | `scoring/subscores.py`, `scoring/signal.py`, weights & thresholds in `config/weights.yaml` |
| Narrative | `narrative/writer.py`: the LLM explains the computed signal; numbers are checked against the fact sheet |
| Output | `reports/render.py` → `reports/<date>/<class>_<symbol>/{onepager.pdf,onepager.html,report.md,report.html}` |

## Signal logic (v1)

There are seven sub-scores on a 0–100 scale (50 = neutral), weighted per asset class:
- sentiment level
- sentiment momentum
- attention
- positioning (contrarian)
- trend
- fundamentals
- macro

**Rules**
- **BUY:** composite ≥ 65, and the price is above its SMA200 (or sentiment momentum is strong).
- **SELL:** composite ≤ 35, or a bearish divergence (price near its high while sentiment fades and positioning is crowded).
- **HOLD:** everything else.

**Gates and caps**
- **NO SIGNAL:** too few relevant texts, or stale prices.
- **Euphoria cap:** extreme sentiment plus crowded positioning caps the signal at HOLD.
- **Earnings warning:** when earnings fall within 3 days, the one-pager flags it.

Every threshold lives in `config/weights.yaml`.

## LLM usage and cost

- **Provider:** set as `llm.provider` in `config/settings.yaml`.
  - `claude` (default): your Claude subscription. Each request runs the [Claude Code](https://claude.com/claude-code) CLI headless (`claude -p` with a JSON schema, no tools, no project settings). Install the CLI and log in once with `claude auth login`; no API key is needed and calls cost nothing extra, but they count against the subscription's usage limits. `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` are removed from the CLI's environment so it never bills an API account instead.
  - Models and effort are under `llm.claude` (default `claude-opus-5-5`, effort `low` for scoring and `medium` for narratives, 4 parallel requests). Use `claude-sonnet-5-5` or `claude-haiku-4-5` to stretch the usage limits.
  - `openrouter`: [OpenRouter](https://openrouter.ai), needs `OPENROUTER_API_KEY` in `.env`. The rest of this section describes this provider.
- **OpenRouter models:**
  - Scoring uses `deepseek/deepseek-v4.1-flash` with reasoning off. It is a high-volume classification task.
  - Narratives use `deepseek/deepseek-v4-pro` with low reasoning, one call per asset.
  - Any OpenRouter model with structured-output support can replace either one.
- **Requests:**
  - Each scoring request carries ~20 texts and asks for strict JSON-schema output.
  - Requests are only routed to OpenRouter providers that support JSON-schema output (`require_parameters`).
  - Requests run in parallel (`llm.concurrency`). OpenRouter has no batch discount.
- **Estimated cost for the 30 observed assets** (from `uv run src estimate-costs`; live OpenRouter prices; stocks Mon–Fri, crypto daily):

  | Scoring / narrative model | Per daily run | Per month | At 40 texts/asset/day |
  |---|---|---|---|
  | v4.1-flash / v4-pro (default) | $0.14–0.24 | ~$3.30–5.80 | ~$3.70–6.20 |
  | v4.1-flash / v4.1-flash | $0.02–0.04 | ~$0.60–1.00 | ~$0.90–1.40 |
  | v4-pro / v4-pro | $0.27–0.39 | ~$6.60–9.50 | ~$10–13 |
  | v3.2 / v3.2 | $0.07–0.10 | ~$1.70–2.40 | ~$2.60–3.30 |

  The first run's backfill adds under $0.10 with the default models.
- **Actual spend:** OpenRouter reports the billed cost of every call (Claude subscription calls are logged with their tokens at $0). It is logged, and you can see it with `uv run src costs` or on the dashboard's LLM cost tab.

## Scheduling

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1
```
This registers three tasks:
- crypto, daily at 02:30 local
- stocks, Mon–Fri at 23:45 local
- universe refresh, weekly

Logs go to `data/logs/`.

## Validation

`uv run src evaluate` reports, per signal:
- 7/30/90-day forward returns vs SPY or BTC
- hit rates
- each sub-score's information coefficient

LLM sentiment has no free history, so plan on 8–12 weeks of paper tracking before tuning `weights.yaml`.

## Data source notes

- **Bluesky** is the main social source. It uses the public search API (`api.bsky.app`) and needs no key.
  - The search ignores `$` (`$LINK` also finds "link"), so posts must contain the exact cashtag. A coin's name also counts, but only alongside a crypto word nearby.
  - Posts with more than 5 cashtags, and scam or signals promos, are dropped.
  - News and social posts share each asset's daily LLM budget evenly.
- **Reddit:** API access now requires Reddit's approval (Responsible Builder Policy). Once approved, set `REDDIT_CLIENT_ID`/`REDDIT_CLIENT_SECRET` and the source switches on automatically.
- **StockTwits:** the public API often sits behind Cloudflare (403), so it is used best-effort.
- **Binance futures** (funding, OI) are geo-restricted in some regions. Positioning then relies on what is available.
- **Finnhub** (stock news) switches on automatically when its free key is set.
- **Crypto news RSS:** CoinDesk, Cointelegraph, Decrypt, Bitcoin Magazine, The Block, The Defiant, CryptoSlate, BeInCrypto and CryptoPotato. Feeds are listed in `settings.yaml` under `crypto_rss_feeds`, are fetched once per run, and are matched to each coin. CryptoPanic was dropped because it no longer offers free API access.

Automated research output, not financial advice.
