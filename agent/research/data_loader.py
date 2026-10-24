import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import FundingRate, Ohlcv
from research.features import resample


def load_ohlcv(session: Session, symbol: str, timeframe: str = "1h",
               start_ms: int | None = None, end_ms: int | None = None) -> pd.DataFrame:
    """Load bars; 4h is built from stored 1h bars."""
    base_tf = "1h" if timeframe == "4h" else timeframe
    q = select(Ohlcv.ts, Ohlcv.open, Ohlcv.high, Ohlcv.low, Ohlcv.close, Ohlcv.volume) \
        .where(Ohlcv.symbol == symbol, Ohlcv.timeframe == base_tf).order_by(Ohlcv.ts)
    if start_ms is not None:
        q = q.where(Ohlcv.ts >= start_ms)
    if end_ms is not None:
        q = q.where(Ohlcv.ts < end_ms)
    df = pd.DataFrame(session.execute(q).all(), columns=["ts", "open", "high", "low", "close", "volume"])
    if timeframe == "4h" and not df.empty:
        df = resample(df, "4h")
    return df.reset_index(drop=True)


def load_funding(session: Session, symbol: str) -> pd.DataFrame:
    q = select(FundingRate.ts, FundingRate.rate).where(FundingRate.symbol == symbol) \
        .order_by(FundingRate.ts)
    return pd.DataFrame(session.execute(q).all(), columns=["ts", "rate"])
