"""Trade journal: thesis, signal, entry/exit, PnL, R, MAE/MFE, regime, market snapshot."""
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import JournalEntry, Position
from research.features import atr, regime, rsi


def market_snapshot(df: pd.DataFrame, n: int = 48) -> dict:
    """Compact, JSON-safe picture of the market at decision time."""
    if df is None or df.empty:
        return {}
    tail = df.tail(n)
    return {
        "ts": [int(x) for x in tail["ts"]],
        "close": [round(float(x), 6) for x in tail["close"]],
        "atr14": _last(atr(df, 14)),
        "rsi14": _last(rsi(df["close"], 14)),
        "regime": str(regime(df).iloc[-1]),
    }


def _last(s: pd.Series):
    v = s.iloc[-1] if len(s) else None
    return None if v is None or pd.isna(v) else round(float(v), 6)


class Journal:
    def __init__(self, session: Session):
        self.s = session

    def record_open(self, pos: Position, thesis: str, signal: dict, regime_: str = "",
                    context: dict | None = None) -> JournalEntry:
        e = self.s.scalar(select(JournalEntry).where(JournalEntry.position_id == pos.id))
        if e:
            return e
        e = JournalEntry(position_id=pos.id, symbol=pos.symbol, strategy=pos.strategy,
                         side=pos.side, thesis=thesis, signal=signal, regime=regime_,
                         context=context or {}, entry_ts=pos.opened_ts, entry=pos.entry,
                         qty=pos.qty, exit_reason="")
        self.s.add(e)
        self.s.commit()
        return e

    def record_close(self, pos: Position) -> JournalEntry | None:
        e = self.s.scalar(select(JournalEntry).where(JournalEntry.position_id == pos.id))
        if e is None:
            return None
        risk = abs(pos.entry - pos.stop) * pos.qty
        e.exit_ts, e.exit, e.pnl = pos.closed_ts, pos.exit, pos.pnl
        e.fees, e.funding, e.exit_reason = pos.fees, pos.funding, pos.exit_reason
        e.r = pos.pnl / risk if risk else None
        e.mae_r = pos.mae * pos.qty / risk if risk else None
        e.mfe_r = pos.mfe * pos.qty / risk if risk else None
        self.s.commit()
        return e

    def closed(self, limit: int = 50) -> list[JournalEntry]:
        q = select(JournalEntry).where(JournalEntry.exit_ts.is_not(None)) \
            .order_by(JournalEntry.exit_ts.desc()).limit(limit)
        return list(self.s.scalars(q))
