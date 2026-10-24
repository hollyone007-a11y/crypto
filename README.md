# Crypto · Futures Research

Public Binance market screening and a local **paper-only** futures research agent.

- **Market website:** GitHub Pages serves `docs/` from `main`.
- **Private agent dashboard:** https://futures-agent-observatory.pracovn-pros-6862.chatgpt.site — owner ChatGPT sign-in required.
- `agent/`: Python agent, risk controls, simulated execution, research, tests.
- `cloud/`: read-only snapshot Worker source. Authentication is provided by **private Sites dispatch**; do not deploy it as an unauthenticated public write service.

## What runs where

The public website fetches public market data directly from Binance while it is open. It contains no personal trading history or credentials and does not trade. Its availability does not depend on the laptop.

The Python agent runs on the laptop, starts on Windows sign-in, and sends snapshots to the private hosted dashboard every minute. The dashboard retains its last snapshot when the laptop is offline and marks stale data. GitHub Pages is not a Python process host. Running the agent independently of the laptop requires a separate server.

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

