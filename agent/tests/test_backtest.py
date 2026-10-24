import numpy as np
import pandas as pd
import pytest

from research.backtest import BtConfig, Costs, metrics, run_backtest

H = 3_600_000


def bars(rows):
    """rows: list of (open, high, low, close)."""
    return pd.DataFrame([dict(ts=i * H, open=o, high=h, low=l, close=c, volume=1.0)
                         for i, (o, h, l, c) in enumerate(rows)])


def sigs(n, at=None, side=1, stop=None, take=None):
    s = pd.DataFrame({"side": 0, "stop": np.nan, "take": np.nan, "confidence": 0.0,
                      "rationale": ""}, index=range(n))
    if at is not None:
        s.loc[at, ["side", "stop", "take"]] = [side, stop, take]
    return s


NOCOST = BtConfig(costs=Costs(taker_fee=0.0, slippage_bps=0.0))


def test_long_enters_next_open_and_hits_take():
    df = bars([(100, 100, 100, 100), (101, 101, 101, 101), (101, 112, 100, 111), (111, 111, 111, 111)])
    res = run_backtest(df, sigs(4, at=0, stop=95, take=110), None, NOCOST)
    t = res.trades.iloc[0]
    assert t.entry == 101 and t["stop"] == 96 and t["take"] == 111   # distances re-anchored to fill
    assert t.reason == "take" and t.exit == 111
    assert t.qty == pytest.approx(50 / 5)                     # 0.5% of 10k / stop dist
    assert t.pnl == pytest.approx(100) and t.r == pytest.approx(2.0)
    assert res.equity.iloc[-1] == pytest.approx(10_100)


def test_stop_wins_when_both_hit_same_bar():
    df = bars([(100, 100, 100, 100), (100, 100, 100, 100), (100, 120, 80, 100)])
    t = run_backtest(df, sigs(3, at=0, stop=95, take=110), None, NOCOST).trades.iloc[0]
    assert t.reason == "stop" and t.exit == 95 and t.r == pytest.approx(-1)


def test_gap_through_stop_fills_at_open_short_side():
    df = bars([(100, 100, 100, 100), (100, 100, 100, 100), (110, 111, 109, 110)])
    t = run_backtest(df, sigs(3, at=0, side=-1, stop=105, take=90), None, NOCOST).trades.iloc[0]
    assert t.side == "short" and t.reason == "stop" and t.exit == 110
    assert t.r == pytest.approx(-2)                           # gap doubles the loss


def test_fees_slippage_and_funding_are_charged():
    df = bars([(100, 100, 100, 100)] * 3 + [(100, 100, 100, 100)])
    funding = pd.DataFrame({"ts": [int(1.5 * H), 2 * H + 1], "rate": [0.001, 0.001]})
    cfg = BtConfig(costs=Costs(taker_fee=0.001, slippage_bps=10))
    res = run_backtest(df, sigs(4, at=0, stop=90, take=200), funding, cfg)
    t = res.trades.iloc[0]
    assert t.reason == "end"
    assert t.entry == pytest.approx(100.1) and t.exit == pytest.approx(99.9)
    qty = 50 / 10
    assert t.funding == pytest.approx(-2 * 0.001 * qty * 100)  # long pays positive funding
    assert t.fees == pytest.approx(0.001 * qty * (100.1 + 99.9))
    assert t.pnl == pytest.approx(t.gross - t.fees + t.funding)
    assert res.equity.iloc[-1] == pytest.approx(10_000 + t.pnl)


def test_leverage_cap_limits_size():
    df = bars([(100, 100, 100, 100)] * 3)
    t = run_backtest(df, sigs(3, at=0, stop=99.99, take=101), None, NOCOST).trades.iloc[0]
    assert t.qty == pytest.approx(10_000 * 3 / 100)


def test_signal_on_last_bar_is_not_executed_and_time_stop():
    df = bars([(100, 100, 100, 100)] * 5)
    assert run_backtest(df, sigs(5, at=4, stop=90, take=110), None, NOCOST).trades.empty
    t = run_backtest(df, sigs(5, at=0, stop=90, take=110), None, NOCOST, max_hold_bars=2).trades.iloc[0]
    assert t.reason == "time" and t.bars == 2


def test_one_position_at_a_time():
    df = bars([(100, 100, 100, 100)] * 6)
    s = sigs(6, at=0, stop=90, take=110)
    s.loc[1:3, ["side", "stop", "take"]] = [1, 90, 110]
    assert len(run_backtest(df, s, None, NOCOST).trades) == 1


def test_metrics():
    df = bars([(100, 100, 100, 100), (101, 101, 101, 101), (101, 112, 100, 111)] +
              [(111, 111, 111, 111)] * 48)
    m = metrics(run_backtest(df, sigs(len(df), at=0, stop=95, take=110), None, NOCOST))
    assert m["trades"] == 1 and m["win_rate_pct"] == 100 and m["profit_factor"] is None
    assert m["total_return_pct"] == pytest.approx(1.0) and m["expectancy_r"] == pytest.approx(2.0)
    empty = metrics(run_backtest(df, sigs(len(df)), None, NOCOST))
    assert empty["trades"] == 0 and empty["total_return_pct"] == 0 and empty["max_dd_pct"] == 0
