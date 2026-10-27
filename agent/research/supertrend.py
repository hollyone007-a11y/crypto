"""Independent mathematical implementation of ATR trailing bands.
Reference: QuantConnect Super Trend documentation. No TradingView data or code.
"""
import numpy as np
import pandas as pd
from research.features import atr


def calculate(bars,period=10,multiplier=3.):
    volatility=atr(bars,period).to_numpy();close=bars.close.to_numpy()
    middle=(bars.high.to_numpy()+bars.low.to_numpy())/2
    direction=np.zeros(len(bars),dtype=int);level=np.full(len(bars),np.nan)
    upper=lower=None;trend=-1
    for i in range(len(bars)):
        if not np.isfinite(volatility[i]):continue
        raw_upper=middle[i]+multiplier*volatility[i];raw_lower=middle[i]-multiplier*volatility[i]
        if upper is None:upper,lower=raw_upper,raw_lower
        else:
            upper=raw_upper if raw_upper<upper or close[i-1]>upper else upper
            lower=raw_lower if raw_lower>lower or close[i-1]<lower else lower
            if trend<0 and close[i]>upper:trend=1
            elif trend>0 and close[i]<lower:trend=-1
        direction[i]=trend;level[i]=lower if trend>0 else upper
    return pd.Series(direction,index=bars.index),pd.Series(level,index=bars.index)
