"""The running agent: wiring + scheduled jobs. Every trading job opens its own
DB session and builds a fresh RiskManager/broker/engine on it; the scheduler
runs jobs one at a time, so paper-account updates never interleave."""
import logging
import json
import threading
import time
from dataclasses import asdict
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import desc, select

from analyst.cycle import run_cycle
from analyst.llm import LLM
from analyst.rules import run_rules
from config.settings import ROOT, Settings, to_ccxt_symbol
from data.market import MarketCollector, public_exchange
from data.news import NewsCollector
from db.models import AgentState, EquitySnapshot, FundingRate, Ohlcv, Position, StrategyVersion
from db.session import init_db, make_engine, make_session_factory
from execution.engine import ExecutionEngine
from execution.paper import PaperBroker
from journal.journal import Journal
from learning import pipeline
from learning.evaluate import evaluate_version, frozen_evaluator
from learning.reviewer import review_pending
from learning.weekly import weekly_review
from ops import dashboard, reports
from ops.cloud import publish
from ops.feed import LiveFeed, StaleQuote
from ops.telegram import Telegram
from risk.limits import load_limits
from risk.manager import KILL_SWITCH, RiskManager
from strategies.registry import seed

log = logging.getLogger("agent")
DAY_MS = 86_400_000


class LiveModeRefused(RuntimeError):
    pass


class App:
    def __init__(self, settings: Settings, exchange=None, telegram: Telegram | None = None,
                 llm_client=None, kill_switch: Path = KILL_SWITCH, risk_file: Path | None = None,
                 clock=time.time):
        if settings.is_live:
            raise LiveModeRefused("TRADING_MODE=live but no live execution adapter exists yet. "
                                  "Refusing to start. Set TRADING_MODE=paper.")
        self.settings = settings
        self.symbols = settings.symbol_list
        self.engine_db = make_engine(settings.database_url)
        init_db(self.engine_db)
        self.Session = make_session_factory(self.engine_db)
        self.limits = load_limits(risk_file) if risk_file else load_limits()
        self.exchange = exchange or public_exchange(settings.exchange)
        self.market_exchange = exchange if exchange is not None else public_exchange(settings.exchange)
        self.feed = LiveFeed(self.exchange, self.symbols, max_age_s=30, clock=clock)
        self.tg = telegram or Telegram(settings.telegram_bot_token.get_secret_value(),
                                       settings.telegram_chat_id)
        self.llm_enabled = bool(settings.anthropic_api_key.get_secret_value()) or llm_client is not None
        self.llm_client = llm_client
        self.kill_switch = Path(kill_switch)
        self.clock = clock
        self.lock = threading.RLock()  # telegram thread vs scheduler: one account writer at a time
        self.research_lock = threading.Lock()
        self.market_ready = not settings.universe_enabled
        with self.Session() as s:
            seed(s)
            row=s.get(AgentState,'universe')
            if settings.universe_enabled and row:
                try:
                    symbols=[item['symbol'] for item in json.loads(row.value).get('selected',[])]
                    symbols+=list(s.scalars(select(Position.symbol).where(Position.status=='open')))
                    if symbols:
                        self.symbols=list(dict.fromkeys(symbols))
                        self.feed.set_symbols(self.symbols)
                except (ValueError,KeyError,TypeError):
                    log.warning('Stored universe invalid; configured fallback retained')

    # ---------- wiring ----------
    def now_ms(self) -> int:
        return int(self.clock() * 1000)

    def alert(self, msg: str) -> None:
        log.warning("ALERT %s", msg)
        self.tg.send(f"[futures-agent] {msg}")

    @contextmanager
    def trading(self):
        with self.lock, self.Session() as s:
            rm = RiskManager(s, self.limits, self.kill_switch, on_alert=self.alert,
                             clock=self.now_ms)
            broker = PaperBroker(s, rm, self.feed.quotes, clock=self.now_ms)
            yield s, ExecutionEngine(s, rm, broker, Journal(s), alert=self.alert)

    def _state(self, s, key, default=""):
        row = s.get(AgentState, key)
        return row.value if row else default

    def _set_state(self, s, key, value):
        row = s.get(AgentState, key)
        if row:
            row.value = str(value)
        else:
            s.add(AgentState(key=key, value=str(value)))
        s.commit()

    def _equity(self, eng) -> float | None:
        try:
            return eng.broker.equity()
        except StaleQuote:
            return None

    # ---------- jobs ----------
    def startup(self) -> None:
        with self.lock:
            self.feed.refresh()
        with self.trading() as (s, eng):
            issues = eng.startup()
        analyst_status = "rules" if self.settings.analyst_mode == "rules" else (
            "on" if self.llm_enabled else "OFF (no ANTHROPIC_API_KEY)")
        self.alert(f"agent started in PAPER mode; exchange {self.settings.exchange}; "
                   f"symbols {', '.join(self.symbols)}; "
                   f"analyst {analyst_status}"
                   + (f"; reconcile: {issues}" if issues else ""))

    def tick(self) -> None:
        """Every minute: quotes, stop/take on closed 1m bars and current price, breakers."""
        try:
            with self.lock:
                self.feed.refresh()
        except Exception as e:
            log.warning("feed refresh failed: %s", e)
            return
        with self.trading() as (s, eng):
            for sym in self.symbols:
                try:
                    last = self.feed.last(sym)
                except StaleQuote:
                    continue
                cursor = int(self._state(s, f"tick:{sym}", "0") or 0)
                for b in self.feed.bars.get(sym, []):
                    if b[0] > cursor:
                        eng.on_price(sym, float(b[2]), float(b[3]), float(b[4]), bar_start_ts=int(b[0]))
                        cursor = int(b[0])
                self._set_state(s, f"tick:{sym}", cursor)
                eng.on_price(sym, last, last, last)
            eng.guard()
            # Match the strategy holding horizon used by the backtester.
            from strategies.registry import build
            for position in eng.broker.open_positions():
                family, separator, version = position.strategy.rpartition('@v')
                if not separator or not version.isdigit():
                    continue
                row = s.scalar(select(StrategyVersion).where(
                    StrategyVersion.family == family, StrategyVersion.version == int(version)))
                if row is None:
                    continue
                strategy = build(row)
                horizon = strategy.max_hold_bars * {'1h':3_600_000,'4h':14_400_000}.get(strategy.timeframe,0)
                if horizon and self.now_ms()-position.opened_ts >= horizon:
                    try:
                        closed = eng.broker.close(position, 'time')
                        eng.journal.record_close(closed)
                    except StaleQuote:
                        continue
            now = self.now_ms()
            if now - int(self._state(s, "equity_snap", "0") or 0) >= 300_000:
                eq = self._equity(eng)
                if eq is not None:
                    s.add(EquitySnapshot(ts=now, equity=eq, cash=eng.broker.account.cash,
                                         open_positions=len(eng.broker.open_positions())))
                    self._set_state(s, "equity_snap", now)
            self._set_state(s, "heartbeat", now)

    def collect_market(self) -> None:
        """Hourly: closed 1h/1d bars, funding, OI; then settle new funding prints."""
        now = self.now_ms()
        with self.Session() as s:
            mc = MarketCollector(self.market_exchange, s, self.settings.exchange)
            for sym in self.symbols:
                try:
                    last = s.scalar(select(Ohlcv.ts).where(Ohlcv.symbol == sym, Ohlcv.timeframe == "1h")
                                    .order_by(desc(Ohlcv.ts)).limit(1))
                    warmup_days = 120 if self.settings.exchange == "binanceusdm" else 30
                    if self.settings.exchange == 'binanceusdm':
                        warmup_days=self.settings.history_days
                    first=s.scalar(select(Ohlcv.ts).where(Ohlcv.symbol==sym,Ohlcv.timeframe=='1h')
                                   .order_by(Ohlcv.ts).limit(1))
                    listed=(getattr(self.market_exchange,'markets',{}) or {}).get(to_ccxt_symbol(sym),{}).get('info',{}).get('onboardDate',0)
                    desired_start=max(now-warmup_days*DAY_MS,int(listed or 0))
                    backfill=first is None or first>desired_start+3_600_000
                    since=desired_start if backfill else (last or desired_start)-3*3_600_000
                    mc.collect_ohlcv(sym, "1h", since)
                    mc.collect_ohlcv(sym, "1d", now - 5 * DAY_MS)
                    mc.collect_funding(sym, now - (90 if self.settings.exchange == "binanceusdm" else 3) * DAY_MS)
                    mc.collect_open_interest(sym, now - 2 * DAY_MS)
                except Exception:
                    s.rollback()
                    log.exception('Market history unavailable for %s; other symbols continue',sym)
        self.settle_funding()

    def settle_funding(self) -> None:
        with self.trading() as (s, eng):
            for sym in self.symbols:
                key = f"funding_applied:{sym}"
                cursor = int(self._state(s, key, "0") or 0)
                if cursor == 0:  # first run: start from now, never back-charge history
                    self._set_state(s, key, self.now_ms())
                    continue
                prints = s.scalars(select(FundingRate).where(FundingRate.symbol == sym,
                                                             FundingRate.ts > cursor)
                                   .order_by(FundingRate.ts)).all()
                for f in prints:
                    try:
                        bid, ask = self.feed.quotes(sym)
                    except StaleQuote:
                        break
                    eng.broker.apply_funding(sym, f.rate, (bid + ask) / 2, at_ts=f.ts)
                    self._set_state(s, key, f.ts)

    def snapshot_books(self) -> None:
        with self.Session() as s:
            mc = MarketCollector(self.market_exchange, s, self.settings.exchange)
            for sym in self.symbols:
                mc.snapshot_orderbook(sym)

    def collect_news(self, fear_greed: bool = False, calendar: bool = False) -> None:
        with self.Session() as s:
            nc = NewsCollector(s)
            nc.collect_rss()
            if fear_greed:
                nc.collect_fear_greed(limit=2)
            if calendar:
                nc.collect_calendar()

    def analyst_cycle(self) -> dict | None:
        if not self.market_ready:
            return {'status':'collecting_market_history'}
        if self.settings.analyst_mode == "rules":
            with self.lock:
                self.feed.refresh()
            with self.trading() as (s, eng):
                if eng.risk.trading_blockers():
                    return {"status": "blocked"}
                return run_rules(s, eng, self.symbols, self.now_ms())
        if not self.llm_enabled:
            return None
        with self.trading() as (s, eng):
            if eng.risk.trading_blockers():
                return {"status": "blocked"}  # don't pay for an LLM call that can't trade
            res = run_cycle(s, eng, LLM(s, self.llm_client), self.symbols, self.now_ms())
        for cid, _ in res.get("submitted", []):
            self.alert(f"opened paper trade {cid}")
        return res

    def review_lessons(self) -> int:
        if self.settings.analyst_mode == 'rules':
            from learning.autonomous import review_rules
            with self.Session() as s:
                return review_rules(s, self.now_ms())
        if not self.llm_enabled:
            return 0
        with self.Session() as s:
            return review_pending(s, LLM(s, self.llm_client))

    def _evaluator(self, s):
        symbols=list(self.symbols)
        return frozen_evaluator(s,symbols,self.now_ms())

    def advance_candidates(self) -> list[str]:
        with self.Session() as s:
            out = pipeline.advance(s, self._evaluator(s), self.now_ms())
        for line in out:
            self.alert(f"learning: {line}")
        return out

    def weekly(self) -> dict | None:
        if not self.llm_enabled:
            return None
        with self.Session() as s:
            res = weekly_review(s, LLM(s, self.llm_client), self._evaluator(s), self.now_ms())
        self.alert("weekly review:\n" + (res["summary"] or "model output invalid, nothing proposed")
                   + "\nnew candidates: " + (", ".join(res["created"]) or "none")
                   + "".join(f"\n- {d}" for d in res["decisions"]))
        return res

    def daily_report(self) -> str:
        with self.trading() as (s, eng):
            text = reports.daily_report(s, self.now_ms(), self._equity(eng),
                                        self.settings.trading_mode, eng.risk.trading_blockers())
        self.tg.send(text)
        return text

    # ---------- telegram + dashboard ----------
    def handle_command(self, cmd: str, args: list[str]) -> str:
        if cmd == "/stop":
            self.kill_switch.write_text(f"stopped via telegram at {self.now_ms()}\n")
            with self.trading() as (s, eng):
                closed = eng.guard()
            return f"Kill switch ON. Flattened {len(closed)} position(s). New trades blocked."
        if cmd == "/resume":
            if self.kill_switch.exists():
                self.kill_switch.unlink()
            return ("Kill switch OFF. Note: a max-drawdown halt can only be cleared on the PC "
                    "with `python -m risk.admin reset-halt`.")
        if cmd in ("/status", "/positions"):
            with self.trading() as (s, eng):
                return reports.status_text(s, self._equity(eng), self.settings.trading_mode,
                                           eng.risk.trading_blockers())
        if cmd == "/report":
            return self.daily_report()
        return "Commands: /status /positions /report /stop /resume"

    def page(self) -> str:
        quotes = {}
        for symbol in self.symbols:
            try:
                quotes[symbol] = self.feed.quotes(symbol)
            except StaleQuote:
                pass
        with self.trading() as (s, eng):
            return dashboard.render(s, self.settings.trading_mode, self._equity(eng),
                                    eng.risk.trading_blockers(),
                                    market_source=f"{self.settings.exchange} / {self.settings.analyst_mode}",
                                    quotes=quotes, limits=asdict(self.limits))

    def publish_dashboard(self) -> bool:
        if not self.settings.cloud_dashboard_url:
            return False
        snapshot=self.live_snapshot()
        return publish(self.settings,snapshot['html'],snapshot['heartbeat_ts'],snapshot['generated_ts'],
                       report_text=snapshot['report_text'])

    def live_snapshot(self):
        from learning.lab import report_text
        from ops.live_view import state_json
        with self.lock:
            html=self.page()
            with self.Session() as s:
                heartbeat=int(self._state(s,'heartbeat','0'))
                report=state_json(s,'research_attempt') or state_json(s,'research_report')
        return {'mode':'paper','html':html,'heartbeat_ts':heartbeat,'generated_ts':self.now_ms(),
                'report_text':report_text(report) if report else 'Исследовательский отчёт ещё не сформирован.'}

    def research_cycle(self):
        if self.settings.analyst_mode != 'rules':
            return None
        if not self.market_ready:
            return {'status':'collecting_market_history'}
        if not self.research_lock.acquire(blocking=False):
            return {'status':'already_running'}
        from learning.autonomous import research
        try:
            with self.Session() as s:
                with self.lock:
                    symbols=list(self.symbols); cutoff=self.now_ms()
                return research(s, frozen_evaluator(s,symbols,cutoff), symbols, cutoff,
                                clock=self.now_ms, exchange=self.settings.exchange,
                                report_dir=ROOT/'reports'/'research')
        finally:
            self.research_lock.release()

    def refresh_universe(self):
        if not self.settings.universe_enabled or self.settings.exchange!='binanceusdm':
            return None
        from data.universe import select_universe
        markets=self.market_exchange.load_markets(reload=True)
        tickers=self.market_exchange.fetch_tickers()
        selected=select_universe(markets,tickers,self.now_ms(),self.settings.universe_size,
                                 self.settings.universe_min_volume)
        if not selected:
            raise RuntimeError('No eligible liquid contracts; existing universe retained')
        with self.Session() as s:
            retained=list(s.scalars(select(Position.symbol).where(Position.status=='open')))
            snapshot={'updated_ts':self.now_ms(),'screened':len(markets),'selected':selected,
                      'min_quote_volume':self.settings.universe_min_volume,'min_listing_days':30}
            old=s.get(AgentState,'universe')
            old_symbols={row['symbol'] for row in json.loads(old.value).get('selected',[])} if old else set()
            if old_symbols!={row['symbol'] for row in selected}:
                self.market_ready=False
                self._set_state(s,'research_last_run','0')
            with self.lock:
                self.symbols=list(dict.fromkeys([row['symbol'] for row in selected]+retained))
                self.feed.set_symbols(self.symbols)
            self._set_state(s,'universe',json.dumps(snapshot))
        return snapshot

    def bootstrap_market(self):
        try:
            self.refresh_universe()
        except Exception:
            log.exception('Universe refresh failed; previous symbols retained')
        self.collect_market()
        self.market_ready=True
        self.research_cycle()
        self.analyst_cycle()

