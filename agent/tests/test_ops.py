import logging
import shutil

import httpx
import pytest
from sqlalchemy import select

from config.settings import Settings
from db.models import (AgentState, EquitySnapshot, FundingRate, JournalEntry, Ohlcv, Position,
                       RiskEvent)
from ops import dashboard, reports
from ops.app import App, LiveModeRefused
from ops.feed import LiveFeed, StaleQuote
from ops.run import build_scheduler
from ops.telegram import Telegram
from risk.limits import RISK_FILE
from strategies.base import Signal
from tests.fakes import FakeClient

T0 = 1_792_454_400.0  # seconds
M = 60_000


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class FakeEx:
    """Public-exchange double: ticker + 1m bars + the collector calls."""
    def __init__(self, clock):
        self.clock = clock
        self.px = {"BTC/USDT:USDT": 100.0, "ETH/USDT:USDT": 50.0}
        self.bars = {k: [] for k in self.px}

    def fetch_ticker(self, m):
        p = self.px[m]
        return {"bid": p - 0.05, "ask": p + 0.05, "last": p}

    def fetch_ohlcv(self, m, tf, since=None, limit=100):
        if tf == "1m":
            return self.bars[m][-limit:]
        return []

    def fetch_funding_rate_history(self, m, since=None, limit=200):
        return []

    def fetch_open_interest_history(self, m, tf, since=None, limit=200):
        return []

    def fetch_order_book(self, m, limit=50):
        p = self.px[m]
        return {"timestamp": int(self.clock() * 1000), "bids": [[p - 0.05, 1]], "asks": [[p + 0.05, 1]]}


class TgSpy(Telegram):
    def __init__(self):
        super().__init__("", "")
        self.sent = []

    def send(self, text):
        self.sent.append(text)
        return True


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADING_MODE", raising=False)
    cfg = tmp_path / "risk.yaml"
    shutil.copy(RISK_FILE, cfg)
    clock = Clock()
    s = Settings(_env_file=None, database_url=f"sqlite:///{(tmp_path / 'a.db').as_posix()}",
                 symbols="BTCUSDT,ETHUSDT")
    a = App(s, exchange=FakeEx(clock), telegram=TgSpy(), kill_switch=tmp_path / "STOP",
            risk_file=cfg, clock=clock)
    a.feed.refresh()
    return a


def open_long(app, sym="BTCUSDT", stop=98.0, take=106.0):
    with app.trading() as (s, eng):
        r = eng.submit(sym, Signal("long", 100.0, stop, take, 0.6, "t"), "trend_donchian@v1")
        assert r.ok, r.reasons
        return r.position_id


def test_live_mode_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "live")
    with pytest.raises(LiveModeRefused):
        App(Settings(_env_file=None, database_url="sqlite://"), exchange=FakeEx(Clock()))


def test_feed_stale_quotes():
    clock = Clock()
    f = LiveFeed(FakeEx(clock), ["BTCUSDT"], clock=clock)
    with pytest.raises(StaleQuote):
        f.quotes("BTCUSDT")
    f.refresh()
    assert f.quotes("BTCUSDT") == (99.95, 100.05) and f.last("BTCUSDT") == 100.0
    clock.t += 181
    with pytest.raises(StaleQuote):
        f.quotes("BTCUSDT")


def test_tick_closes_on_closed_1m_bar_but_ignores_pre_entry_bar(app):
    ex, now = app.exchange, int(T0 * 1000)
    pid = open_long(app)
    # bar that started BEFORE the entry dipped below the stop -> must not stop us out
    ex.bars["BTC/USDT:USDT"] = [[now - 2 * M, 100, 100.5, 97.0, 100.2, 1]]
    app.clock.t += 61
    app.tick()
    with app.Session() as s:
        assert s.get(Position, pid).status == "open"
        # next minute bar after entry hits the stop
        ex.bars["BTC/USDT:USDT"].append([now + M, 100.2, 100.3, 97.5, 99.0, 1])
    app.clock.t += 120
    app.tick()
    with app.Session() as s:
        p = s.get(Position, pid)
        assert p.status == "closed" and p.exit_reason == "stop"
        assert s.scalar(select(JournalEntry)).exit_reason == "stop"
        assert s.get(AgentState, "heartbeat") is not None
        assert s.scalar(select(EquitySnapshot)) is not None


def test_tick_survives_feed_failure(app):
    app.exchange.fetch_ticker = lambda m: (_ for _ in ()).throw(RuntimeError("network down"))
    app.tick()  # must not raise


def test_funding_settled_once_and_only_for_positions_open_at_print(app):
    app.settle_funding()                                   # first run sets the cursor to "now"
    now = int(app.clock() * 1000)
    app.clock.t += 30
    app.feed.refresh()
    pid = open_long(app)                                   # opened at now + 30s
    with app.Session() as s:
        s.add_all([FundingRate(exchange="bybit", symbol="BTCUSDT", ts=now + 10_000, rate=0.01),   # before open
                   FundingRate(exchange="bybit", symbol="BTCUSDT", ts=now + 40_000, rate=0.001)])
        s.commit()
    app.clock.t += 60
    app.settle_funding()  # old quote is no longer eligible under the 30s freshness rule
    with app.Session() as s:
        assert s.get(Position,pid).funding == 0
    app.feed.refresh()
    app.settle_funding()
    app.settle_funding()                                   # idempotent
    with app.Session() as s:
        p = s.get(Position, pid)
        assert p.funding == pytest.approx(-0.001 * p.qty * 100.0)


def test_collect_market_and_books(app):
    app.collect_market()
    app.snapshot_books()
    with app.Session() as s:
        assert s.scalar(select(Ohlcv)) is None  # fake returns no candles; job still completes


def test_telegram_commands(app):
    open_long(app)
    out = app.handle_command("/status", [])
    assert "PAPER" in out and "BTCUSDT long" in out
    out = app.handle_command("/stop", [])
    assert app.kill_switch.exists() and "Flattened 1" in out
    with app.trading() as (s, eng):
        assert not eng.submit("ETHUSDT", Signal("long", 50, 49, 52, 0.5, "t"), "x").ok
    assert "OFF" in app.handle_command("/resume", []) and not app.kill_switch.exists()
    assert "Commands" in app.handle_command("/whatever", [])
    assert "Daily report" in app.handle_command("/report", [])


def test_analyst_disabled_without_key_and_blocked_skip(app):
    assert app.analyst_cycle() is None and app.review_lessons() == 0 and app.weekly() is None
    app.llm_enabled, app.llm_client = True, FakeClient()  # would raise if called
    app.kill_switch.write_text("")
    assert app.analyst_cycle() == {"status": "blocked"}


def test_analyst_cycle_runs_with_llm(app):
    app.llm_enabled = True
    app.llm_client = FakeClient({"thesis": "flat", "market_view": "neutral", "risk_flags": [],
                                 "strategy_weights": {"trend_donchian": 1, "meanrev_z": 1, "funding_fade": 1},
                                 "picks": []})
    assert app.analyst_cycle()["status"] == "ok"


def test_weekly_and_advance_alert(app):
    app.llm_enabled = True
    app.llm_client = FakeClient("not json")
    res = app.weekly()
    assert res["created"] == [] and any("weekly review" in m for m in app.tg.sent)
    assert app.advance_candidates() == []


def test_daily_report_and_startup(app):
    app.startup()
    assert any("PAPER" in m for m in app.tg.sent)
    open_long(app)
    with app.Session() as s:
        s.add(EquitySnapshot(ts=int(T0 * 1000) - 86_400_000 - 1, equity=9_900, cash=9_900, open_positions=0))
        s.commit()
    text = app.daily_report()
    assert "24h PnL" in text and app.tg.sent[-1] == text


def test_dashboard_escapes_untrusted_text_and_serves(app):
    from db.models import AnalystRun, Lesson
    with app.Session() as s:
        s.add(AnalystRun(ts=1, purpose="decide", model="m", ok=True, request={},
                         response='{"thesis": "<script>alert(1)</script>"}'))
        s.add(Lesson(ts=1, journal_id=1, strategy="x", symbol="BTCUSDT", outcome="loss",
                     what_worked="", mistake="", lesson="<img src=x onerror=alert(1)>", tags=[]))
        s.add(RiskEvent(ts=1, kind="reject", symbol="BTCUSDT", detail={"reasons": ["<b>x</b>"]}))
        s.commit()
    open_long(app)
    html = app.page()
    assert "<script>alert" not in html and "&lt;script&gt;" in html
    assert "<img src=x" not in html and "&lt;b&gt;x" in html
    assert "PAPER" in html and "BTCUSDT" in html
    httpd = dashboard.serve(lambda: html, port=0)
    try:
        port = httpd.server_address[1]
        r = httpx.get(f"http://127.0.0.1:{port}/")
        assert r.status_code == 200 and "Futures Agent" in r.text
        assert httpx.get(f"http://127.0.0.1:{port}/x").status_code == 404
    finally:
        httpd.shutdown()
    broken = dashboard.serve(lambda: 1 / 0, port=0)
    try:
        assert httpx.get(f"http://127.0.0.1:{broken.server_address[1]}/").status_code == 500
    finally:
        broken.shutdown()


def test_reports_without_data(app):
    with app.Session() as s:
        assert "Equity: n/a" in reports.status_text(s, None, "paper", [])


def test_scheduler_jobs_registered(app):
    sched = build_scheduler(app)
    ids = {j.id for j in sched.get_jobs()}
    assert {"tick", "market", "analyst", "books", "news", "news_full", "lessons", "advance",
            "daily_report", "weekly"} <= ids


# ---------------- telegram client ----------------

TOKEN = "123456:SECRET-TOKEN"


def tg_with(handler):
    return Telegram(TOKEN, "42", httpx.Client(transport=httpx.MockTransport(handler)))


def test_telegram_send_and_disabled():
    seen = []

    def h(req):
        seen.append(req)
        return httpx.Response(200, json={"ok": True})
    assert tg_with(h).send("hi")
    assert seen[0].url.path == f"/bot{TOKEN}/sendMessage"
    assert not Telegram("", "").send("x") and Telegram("", "").poll_once(lambda c, a: "") == 0


def test_telegram_only_authorized_chat_and_offset():
    replies = []

    def h(req):
        if req.url.path.endswith("getUpdates"):
            return httpx.Response(200, json={"result": [
                {"update_id": 5, "message": {"chat": {"id": 999}, "text": "/stop"}},      # stranger
                {"update_id": 6, "message": {"chat": {"id": 42}, "text": "hello"}},       # not a command
                {"update_id": 7, "message": {"chat": {"id": 42}, "text": "/Status@bot x"}}]})
        replies.append(req.read())
        return httpx.Response(200, json={"ok": True})
    tg = tg_with(h)
    calls = []
    assert tg.poll_once(lambda c, a: calls.append((c, a)) or "ok") == 1
    assert calls == [("/status", ["x"])] and tg.offset == 8 and len(replies) == 1


def test_telegram_errors_never_log_token(caplog):
    def h(req):
        raise httpx.ConnectError(f"cannot reach {req.url}")
    tg = tg_with(h)
    with caplog.at_level(logging.DEBUG):
        assert not tg.send("x")
        assert tg.poll_once(lambda c, a: "") == 0
    assert TOKEN not in caplog.text and "<token>" in caplog.text
