"""Post-trade review: Sonnet writes one lesson per closed journal entry."""
import json
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from analyst.llm import LLM, ROUTINE_MODEL
from analyst.schema import LESSON_SCHEMA, LessonOut
from db.models import JournalEntry, Lesson

SYSTEM = """You review one closed paper trade of an automated futures system.
Be specific and short. Separate luck from process: a losing trade can be a good trade.
'lesson' must be one actionable sentence about the strategy or the analyst's filter,
not generic advice. Tags: short lowercase keywords (e.g. regime, funding, news, stop_too_tight)."""


def review_pending(session: Session, llm: LLM, limit: int = 10) -> int:
    reviewed = select(Lesson.journal_id)
    pending = session.scalars(select(JournalEntry).where(
        JournalEntry.exit_ts.is_not(None), JournalEntry.id.not_in(reviewed))
        .order_by(JournalEntry.exit_ts).limit(limit)).all()
    n = 0
    for e in pending:
        trade = {k: getattr(e, k) for k in ("symbol", "strategy", "side", "thesis", "signal",
                                            "regime", "entry", "exit", "pnl", "r", "mae_r",
                                            "mfe_r", "fees", "funding", "exit_reason")}
        trade["hours_held"] = round((e.exit_ts - e.entry_ts) / 3_600_000, 1)
        trade["closes_last_48h_at_entry"] = (e.context or {}).get("close", [])[-48:]
        out = llm.call(purpose="lesson", model=ROUTINE_MODEL, system=SYSTEM,
                       user=json.dumps(trade, default=str), schema=LESSON_SCHEMA,
                       out=LessonOut, effort="low", max_tokens=4000)
        if out is None:
            continue
        session.add(Lesson(ts=int(time.time() * 1000), journal_id=e.id, strategy=e.strategy,
                           symbol=e.symbol, outcome=out.outcome, what_worked=out.what_worked,
                           mistake=out.mistake, lesson=out.lesson, tags=out.tags))
        session.commit()
        n += 1
    return n
