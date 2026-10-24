import numpy as np
import pandas as pd
import pytest

from research.features import adx, atr, regime, resample, rsi, zscore
from strategies.base import Signal
from strategies.baseline import ALL, FundingFade, MeanReversion, TrendFollowing

H = 3_600_000


@pytest.fixture(scope="module")
def market():
    rng = np.random.default_rng(7)
    n = 3000
    # regime-switching random walk: trends then chop
    drift = np.where((np.arange(n) // 500) % 2 == 0, 0.0015, 0.0)
    close = 100 * np.exp(np.cumsum(drift + rng.normal(0, 0.01, n)))
    open_ = np.r_[close[0], close[:-1]]
    spread = np.abs(rng.normal(0, 0.006, n)) * close
    df = pd.DataFrame({"ts": np.arange(n) * H, "open": open_,
                       "high": np.maximum(open_, close) + spread,
                       "low": np.minimum(open_, close) - spread, "close": close,
                       "volume": 1.0})
    f_ts = np.arange(0, n * H, 8 * H)
    rate = rng.normal(0.0001, 0.0001, len(f_ts))
    rate[[150, 200]] = [0.003, -0.003]  # two crowded prints
    return df, pd.DataFrame({"ts": f_ts, "rate": rate})


def test_indicators_basic(market):
    df, _ = market
    r = rsi(df["close"]).dropna()
    assert r.between(0, 100).all()
    assert (atr(df) .dropna() > 0).all()
    assert adx(df).dropna().between(0, 100).all()
    z = zscore(pd.Series([1.0, 2, 3, 4, 100]), 5)
    assert z.iloc[-1] > 1.9
    assert set(regime(df).unique()) <= {"trend_up", "trend_down", "range", "high_vol", "unknown"}


def test_resample_4h_drops_partial(market):
    df, _ = market
    r = resample(df.iloc[:10], "4h")
    assert len(r) == 2  # 10 hourly bars -> 2 full 4h bars
    assert r.iloc[0]["open"] == df.iloc[0]["open"] and r.iloc[0]["close"] == df.iloc[3]["close"]
    assert r.iloc[0]["high"] == df.iloc[:4]["high"].max()
    assert r.iloc[1]["ts"] == 4 * H


@pytest.mark.parametrize("cls", ALL)
def test_signals_are_well_formed(cls, market):
    df, funding = market
    strat = cls()
    s = strat.signals(df, funding)
    assert len(s) == len(df)
    act = s[s["side"] != 0]
    assert len(act) > 0, f"{strat.name} never fired on synthetic data"
    c = df.loc[act.index, "close"]
    lo = act["side"] > 0
    assert ((act.loc[lo, "stop"] < c[lo]) & (c[lo] < act.loc[lo, "take"])).all()
    assert ((act.loc[~lo, "take"] < c[~lo]) & (c[~lo] < act.loc[~lo, "stop"])).all()
    assert act["confidence"].between(0, 1).all() and (act["rationale"] != "").all()


@pytest.mark.parametrize("cls", ALL)
def test_no_lookahead(cls, market):
    """Signals at bar i must not change when future bars are removed."""
    df, funding = market
    strat = cls()
    full = strat.signals(df, funding)
    for cut in (1200, 2000, 2999):
        part = strat.signals(df.iloc[:cut + 1], funding[funding["ts"] <= df["ts"].iloc[cut] + H])
        pd.testing.assert_series_equal(part["side"], full["side"].iloc[:cut + 1], check_names=False)


def test_funding_fade_fires_once_per_print(market):
    df, funding = market
    s = FundingFade().signals(df, funding)
    fired = s[s["side"] != 0]
    assert len(fired) == fired.index.nunique()
    assert FundingFade().signals(df, None)["side"].eq(0).all()
    assert -1 in fired["side"].values and 1 in fired["side"].values


def test_latest_signal_and_signal_validation(market):
    df, _ = market
    strat = MeanReversion()
    s = strat.signals(df)
    i = s.index[s["side"] != 0][0]
    sig = strat.latest_signal(df.iloc[:i + 1])
    assert isinstance(sig, Signal) and sig.entry == df["close"].iloc[i]
    assert sig.to_dict()["side"] in ("long", "short")
    assert TrendFollowing().latest_signal(df.iloc[:10]) is None
    with pytest.raises(ValueError):
        Signal("long", 100, 101, 110, 0.5, "x")
    with pytest.raises(ValueError):
        Signal("short", 100, 99, 90, 0.5, "x")
    with pytest.raises(ValueError):
        Signal("long", 100, 90, 110, 1.5, "x")
