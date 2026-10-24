import dataclasses
import shutil

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

from db.models import JournalEntry, Order, PaperAccount, Position
from execution.engine import ExecutionEngine
from execution.paper import PaperBroker
from journal.journal import Journal, market_snapshot
from risk.limits import RISK_FILE, load_limits
from risk.manager import OrderRequest, RiskManager, RiskViolation
from strategies.base import Signal

T0 = 1_792_454_400_000 + 3_600_000


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


class Quotes(dict):
    def __call__(self, symbol):
        return self[symbol]


@pytest.fixture
def env(session, tmp_path):
    cfg = tmp_path / "risk.yaml"
    shutil.copy(RISK_FILE, cfg)
    alerts, clock = [], Clock()
    quotes = Quotes(BTCUSDT=(99.9, 100.1), ETHUSDT=(49.9, 50.1), SOLUSDT=(9.9, 10.1),
                    XRPUSDT=(0.99, 1.01))
    rm = RiskManager(session, load_limits(cfg), kill_switch=tmp_path / "STOP",
                     on_alert=alerts.append, clock=clock)
    broker = PaperBroker(session, rm, quotes, taker_fee=0.001, slippage_bps=0, clock=clock)
    eng = ExecutionEngine(session, rm, broker, Journal(session), alert=alerts.append)
    return eng, quotes, alerts, clock, tmp_path


LONG = Signal("long", 100.0, 98.0, 106.0, 0.6, "breakout")
SHORT = Signal("short", 50.0, 51.0, 47.0, 0.5, "fade")


def test_open_fills_at_ask_sizes_by_risk_and_journals(env):
    eng, *_ = env
    r = eng.submit("BTCUSDT", LONG, "trend", thesis="BTC breaking out", regime="trend_up",
                   context={"note": "x"}, client_order_id="c1")
    assert r.ok and r.qty == pytest.approx(10_000 * 0.005 / (100.1 - 98.0))
    pos = eng.s.get(Position, r.position_id)
    assert pos.entry == 100.1 and pos.stop == 98.0 and pos.take == 106.0
    acc = eng.s.get(PaperAccount, 1)
    assert acc.cash == pytest.approx(10_000 - 0.001 * 100.1 * pos.qty)
    j = eng.s.scalar(select(JournalEntry))
    assert j.thesis == "BTC breaking out" and j.signal["rationale"] == "breakout"
    assert j.regime == "trend_up" and j.context == {"note": "x"}


def test_idempotent_client_order_id(env):
    eng, *_ = env
    a = eng.submit("BTCUSDT", LONG, "trend", client_order_id="same")
    b = eng.submit("BTCUSDT", LONG, "trend", client_order_id="same")
    assert a.position_id == b.position_id and "duplicate" in b.reasons[0]
    assert len(eng.s.scalars(select(Order)).all()) == 1
    # broker level too: replaying an order id never double-fills
    assert eng.broker.open(None, "same").id == a.position_id


def test_broker_refuses_without_valid_approval(env):
    eng, *_ = env
    with pytest.raises(RiskViolation):
        eng.broker.open(None, "x1")
    d = eng.risk.evaluate(OrderRequest("BTCUSDT", "long", 100.1, 98.0), eng.broker.snapshot())
    forged = dataclasses.replace(d.approval, qty=d.approval.qty * 100)
    with pytest.raises(RiskViolation, match="tampered"):
        eng.broker.open(forged, "x2")
    assert eng.broker.open_positions() == []


def test_llm_style_bypass_through_engine_rejected(env):
    eng, *_ = env
    r = eng.submit("BTCUSDT", LONG, "llm", requested_qty=1_000)
    assert not r.ok and "exceeds allowed" in r.reasons[0]
    r = eng.submit("BTCUSDT", LONG, "llm", requested_leverage=20)
    assert not r.ok and "leverage" in r.reasons[0]
    assert eng.broker.open_positions() == []


def test_max_three_positions(env):
    eng, *_ = env
    sigs = {"BTCUSDT": LONG, "ETHUSDT": SHORT,
            "SOLUSDT": Signal("long", 10, 9.5, 11, 0.5, "x"),
            "XRPUSDT": Signal("long", 1, 0.95, 1.1, 0.5, "x")}
    res = {s: eng.submit(s, g, "t") for s, g in sigs.items()}
    assert [r.ok for r in res.values()] == [True, True, True, False]
    assert "max open positions" in " ".join(res["XRPUSDT"].reasons)


def test_take_profit_closes_and_journal_has_r_mae_mfe(env):
    eng, quotes, *_ = env
    r = eng.submit("BTCUSDT", LONG, "trend")
    eng.on_price("BTCUSDT", high=101, low=99, last=100.5)        # no exit
    closed = eng.on_price("BTCUSDT", high=106.5, low=100, last=106.2)
    assert len(closed) == 1 and closed[0].exit_reason == "take" and closed[0].exit == 106.0
    j = eng.s.scalar(select(JournalEntry).where(JournalEntry.position_id == r.position_id))
    risk = (100.1 - 98.0) * j.qty
    assert j.exit == 106.0 and j.pnl == pytest.approx(closed[0].pnl)
    assert j.r == pytest.approx(j.pnl / risk)
    assert j.mae_r == pytest.approx((100.1 - 99) * j.qty / risk)
    assert j.mfe_r == pytest.approx((106.5 - 100.1) * j.qty / risk)
    acc = eng.s.get(PaperAccount, 1)
    assert acc.cash == pytest.approx(10_000 + j.pnl)
    assert eng.journal.closed()[0].id == j.id


def test_stop_beats_take_and_gap_fills_worse(env):
    eng, *_ = env
    eng.submit("ETHUSDT", SHORT, "fade")
    closed = eng.on_price("ETHUSDT", high=53, low=46, last=52.5)  # both touched, gapped past stop
    p = closed[0]
    assert p.exit_reason == "stop" and p.exit == 52.5 and p.pnl < 0


def test_funding_applied_to_cash_and_position(env):
    eng, *_ = env
    eng.submit("ETHUSDT", SHORT, "fade")
    amt = eng.broker.apply_funding("ETHUSDT", 0.001, 50.0)
    pos = eng.broker.open_positions()[0]
    assert amt == pytest.approx(0.001 * pos.qty * 50) and pos.funding == pytest.approx(amt)


def test_kill_switch_flattens_and_blocks(env):
    eng, _, alerts, _, tmp = env
    eng.submit("BTCUSDT", LONG, "t")
    (tmp / "STOP").write_text("")
    closed = eng.on_price("BTCUSDT", 100.5, 99.5, 100.0)
    assert closed and closed[0].exit_reason == "kill_switch"
    assert eng.broker.open_positions() == []
    assert not eng.submit("ETHUSDT", SHORT, "t").ok
    assert any("Flattened" in a for a in alerts)


def test_drawdown_halt_flattens_everything(env):
    eng, quotes, alerts, *_ = env
    eng.submit("BTCUSDT", LONG, "t")
    eng.risk._set("peak_equity", 12_000)              # equity ~10k -> 16% below peak
    closed = eng.on_price("BTCUSDT", 100.5, 99.5, 100.0)
    assert closed and closed[0].exit_reason == "halt" and eng.risk.halted
    assert any("FULL HALT" in a for a in alerts)


def test_daily_loss_blocks_new_trades_via_engine(env):
    eng, quotes, *_ = env
    eng.risk.update_equity(10_000)
    eng.broker.account.cash = 9_700                   # -3% today
    r = eng.submit("BTCUSDT", LONG, "t")
    assert not r.ok and "daily loss" in " ".join(r.reasons)


def test_restart_resumes_state_and_reconciles(env, session):
    eng, quotes, alerts, clock, _ = env
    eng.submit("BTCUSDT", LONG, "t")
    bad = Position(symbol="ETHUSDT", side="long", qty=1, entry=50, stop=55, take=None,
                   status="open", opened_ts=T0, fees=0, funding=0, mae=0, mfe=0,
                   strategy="corrupt", exit_reason="")
    session.add(bad)
    session.commit()
    broker2 = PaperBroker(session, eng.risk, quotes, clock=clock)   # "restart"
    eng2 = ExecutionEngine(session, eng.risk, broker2, Journal(session), alert=alerts.append)
    issues = eng2.startup()
    assert len(issues) == 1 and "invalid stop" in issues[0]
    assert [p.symbol for p in broker2.open_positions()] == ["BTCUSDT"]
    assert broker2.account.cash == eng.broker.account.cash


def test_close_is_idempotent_and_equity_marks(env):
    eng, quotes, *_ = env
    eng.submit("BTCUSDT", LONG, "t")
    p = eng.broker.open_positions()[0]
    quotes["BTCUSDT"] = (101.9, 102.1)
    assert eng.broker.equity() == pytest.approx(eng.broker.account.cash + (102 - 100.1) * p.qty)
    eng.broker.close(p, "manual")
    cash = eng.broker.account.cash
    eng.broker.close(p, "manual")
    p.status = "open"                                   # even if state were confused,
    eng.broker.close(p, "manual")                       # the close order id blocks a 2nd fill
    assert eng.broker.account.cash == cash


def test_market_snapshot():
    n = 300
    rng = np.random.default_rng(1)
    c = 100 + np.cumsum(rng.normal(0, 1, n))
    df = pd.DataFrame({"ts": np.arange(n) * 3_600_000, "open": c, "high": c + 1, "low": c - 1,
                       "close": c, "volume": 1.0})
    snap = market_snapshot(df, n=10)
    assert len(snap["close"]) == 10 and snap["atr14"] > 0 and 0 <= snap["rsi14"] <= 100
    assert snap["regime"] in {"trend_up", "trend_down", "range", "high_vol", "unknown"}
    assert market_snapshot(pd.DataFrame()) == {}
