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
        self.scan_exchange = exchange if exchange is not None else public_exchange(settings.exchange)
        self.priority_exchange = exchange if exchange is not None else public_exchange(settings.exchange)
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
            engine=ExecutionEngine(s, rm, broker, Journal(s), alert=self.alert)
            engine.profile=dict(margin_pct=self.settings.entry_margin_pct,
                leverage=self.settings.entry_leverage,max_hold_minutes=self.settings.max_hold_minutes,
                stalled_exit_minutes=self.settings.stalled_exit_minutes)
            engine.profile['apply_holding_to_legacy']=self.settings.legacy_duration_enabled
            engine.profile['experimental_mode']=self.settings.experimental_demo_enabled
            yield s, engine

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
        from analyst.copy_trading import store
        with self.Session() as s:
            store(s,'execution_profile',dict(margin_pct=self.settings.entry_margin_pct,
                leverage=self.settings.entry_leverage,max_hold_minutes=self.settings.max_hold_minutes,
                stalled_exit_minutes=self.settings.stalled_exit_minutes,
                validation_scope='baseline_only' if self.settings.entry_margin_pct else 'baseline',
                active_demo_enabled=self.settings.active_demo_enabled,legacy_duration_enabled=self.settings.legacy_duration_enabled,
                experimental_demo_enabled=self.settings.experimental_demo_enabled,
                ts=self.now_ms()))
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
                from execution.holding import enforce
                if enforce(s,eng,position,self.now_ms()):
                    continue
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
            from analyst.copy_trading import store
            store(s, 'news_status', dict(ts=self.now_ms(), successful_sources=nc.successful_sources,
                                        failed_sources=nc.failed_sources))
            if fear_greed:
                nc.collect_fear_greed(limit=2)
            if calendar:
                nc.collect_calendar()

    def collect_copy_sources(self) -> None:
        from analyst.copy_trading import source_of, store
        from analyst.news_risk import read_state
        from data.copy_sources import collect
        with self.Session() as s:
            previous = read_state(s, 'copy_sources')
            positions = list(s.scalars(select(Position).where(
                Position.status == 'open', Position.strategy == 'copy_hl@v1')))
            tracked = [source_of(s, p).get('address', '') for p in positions]
        result = collect(previous, self.now_ms(), tracked)
        with self.Session() as s:
            store(s, 'copy_sources', result)
            from learning.trader_observations import observe
            observe(s,result,self.now_ms())

    def copy_cycle(self) -> dict:
        from analyst.copy_trading import apply
        with self.trading() as (s, eng):
            return apply(s, eng, self.symbols, self.now_ms(), self.settings.exchange)

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
        from ops.research_report import build as build_report
        with self.lock:
            html=self.page()
            with self.Session() as s:
                heartbeat=int(self._state(s,'heartbeat','0'))
                report=build_report(s)
        return {'mode':'paper','html':html,'heartbeat_ts':heartbeat,'generated_ts':self.now_ms(),
                'report_text':report}

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
        catalog=select_universe(markets,tickers,self.now_ms(),size=None,min_volume=0,min_listing_days=0)
        if not selected:
            raise RuntimeError('No eligible liquid contracts; existing universe retained')
        with self.Session() as s:
            from analyst.copy_trading import store
            store(s,'market_catalog',dict(ts=self.now_ms(),contracts=catalog,
                  scope='Все активные крипто USDT perpetual Binance. Не акции, не стейблкоины и не закрытые контракты.'))
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

    def multiframe_collect(self):
        if not self.settings.multiframe_enabled: return None
        from learning.multiframe import run_batch
        with self.Session() as s:
            return run_batch(s,self.scan_exchange,self.now_ms(),self.settings.multiframe_batch_size)

    def review_strategy_sources(self):
        if not self.settings.multiframe_enabled: return None
        from learning.source_watch import review
        with self.Session() as s:
            return review(s,self.now_ms())

    def priority_research(self):
        if not self.settings.multiframe_enabled:return None
        from learning.multiframe import refresh_priority
        with self.Session() as s:return refresh_priority(s,self.priority_exchange,self.now_ms())

    def signal_learning_cycle(self):
        if not self.settings.multiframe_enabled:return None
        from learning.observatory import cycle
        with self.Session() as s:return cycle(s,self.now_ms())

    def multiframe_execute(self):
        if not self.settings.multiframe_enabled: return None
        if (self.settings.entry_margin_pct,self.settings.entry_leverage,self.settings.max_hold_minutes,
            self.settings.stalled_exit_minutes,self.limits.max_risk_per_trade_pct)!=(10,10,60,30,.5):
            return {'status':'profile_mismatch','reason':'MTF-исследования относятся к профилю 10×/10%, 30/60 мин, риск 0.5%.'}
        from learning.multiframe import pending_signals
        from analyst.copy_trading import store
        from analyst.news_risk import read_state
        from strategies.base import Signal
        from strategies.public_methods import METHODS
        with self.Session() as s:
            candidates=pending_signals(s,self.now_ms())
            if self.settings.experimental_demo_enabled:
                from learning.exploration import pending
                candidates+=pending(s,self.now_ms())
        if not candidates:
            from ops.quality import audit
            with self.Session() as s:
                quality=audit(s,self.now_ms())
                state=read_state(s,'mtf_decisions')
                state.update(ts=self.now_ms(),status='waiting_experimental_signal' if self.settings.experimental_demo_enabled else 'waiting_validation',
                    reason=('Учебное демо включено: ожидается свежий сетап 5м/15м с положительным test на малой выборке. Максимум 1 позиция и 2 входа за UTC-день.' if self.settings.experimental_demo_enabled else
                        f"Нет свежих допущенных сигналов: допуск {quality['qualified_studies']} из {quality['studies']}; недостаточная выборка {quality['insufficient_studies']}; тест не пройден {quality['rejected_studies']}."))
                store(s,'mtf_decisions',state)
            return {'status':state['status'],'reason':state['reason']}
        with self.lock:
            with self.Session() as s:
                base=[x['symbol'] for x in read_state(s,'universe').get('selected',[])] or self.settings.symbol_list
                opened=list(s.scalars(select(Position.symbol).where(Position.status=='open')))
            self.symbols=list(dict.fromkeys(base+opened+[x['symbol'] for x in candidates]))
            self.feed.set_symbols(self.symbols)
            self.feed.refresh()
        with self.trading() as (s,eng):
            state=read_state(s,'mtf_decisions');seen=set(state.get('seen',[]));events=state.get('events',[])
            base_profile=dict(eng.profile)
            for candidate in candidates:
                active=candidate.get('policy')=='active'
                if active and not (self.settings.active_demo_enabled or
                    (candidate.get('experimental') and self.settings.experimental_demo_enabled)):continue
                eng.profile={**base_profile,'active_demo':active,
                    'experimental_entry':candidate.get('experimental',False),
                    'max_hold_minutes':45 if active else 60,'stalled_exit_minutes':20 if active else 30}
                import hashlib
                coid='mtf-'+hashlib.sha256(f"{candidate['symbol']}:{candidate['key']}:{candidate['signal_ts']}".encode()).hexdigest()[:40]
                if coid in seen: continue
                seen.add(coid)
                if not 0<=self.now_ms()-candidate['signal_ts']<=120_000: continue
                source=METHODS[candidate['method']]
                result=eng.submit(candidate['symbol'],Signal(**candidate['signal']),candidate['key'],
                    thesis=source['name']+' · '+candidate['timeframe']+(' · учебная сделка, малая выборка' if candidate.get('experimental') else ' · экспериментальная демосделка'),
                    context=dict(mtf=candidate,source=source),client_order_id=coid)
                events.append(dict(ts=self.now_ms(),symbol=candidate['symbol'],timeframe=candidate['timeframe'],policy=candidate.get('policy','balanced'),
                    method=candidate['method'],status='демовход' if result.ok else 'пропущен',
                    reason='Испытание на будущих данных; доходность не подтверждена.' if result.ok else '; '.join(result.reasons)))
            store(s,'mtf_decisions',dict(ts=self.now_ms(),status='processed',reason='Свежие сигналы обработаны через риск-контроль.',seen=sorted(seen)[-2000:],events=events[-100:]))

    def bootstrap_market(self):
        try:
            self.refresh_universe()
        except Exception:
            log.exception('Universe refresh failed; previous symbols retained')
        self.collect_market()
        self.market_ready=True
        self.research_cycle()
        self.analyst_cycle()

