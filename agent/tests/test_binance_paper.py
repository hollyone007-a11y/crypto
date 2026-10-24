import shutil

import ccxt
import pandas as pd
import pytest
from sqlalchemy import func, select

from analyst import rules
from config.settings import Settings
from data.market import BinancePublic, public_exchange
from db.models import AnalystRun, Order, Position, StrategyVersion
from ops.app import App, LiveModeRefused
from risk.limits import RISK_FILE
from strategies.base import Signal
from tests.test_ops import Clock, FakeEx, TgSpy


@pytest.fixture
def rules_app(tmp_path):
    cfg = tmp_path / "risk.yaml"
    shutil.copy(RISK_FILE, cfg)
    clock = Clock()
    settings = Settings(_env_file=None, trading_mode="paper", exchange="binanceusdm",
                        analyst_mode="rules", database_url=f"sqlite:///{tmp_path.as_posix()}/test.db")
    app = App(settings, exchange=FakeEx(clock), telegram=TgSpy(),
              kill_switch=tmp_path / "STOP", risk_file=cfg, clock=clock)
    with app.Session() as s:
        for v in s.scalars(select(StrategyVersion)):
            v.metrics={'wf':{'trades':40,'expectancy_r':.1,'sharpe':.5},'wf_symbols':['BTCUSDT','ETHUSDT']}
        s.commit()
    return app


def candidate():
    bars = pd.DataFrame({"ts": list(range(100)), "open": [100.] * 100,
                         "high": [101.] * 100, "low": [99.] * 100,
                         "close": [100.] * 100, "volume": [1.] * 100})
    return dict(candidate_id="BTC:trend:123", symbol="BTCUSDT", strategy="trend_donchian@v1",
                signal=Signal("long", 100., 98., 106., .7, "test breakout"), bars=bars)


def test_rules_fill_is_simulated_and_retry_does_not_duplicate(rules_app, monkeypatch):
    c = candidate()
    monkeypatch.setattr(rules, "build_candidates", lambda *args: ([c, c], {}))
    assert len(rules_app.analyst_cycle()["submitted"]) == 1
    rules_app.analyst_cycle()
    with rules_app.Session() as s:
        assert s.scalar(select(func.count()).select_from(Order)) == 1
        assert s.scalar(select(func.count()).select_from(Position)) == 1
        assert s.scalar(select(AnalystRun)).model == "rules-baseline-v1"


def test_stop_blocks_rules(rules_app):
    rules_app.kill_switch.write_text("stop")
    assert rules_app.analyst_cycle() == {"status": "blocked"}
    with rules_app.Session() as s:
        assert s.scalar(select(func.count()).select_from(Order)) == 0


def test_unproven_strategy_is_skipped(rules_app,monkeypatch):
    c=candidate()
    monkeypatch.setattr(rules,'build_candidates',lambda *a:([c],{}))
    with rules_app.Session() as s:
        for v in s.scalars(select(StrategyVersion)):
            v.metrics={'wf':{'trades':8,'expectancy_r':-.1,'sharpe':-.5},'wf_symbols':['BTCUSDT','ETHUSDT']}
        s.commit()
    result=rules_app.analyst_cycle()
    assert not result['submitted'] and len(result['skipped'])==1


def test_network_failure_never_creates_paper_order(rules_app):
    def fail(*args):
        raise RuntimeError("network down")
    rules_app.exchange.fetch_ticker = fail
    with pytest.raises(RuntimeError):
        rules_app.analyst_cycle()
    with rules_app.Session() as s:
        assert s.scalar(select(func.count()).select_from(Order)) == 0


def test_binance_quote_uses_public_orderbook(monkeypatch):
    monkeypatch.setattr(ccxt.binanceusdm, "fetch_ticker", lambda *a: {"last": 100.})
    ex = public_exchange("binanceusdm")
    monkeypatch.setattr(ex, "fetch_order_book", lambda *a, **k: {"bids": [[99., 1]], "asks": [[101., 1]]})
    assert isinstance(ex, BinancePublic) and not ex.apiKey and not ex.secret
    assert ex.fetch_ticker("BTC/USDT:USDT") == {"last": 100., "bid": 99., "ask": 101.}
    monkeypatch.setattr(ex, "fetch_order_book", lambda *a, **k: {"bids": [], "asks": []})
    with pytest.raises(ValueError):
        ex.fetch_ticker("BTC/USDT:USDT")


def test_binance_rules_cannot_start_live():
    with pytest.raises(LiveModeRefused):
        App(Settings(_env_file=None, trading_mode="live", exchange="binanceusdm", analyst_mode="rules"))
