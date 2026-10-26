"""Indicators and market-regime detection. Pure functions over OHLCV DataFrames."""
import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev = df["close"].shift(1)
    return pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(),
                      (df["low"] - prev).abs()], axis=1).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return wilder(true_range(df), n)


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    up, down = wilder(d.clip(lower=0), n), wilder(-d.clip(upper=0), n)
    rs = up / down.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100.0).where(up.notna())


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr = wilder(true_range(df), n)
    plus_di = 100 * wilder(plus_dm, n) / tr
    minus_di = 100 * wilder(minus_dm, n) / tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return wilder(dx.fillna(0), n)


def zscore(s: pd.Series, n: int) -> pd.Series:
    m, sd = s.rolling(n).mean(), s.rolling(n).std(ddof=0)
    return (s - m) / sd.replace(0, np.nan)


def regime(df: pd.DataFrame, adx_n: int = 14, vol_window: int = 500) -> pd.Series:
    """trend_up / trend_down / range / high_vol — uses only past data at each bar."""
    a = adx(df, adx_n)
    atr_pct = atr(df, 14) / df["close"]
    vol_rank = atr_pct.rolling(vol_window, min_periods=100).rank(pct=True)
    slope = ema(df["close"], 50) - ema(df["close"], 50).shift(10)
    out = pd.Series("range", index=df.index, dtype=object)
    out[(a > 25) & (slope > 0)] = "trend_up"
    out[(a > 25) & (slope < 0)] = "trend_down"
    out[vol_rank > 0.9] = "high_vol"
    out[a.isna()] = "unknown"
    return out


def resample(df: pd.DataFrame, rule: str, base_ms: int | None = None) -> pd.DataFrame:
    """Resample ts-ms OHLCV frame (e.g. 1h -> 4h). Drops incomplete last bucket."""
    if df.empty: return df.copy()
    df=df.drop_duplicates('ts').sort_values('ts')
    step=base_ms or (int(df.ts.diff().dropna().median()) if len(df)>1 else 0)
    duration=int(pd.Timedelta(rule).total_seconds()*1000)
    if not step or duration%step: return df.iloc[:0].copy()
    x = df.set_index(pd.to_datetime(df["ts"], unit="ms", utc=True))
    agg = x.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
         "ts": "count"}).rename(columns={"ts": "n"})
    full = duration//step
    agg = agg[agg["n"] == full].drop(columns="n").dropna()
    agg["ts"] = (agg.index.as_unit("ms").asi8).astype("int64")
    return agg.reset_index(drop=True)[["ts", "open", "high", "low", "close", "volume"]]
