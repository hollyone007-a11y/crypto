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

Risk limits remain global across all coins: 0.5% planned risk per trade, 3 open positions, 10x leverage ceiling (fixed-margin entries target 1x equity notional per position), 2% daily loss limit, 10% drawdown halt, mandatory stops. Gaps, fees and execution uncertainty can exceed planned loss even in simulation.

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

## Quality audit and controlled active demo

Historical validation is cached for up to six hours with its original dataset, cutoff and metrics preserved. Live signals and realized-loss guards refresh separately; new prices never overwrite old validation evidence. This removes full historical replays from every minute of priority research. The dashboard now reports how many studies are insufficient, rejected or admitted, so idle trading is distinguishable from a stopped process. Cache/expiry and loss-guard regressions are covered; current suite: 238 tests.


The dashboard's Laboratory now reports observed sample size, lesson coverage, actual new-profile and active-profile outcomes, data freshness and source failures. Predictive accuracy remains **unknown**; a small number of old-profile paper trades and fixed signal-confidence values cannot establish forecasting skill or profitable adaptation.

A four-contract priority research queue refreshes every 60 seconds, separately from the all-contract history sweep. Completed coins must still pass current volume/listing filters. Failed refresh attempts never certify fresh exchange data, and future-dated study records cannot create candidates. Daily author-document checks remain static-source monitoring; Hyperliquid supplies public fills/positions, not private trader strategies. Historical news and combined-portfolio execution remain outside the OHLC replay.

The optional **active demo** variant covers 5m/15m with targets 25% nearer, stalled-exit checks after 20 minutes and a 45-minute maximum hold. Position notional, leverage and the 0.5% risk budget remain unchanged. Runtime hard caps permit one open active position and two active entries per UTC day; replay applies the daily cap to each isolated run. Global competition among coins/methods is not simulated, so isolated results are not expected portfolio returns. Both balanced and active gates retain 20+ training/test trades, positive net result and t>=3.

All hypotheses now share 14d training, a 1h embargo and 14d test after warm-up; 38 days of continuous history are needed. Collection retains up to 42 days and prunes5m data beyond 45 days. Profit remains unconfirmed. Legacy positions now receive the configured duration cap without being relabelled as 10x entries; winning lessons no longer claim a nonexistent loss. `ACTIVE_DEMO_ENABLED` and `LEGACY_DURATION_ENABLED` are explicit local settings. At release 234 tests passed.

## Live updates and research reports

### Mobile dashboard and all-contract multi-timeframe laboratory

The owner-private dashboard now uses five compact sections: Overview, Positions, Market, Laboratory and Journal. A bottom navigation, one-column cards, 44+ pixel controls, searchable/paged contract list and collapsed evidence details adapt to phone widths. Prices stream only for open positions and up to 20 visible market rows; the complete catalog has its own timestamp. Read-only account snapshots and source failures remain explicit.

`MULTIFRAME_ENABLED=true` enables an independent scanner for **all active crypto USDT perpetuals** returned by Binance (519 at verification); stablecoins, non-crypto and inactive contracts are outside this catalog. Observation does not mean permission to enter: volume >=5m USDT/day, listing age >=30d, adequate fresh history, source validity and existing risk gates remain required. The quote/order writer watches a smaller set plus all open positions, keeping stops responsive. Up to eight contracts are visited each minute with a 40-second work budget; each visit fetches at most two 5m pages and one funding page. Backfill runs progressively. Five-minute candles older than 45 days are pruned; low disk space pauses collection.

Three fixed, sourced research hypotheses—Bollinger continuation, Donchian breakout and Raschke-inspired ADX pullback—are evaluated on 5m, 15m, 1h and 4h. These are disclosed adaptations with own ATR stops/targets and duration exits, not reproductions of private trader systems. A bounded daily source review detects document changes and unavailability; it never downloads executable strategy code or silently changes rules.

The scored calendar is identical for all 18 combinations per coin: 14d training, 1h embargo, then 14d held-out test, preceded by warm-up. All higher timeframes derive from the same continuous 5m history. Funding requires successful endpoint coverage; unavailable history is never assumed zero. Dataset identity includes prices, funding, model assumptions and method/replay implementation; obsolete revisions cannot supply executable candidates. Cross-timeframe summary uses an identical symbol cohort and cutoff, including losing/no-trade methods.

The dedicated replay models fixed 10x/10% margin sizing, costs, stop-first ambiguous candles, executable-side stalled exits, 30/60-minute duration rules and sticky marked-equity breakers **at 5m resolution**. This is an approximate, separate-coin OHLC experiment, not tick-accurate or combined-portfolio validation; historical news is not replayed. It does not unlock baseline production promotions. Experimental paper entries require >=20 train and >=20 test trades, positive net results, t>=3 and drawdown limits, then fresh signals/quotes and the common runtime risk gate. Testing 12 hypotheses creates selection bias; future independent evidence remains necessary. Negative recent realized paper series suspend the specific coin/method/timeframe. No profit or guaranteed trade frequency is implied.

The combined downloadable report includes methods/sources, coverage, scores, rejected profiles, duration exits and actual trade lessons. At verification BTC and ETH completed 24 comparisons with zero qualified new candidates; other contracts continue accumulating history. The Python agent still runs on the laptop, while the private web dashboard is hosted independently. Tests: 225 passed.

### Public traders, news and requested demo sizing

The private dashboard now has a **Traders and news** tab. The read-only Hyperliquid Info API supplies recent fills and positions. A six-hour cached leaderboard screen chooses three wallets with account value >=100k USD, month ROI 1–200%, positive weekly/monthly/lifetime PnL and month volume >=1m USD. This is a historical screen, not verified trader skill: drawdown, account age and survivorship are not established. No private TradingView scraping or Binance leader API access is implied. Official fill documentation: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint

Only newly observed opens <=120 seconds old on exactly matching Binance-universe symbols can enter the local **paper** account. Current source position, fresh hourly history, <=0.5% cross-venue price difference, news freshness and the common risk gate are required. Own stop/take, fees and exits make returns different from the source. Source closure/reversal closes the demo copy; a detected partial reduction closes the whole adapted copy. Copy-only risk is additionally capped at 0.1% equity, with a 24-hour fallback timeout. Skipped signals include reasons. Source failures never reuse stale wallet snapshots.

RSS polling runs every five minutes. Headlines are relevance-filtered, dated and linked; unknown publication times cannot certify freshness. A bounded keyword filter pauses new entries on recent relevant hack/exploit/bankruptcy/delisting/depeg headlines, plus high-impact USD macro-event windows. This is a heuristic with false positives and omissions, not sentiment intelligence or causal price prediction. Positive news does not automatically create a buy.

The requested runtime profile is **10x simulated leverage, 10% equity as margin**: target notional = 100% current equity. Maximum holding is 60 minutes; after 30 minutes the engine exits if estimated net progress is below 0.25R. These conditions are journalled at entry and apply to new positions. A target exceeding the unchanged 0.5% stop-loss risk budget including estimated round-trip costs is rejected, never silently reduced or rescued by tightening the stop. Many signals, especially conservative copy candidates, will not qualify. There is no guarantee of profitable trades. Configure the four `ENTRY_*`/holding settings in `.env.example`; risk.yaml permits leverage up to 10 while daily/drawdown and position-count limits remain.

Baseline historical metrics do not validate this changed sizing, news/copy logic or duration exits. Research and trade reviews continue, but automatic promotion is blocked until matching execution-profile validation is available. The dashboard explicitly distinguishes this evidence scope. Windows Task Scheduler uses `ops.background`, which retains startup diagnostics and holds an exclusive background-writer lock.

Price display uses Binance mark-price WebSocket updates at one-second cadence; these are estimates, not confirmed order fills. The paper engine checks bid/ask on a five-second schedule; minute candles are cached until the minute changes. The private site polls snapshots without navigation, preserving the selected tab and unchanged research content. Stale quotes, old agent heartbeat and failed synchronization are labelled separately.

Research records observations, a hypothesis and its reason, paired historical comparisons, provisional replay of newly received candles, and actual promotion/rollback events. Only recorded rule changes count as applied changes. Two predeclared parameter tests and up to two observation-led tests per parent version bound the search; observation-led tests require at least 20 closed trades. Candidate and benchmark use a frozen dataset, cutoff and identical costs. Missing/stale per-coin history and nonfinite benchmark metrics cannot certify improvement.

Human-readable and JSON reports are saved locally under `agent/reports/research/` (ignored by Git). The latest report is downloadable from the private dashboard. A six-hour minimum separates full research runs; the scheduler checks readiness every ten minutes. No neural-network retraining or autonomous real-money execution is implemented.


## TradingView references and public execution observations

TradingView links are human-view chart navigation only. No TradingView scraping, imported market data, alert/webhook trading integration or automated trader-content collection is enabled. Its standard policies restrict automated collection and non-display use; its Strategy Tester reports simulated trades, not verified author executions. Sources: https://www.tradingview.com/policies/ and https://www.tradingview.com/pine-script-docs/concepts/strategies/ .

The public Hyperliquid feed now records first-seen fills with timestamps, direction, price, quantity, reported closedPnL, fee and feeToken. Overlapping polls are deduplicated. Suspected incomplete windows/poll gaps are counted. A closing fill is not assumed to be a complete trade. The private strategy is unknown; funding, complete account history and mixed fee currencies are not reconciled into a net-account return. The observed leaders are selected by a historical screen, not certified trading skill. Records do not directly change execution rules or relax gates.

A fourth predeclared hypothesis, independent Supertrend(ATR10, multiplier3) plusEMA50 confirmation, expands research to24 balanced/active timeframe variants per coin. Calculations use Binance data. Public mathematical reference: https://www.quantconnect.com/docs/v2/writing-algorithms/indicators/supported-indicators/super-trend . This Python implementation is independently written; no TradingView script/data is downloaded. Own stop, target and time exits are disclosed. New independent future evidence remains required; no profitable outcome is claimed. Tests:245 passed.


## Opt-in experimental paper research

`EXPERIMENTAL_DEMO_ENABLED=true` enables separately labelled forward paper entries from active 5m/15m variants with at least one positive-net test trade. Training can be negative; these variants do not pass strict validation. The existing historical metrics are preserved. Only fresh signals are executable, with mandatory stops and the existing 0.5% risk check, 10% margin and 10x leverage. The entire paper account is capped at one open position and two entries per UTC day while this mode is enabled. Three latest losing-in-total exploratory outcomes pause that symbol/method. This is small-sample research, not evidence of profitability. Defaults remain disabled; no live execution adapter is provided.


## Entry quality and loss cooldown

MTF execution rejects quote movement beyond the smaller of 0.15% of signal price and one quarter of the original stop distance, in either direction. Experimental symbol/strategy pairs wait 24 hours after a loss; the existing three-outcome net-loss pause also remains. Submit rechecks the journal, so cached signals cannot bypass the cooldown. New guard rules have not established profitability and are not included in prior historical results. Stop-gap fills can exceed the planned risk estimate.

Combined train and test net must remain positive for exploratory admission; a tiny positive test cannot rescue an overall negative historical pair. This guard does not establish statistical significance.


## Durable signal research pipeline

The observatory records every detected predeclared-method signal in available closed candles independently of execution qualification. Initial replay is bounded to the previous 24 hours. Unique observation IDs prevent double counting and preserve the original prediction. Late observations are labelled retrospective; prospective decisions are persisted before their planned next-5m-bar entry. Conservative OHLC outcomes include fees, spread, slippage and covered funding. Missing price/funding data never becomes fabricated profit.

A separate 15-second worker resolves pending observations and updates per-method/timeframe/exit empirical net-R estimates from completed forward observations only. Forecasts are frozen before later outcomes, and forecast error is scored afterward. Retrospective outcomes, future observations, and actual paper account orders remain distinct. These unit-notional observations do not change account cash, order limits, risk settings or strategy admission. Cohort statistics retain the latest 10,000 closed observations per current model revision and are descriptive, not evidence of a profitable autonomous model.

Execution filters processed IDs before taking a batch, so previously handled top-ranked candidates cannot permanently hide later fresh signals.
