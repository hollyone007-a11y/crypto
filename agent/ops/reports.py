"""Plain-text status and daily report (Telegram) built from the DB."""
from datetime import UTC, datetime

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from db.models import AnalystRun, EquitySnapshot, JournalEntry, Lesson, PaperAccount, Position

DAY_MS = 86_400_000


def _fmt_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%m-%d %H:%M")


def status_text(s: Session, equity: float | None, mode: str, blockers: list[str]) -> str:
    acc = s.get(PaperAccount, 1)
    lines = [f"Mode: {mode.upper()}",
             f"Equity: {equity:.2f} USDT" if equity is not None else "Equity: n/a (no fresh quotes)",
             f"Cash: {acc.cash:.2f}" if acc else "Cash: n/a",
             "Trading: " + ("BLOCKED - " + "; ".join(blockers) if blockers else "allowed")]
    pos = s.scalars(select(Position).where(Position.status == "open")).all()
    lines.append(f"Open positions: {len(pos)}")
    for p in pos:
        lines.append(f"  {p.symbol} {p.side} {p.qty:.4f} @ {p.entry:.2f} stop {p.stop:.2f} "
                     f"take {p.take if p.take is None else round(p.take, 2)} [{p.strategy}]")
    run = s.scalar(select(AnalystRun).where(AnalystRun.purpose == "decide")
                   .order_by(desc(AnalystRun.ts)).limit(1))
    if run:
        lines.append(f"Last analyst run: {_fmt_ts(run.ts)} UTC ({'ok' if run.ok else 'failed'})")
    return "\n".join(lines)


def daily_report(s: Session, now_ms: int, equity: float | None, mode: str,
                 blockers: list[str]) -> str:
    since = now_ms - DAY_MS
    closed = s.scalars(select(JournalEntry).where(JournalEntry.exit_ts >= since)).all()
    snap = s.scalar(select(EquitySnapshot).where(EquitySnapshot.ts <= since)
                    .order_by(desc(EquitySnapshot.ts)).limit(1))
    lines = ["Daily report (last 24h)", status_text(s, equity, mode, blockers)]
    if snap and equity is not None:
        lines.append(f"24h PnL: {equity - snap.equity:+.2f} ({100 * (equity / snap.equity - 1):+.2f}%)")
    lines.append(f"Closed trades: {len(closed)}, net {sum(e.pnl or 0 for e in closed):+.2f}, "
                 f"sum R {sum(e.r or 0 for e in closed):+.2f}")
    for e in closed[:10]:
        lines.append(f"  {e.symbol} {e.side} {e.exit_reason} {e.pnl:+.2f} ({e.r:+.2f}R) [{e.strategy}]")
    lessons = s.scalars(select(Lesson).where(Lesson.ts >= since)).all()
    for l in lessons[:5]:
        lines.append(f"  lesson: {l.lesson}")
    return "\n".join(lines)
