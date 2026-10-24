"""Weekly review: Opus aggregates lessons + stats and proposes candidates.
It can ONLY propose parameter changes (bounded) or a new strategy file;
promotion is decided by learning.pipeline, never by the model."""
import json
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from analyst.llm import LLM, WEEKLY_MODEL
from analyst.schema import WEEKLY_SCHEMA, WeeklyReview
from db.models import JournalEntry, Lesson, StrategyVersion
from learning import pipeline
from strategies.registry import label, seed

WEEK_MS = 7 * 86_400_000

SYSTEM = """You run the weekly review of an automated crypto-futures paper-trading system.
Input: this week's per-trade lessons, per-strategy statistics and current production params.
Output: a short summary and AT MOST 3 parameter proposals and AT MOST 1 new strategy.

Hard constraints (proposals violating them are discarded automatically):
- Parameter changes: only existing param names, each new value within 0.5x..2x of current.
- Do not chase the last week's noise: small samples are not evidence. Proposing nothing is fine.
- Every proposal is backtested out-of-sample and paper-traded for 2 weeks before it can go live;
  aim for robustness (Sharpe, drawdown), not for fitting recent trades.
- A new strategy is a single Python file: imports only from math, numpy, pandas, dataclasses,
  typing, research.features, strategies.base. Define one @dataclass class subclassing Strategy
  with name/timeframe/params/max_hold_bars fields and signals(self, df, funding=None) returning
  strategies.base.empty_signals(df) filled with side (+1/-1/0), stop, take, confidence, rationale.
  Signals at bar i may only use data up to bar i (no shift(-k), no centered windows).
  No file/network/OS access, no eval/exec, no dunder access, no while/try/with."""


def weekly_review(session: Session, llm: LLM, evaluate: pipeline.Evaluator,
                  now_ms: int | None = None) -> dict:
    now_ms = now_ms or int(time.time() * 1000)
    seed(session)
    lessons = session.scalars(select(Lesson).where(Lesson.ts >= now_ms - WEEK_MS)).all()
    closed = session.scalars(select(JournalEntry).where(
        JournalEntry.exit_ts >= now_ms - 4 * WEEK_MS, JournalEntry.r.is_not(None))).all()
    stats: dict[str, dict] = {}
    for e in closed:
        st = stats.setdefault(e.strategy, {"trades": 0, "sum_r": 0.0, "wins": 0})
        st["trades"] += 1
        st["sum_r"] = round(st["sum_r"] + e.r, 3)
        st["wins"] += e.r > 0
    prods = session.scalars(select(StrategyVersion).where(StrategyVersion.status == "production")).all()
    payload = {
        "lessons_this_week": [{"strategy": l.strategy, "symbol": l.symbol, "outcome": l.outcome,
                               "mistake": l.mistake, "lesson": l.lesson, "tags": l.tags}
                              for l in lessons],
        "stats_last_4_weeks": stats,
        "production": [{"family": p.family, "version": label(p), "kind": p.kind, "params": p.params,
                        "oos": p.metrics.get("wf")} for p in prods],
    }
    review = llm.call(purpose="weekly", model=WEEKLY_MODEL, system=SYSTEM,
                      user=json.dumps(payload, default=str), schema=WEEKLY_SCHEMA,
                      out=WeeklyReview, effort="high", max_tokens=16000)
    created = []
    if review is not None:
        for p in review.param_proposals:
            v = pipeline.propose_params(session, p)
            if v:
                created.append(label(v))
        for p in review.code_proposals:
            v = pipeline.propose_code(session, p)
            if v:
                created.append(label(v))
    decisions = pipeline.advance(session, evaluate, now_ms)
    return {"summary": review.summary if review else None, "created": created,
            "decisions": decisions}
