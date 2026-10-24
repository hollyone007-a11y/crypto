"""Compact market context for the analyst. Everything here is computed by code;
news text is passed through as clearly-marked untrusted data."""
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from db.models import FearGreed, Lesson, MacroEvent, News, OpenInterest, OrderBookSnapshot
from research.features import atr, regime, zscore

H_MS = 3_600_000


def symbol_context(session: Session, symbol: str, bars_1h, funding) -> dict:
    c = bars_1h["close"]
    last = float(c.iloc[-1])
    ctx = {
        "symbol": symbol,
        "last": last,
        "chg_24h_pct": round(100 * (last / float(c.iloc[-25]) - 1), 2) if len(c) > 25 else None,
        "chg_7d_pct": round(100 * (last / float(c.iloc[-169]) - 1), 2) if len(c) > 169 else None,
        "atr_pct": round(100 * float(atr(bars_1h).iloc[-1]) / last, 3) if len(c) > 15 else None,
        "regime": str(regime(bars_1h).iloc[-1]),
    }
    if funding is not None and len(funding):
        z = zscore(funding["rate"], 90)
        ctx["funding_last"] = float(funding["rate"].iloc[-1])
        ctx["funding_z90"] = None if z.isna().iloc[-1] else round(float(z.iloc[-1]), 2)
    oi = session.scalars(select(OpenInterest).where(OpenInterest.symbol == symbol)
                         .order_by(desc(OpenInterest.ts)).limit(25)).all()
    if len(oi) >= 25 and oi[-1].amount:
        ctx["oi_chg_24h_pct"] = round(100 * (oi[0].amount / oi[-1].amount - 1), 2)
    ob = session.scalar(select(OrderBookSnapshot).where(OrderBookSnapshot.symbol == symbol)
                        .order_by(desc(OrderBookSnapshot.ts)).limit(1))
    if ob:
        ctx["book_imbalance_top10"] = round(ob.imbalance, 3)
    return ctx


def global_context(session: Session, now_ms: int) -> dict:
    fg = session.scalar(select(FearGreed).order_by(desc(FearGreed.ts)).limit(1))
    events = session.scalars(select(MacroEvent).where(
        MacroEvent.ts >= now_ms - 2 * H_MS, MacroEvent.ts <= now_ms + 24 * H_MS,
        MacroEvent.impact == "High").order_by(MacroEvent.ts)).all()
    news = session.scalars(select(News).where(News.published_ts >= now_ms - 12 * H_MS)
                           .order_by(desc(News.published_ts)).limit(20)).all()
    lessons = session.scalars(select(Lesson).order_by(desc(Lesson.ts)).limit(8)).all()
    return {
        "fear_greed": {"value": fg.value, "label": fg.classification} if fg else None,
        "macro_next_24h": [{"in_hours": round((e.ts - now_ms) / H_MS, 1), "country": e.country,
                            "event": e.title, "forecast": e.forecast, "previous": e.previous}
                           for e in events],
        "headlines": [{"source": n.source, "hours_ago": round((now_ms - n.published_ts) / H_MS, 1),
                       "title": n.title[:200]} for n in news],
        "recent_lessons": [{"strategy": l.strategy, "outcome": l.outcome, "lesson": l.lesson}
                           for l in lessons],
    }
