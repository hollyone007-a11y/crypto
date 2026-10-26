# Crypto · Futures Research

Public Binance market screening and a local **paper-only** futures research agent.

- **Market website:** GitHub Pages serves `docs/` from `main`.
- **Private agent dashboard:** https://futures-agent-observatory.pracovn-pros-6862.chatgpt.site — owner ChatGPT sign-in required.
- `agent/`: Python agent, risk controls, simulated execution, research, tests.
- `cloud/`: read-only snapshot Worker source. Authentication is provided by **private Sites dispatch**; do not deploy it as an unauthenticated public write service.

## What runs where

The public website fetches public market data directly from Binance while it is open. It contains no personal trading history or credentials and does not trade. Its availability does not depend on the laptop.

The Python agent runs on the laptop, starts on Windows sign-in, and sends snapshots to the private hosted dashboard approximately every 15 seconds. The dashboard retains its last snapshot when the laptop is offline and marks stale data. GitHub Pages is not a Python process host. Running the agent independently of the laptop requires a separate server.

## Universe and decisions

Every six hours the agent ranks active Binance USDT linear crypto perpetuals by 24-hour quote volume and selects up to 20. It excludes stablecoins, contracts younger than 30 days, equity and commodity products, and contracts below 50 million USDT turnover. Open positions remain monitored if their coin leaves the selection.

Closed hourly candles, funding, order books and open interest support four research families: Donchian/Turtle-inspired breakout, EMA+ADX, range mean reversion and extreme funding. The breakout is an intraday EMA-filtered adaptation, not the original daily Turtle system. No private trader trades, reputation ranking or profitability claim is reproduced.

New entries require positive validation evidence, at least 20 validation trades **for the coin**, and risk approval. Parameter hypotheses are bounded; changes require historical validation and new-data testing. Candidates, skipped entries, reasons and post-trade observations are recorded. A profitable backtest does not establish future profitability.

Risk limits remain global across all coins: 0.5% planned risk per trade, 3 open positions, 3x aggregate notional cap, 2% daily loss limit, 10% drawdown halt, mandatory stops. Gaps, fees and execution uncertainty can exceed planned loss even in simulation.

## TradingView

The websites link to TradingView charts for **manual viewing only**. No TradingView scraping, webhook ingestion, proprietary Pine scripts or automated copying of community ideas is enabled. See [TradingView terms](https://www.tradingview.com/policies/) and [official webhook requirements](https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/). Any future data/alert integration requires independently confirmed permitted access and account configuration.

## Run locally (Python 3.12)

```powershell
cd agent
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m ops.run
```

Open http://127.0.0.1:8765/. `ops/install_task.ps1` installs the current-user Windows startup task. The sample configuration uses Binance, rules analysis and local paper execution. No exchange account or key is required. `TRADING_MODE=live` is refused; no real-money adapter is implemented.

For private snapshot publishing, configure `CLOUD_DASHBOARD_URL` and the owner-private Sites service credential **locally**. Never put that credential into Pages JavaScript or this repository. SQLite databases, WAL/SHM files, `.env`, logs and local state are excluded.

```powershell
.\.venv\Scripts\python.exe -m pytest
```

The public site can be previewed with `python -m http.server 8080 --directory docs`. Market requests may be unavailable in jurisdictions where Binance restricts access; the UI shows an error instead of invented prices.

## Live updates and research reports

### Public traders, news and requested demo sizing

The private dashboard now has a **Traders and news** tab. The read-only Hyperliquid Info API supplies recent fills and positions. A six-hour cached leaderboard screen chooses three wallets with account value >=100k USD, month ROI 1–200%, positive weekly/monthly/lifetime PnL and month volume >=1m USD. This is a historical screen, not verified trader skill: drawdown, account age and survivorship are not established. No private TradingView scraping or Binance leader API access is implied. Official fill documentation: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint

Only newly observed opens <=120 seconds old on exactly matching Binance-universe symbols can enter the local **paper** account. Current source position, fresh hourly history, <=0.5% cross-venue price difference, news freshness and the common risk gate are required. Own stop/take, fees and exits make returns different from the source. Source closure/reversal closes the demo copy; a detected partial reduction closes the whole adapted copy. Copy-only risk is additionally capped at 0.1% equity, with a 24-hour fallback timeout. Skipped signals include reasons. Source failures never reuse stale wallet snapshots.

RSS polling runs every five minutes. Headlines are relevance-filtered, dated and linked; unknown publication times cannot certify freshness. A bounded keyword filter pauses new entries on recent relevant hack/exploit/bankruptcy/delisting/depeg headlines, plus high-impact USD macro-event windows. This is a heuristic with false positives and omissions, not sentiment intelligence or causal price prediction. Positive news does not automatically create a buy.

The requested runtime profile is **10x simulated leverage, 10% equity as margin**: target notional = 100% current equity. Maximum holding is 60 minutes; after 30 minutes the engine exits if estimated net progress is below 0.25R. These conditions are journalled at entry and apply to new positions. A target exceeding the unchanged 0.5% stop-loss risk budget including estimated round-trip costs is rejected, never silently reduced or rescued by tightening the stop. Many signals, especially conservative copy candidates, will not qualify. There is no guarantee of profitable trades. Configure the four `ENTRY_*`/holding settings in `.env.example`; risk.yaml permits leverage up to 10 while daily/drawdown and position-count limits remain.

Baseline historical metrics do not validate this changed sizing, news/copy logic or duration exits. Research and trade reviews continue, but automatic promotion is blocked until matching execution-profile validation is available. The dashboard explicitly distinguishes this evidence scope. Windows Task Scheduler uses `ops.background`, which retains startup diagnostics and holds an exclusive background-writer lock.

Price display uses Binance mark-price WebSocket updates at one-second cadence; these are estimates, not confirmed order fills. The paper engine checks bid/ask on a five-second schedule; minute candles are cached until the minute changes. The private site polls snapshots without navigation, preserving the selected tab and unchanged research content. Stale quotes, old agent heartbeat and failed synchronization are labelled separately.

Research records observations, a hypothesis and its reason, paired historical comparisons, provisional replay of newly received candles, and actual promotion/rollback events. Only recorded rule changes count as applied changes. Two predeclared parameter tests and up to two observation-led tests per parent version bound the search; observation-led tests require at least 20 closed trades. Candidate and benchmark use a frozen dataset, cutoff and identical costs. Missing/stale per-coin history and nonfinite benchmark metrics cannot certify improvement.

Human-readable and JSON reports are saved locally under `agent/reports/research/` (ignored by Git). The latest report is downloadable from the private dashboard. A six-hour minimum separates full research runs; the scheduler checks readiness every ten minutes. No neural-network retraining or autonomous real-money execution is implemented.
