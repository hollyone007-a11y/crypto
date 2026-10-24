"""Three baseline strategies. Parameters are textbook defaults chosen BEFORE
looking at results — they are intentionally not tuned to history."""
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from research.features import adx, atr, ema, regime, rsi, zscore
from strategies.base import Strategy, empty_signals

TF_MS = {"1h": 3_600_000, "4h": 14_400_000}


@dataclass
class TrendFollowing(Strategy):
    """Donchian breakout in the direction of the 200-EMA."""
    name: str = "trend_donchian"
    timeframe: str = "4h"
    params: dict = field(default_factory=lambda: {
        "channel": 55, "ema": 200, "atr": 20, "stop_atr": 2.0, "take_atr": 6.0})

    def signals(self, df, funding=None):
        p, out = self.params, empty_signals(df)
        a = atr(df, p["atr"])
        hi = df["high"].rolling(p["channel"]).max().shift(1)
        lo = df["low"].rolling(p["channel"]).min().shift(1)
        trend = ema(df["close"], p["ema"])
        long_ = (df["close"] > hi) & (df["close"] > trend)
        short = (df["close"] < lo) & (df["close"] < trend)
        _fill(out, df, long_, short, a * p["stop_atr"], a * p["take_atr"],
              conf=(abs(df["close"] - trend) / (3 * a)).clip(0, 1),
              why_long=f"close broke {p['channel']}-bar high above EMA{p['ema']}",
              why_short=f"close broke {p['channel']}-bar low below EMA{p['ema']}")
        return out


@dataclass
class MeanReversion(Strategy):
    """Fade 2-sigma stretches back to the mean, only in a ranging regime."""
    name: str = "meanrev_z"
    timeframe: str = "1h"
    max_hold_bars: int = 48
    params: dict = field(default_factory=lambda: {
        "window": 48, "z": 2.0, "rsi_lo": 30, "rsi_hi": 70, "stop_atr": 2.0})

    def signals(self, df, funding=None):
        p, out = self.params, empty_signals(df)
        a = atr(df, 14)
        z = zscore(df["close"], p["window"])
        mean = df["close"].rolling(p["window"]).mean()
        r = rsi(df["close"], 14)
        ranging = regime(df) == "range"
        long_ = ranging & (z < -p["z"]) & (r < p["rsi_lo"])
        short = ranging & (z > p["z"]) & (r > p["rsi_hi"])
        _fill(out, df, long_, short, a * p["stop_atr"], (mean - df["close"]).abs(),
              conf=(z.abs() / 3).clip(0, 1),
              why_long="range regime, z<-2 & RSI oversold -> revert to mean",
              why_short="range regime, z>+2 & RSI overbought -> revert to mean")
        return out


@dataclass
class FundingFade(Strategy):
    """Fade crowded positioning: extreme funding vs its own 30-day history."""
    name: str = "funding_fade"
    timeframe: str = "1h"
    max_hold_bars: int = 72
    params: dict = field(default_factory=lambda: {
        "lookback": 90, "z": 2.0, "min_abs_rate": 0.0002, "stop_atr": 2.5, "take_atr": 3.75})

    def signals(self, df, funding=None):
        p, out = self.params, empty_signals(df)
        if funding is None or funding.empty:
            return out
        f = funding.sort_values("ts")[["ts", "rate"]].copy()
        f["fz"] = zscore(f["rate"], p["lookback"])
        f["fts"] = f["ts"]
        # a print is usable once the bar that contains it has CLOSED
        bars = pd.DataFrame({"close_ts": df["ts"] + TF_MS[self.timeframe]})
        m = pd.merge_asof(bars, f.rename(columns={"ts": "close_ts"}), on="close_ts",
                          direction="backward")
        m.index = df.index
        fresh = m["fts"].ne(m["fts"].shift(1)) & m["fts"].notna()  # act once per print
        a = atr(df, 14)
        short = fresh & (m["fz"] > p["z"]) & (m["rate"] > p["min_abs_rate"])
        long_ = fresh & (m["fz"] < -p["z"]) & (m["rate"] < -p["min_abs_rate"])
        _fill(out, df, long_, short, a * p["stop_atr"], a * p["take_atr"],
              conf=(m["fz"].abs() / 4).clip(0, 1),
              why_long="funding extremely negative (crowded shorts) -> fade",
              why_short="funding extremely positive (crowded longs) -> fade")
        return out


def _fill(out, df, long_, short, stop_d, take_d, conf, why_long, why_short):
    valid = stop_d.gt(0) & take_d.gt(0)
    long_, short = (long_ & valid).fillna(False), (short & valid & ~long_).fillna(False)
    c = df["close"]
    out.loc[long_, "side"] = 1
    out.loc[long_, "stop"] = c - stop_d
    out.loc[long_, "take"] = c + take_d
    out.loc[long_, "rationale"] = why_long
    out.loc[short, "side"] = -1
    out.loc[short, "stop"] = c + stop_d
    out.loc[short, "take"] = c - take_d
    out.loc[short, "rationale"] = why_short
    out.loc[long_ | short, "confidence"] = np.nan_to_num(conf[long_ | short].astype(float))


@dataclass
class EmaAdxTrend(Strategy):
    """Public trend-following concept; an adaptation, not a trader's private system."""
    name: str = 'ema_adx'
    timeframe: str = '1h'
    max_hold_bars: int = 72
    params: dict = field(default_factory=lambda:{'fast':20,'slow':50,'trend':200,
        'adx_min':25.,'stop_atr':2.,'take_atr':4.})

    def signals(self,df,funding=None):
        p,out=self.params,empty_signals(df)
        fast,slow=ema(df.close,p['fast']),ema(df.close,p['slow'])
        trend=ema(df.close,p['trend']); strength=adx(df)
        long_=(fast>slow)&(fast.shift(1)<=slow.shift(1))&(df.close>trend)&(strength>p['adx_min'])
        short=(fast<slow)&(fast.shift(1)>=slow.shift(1))&(df.close<trend)&(strength>p['adx_min'])
        a=atr(df)
        _fill(out,df,long_,short,a*p['stop_atr'],a*p['take_atr'],
              conf=(strength/50).clip(0,1),why_long='EMA cross up with ADX and long-term trend',
              why_short='EMA cross down with ADX and long-term trend')
        return out


ALL = [TrendFollowing, MeanReversion, FundingFade, EmaAdxTrend]
