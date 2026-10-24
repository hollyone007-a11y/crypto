"""Bounded, deterministic research for PAPER mode; no generated code or risk edits."""
import json
from sqlalchemy import select, func

from analyst.schema import ParamChange, ParamProposal
from db.models import AgentState, JournalEntry, Lesson, Ohlcv, StrategyVersion
from learning import pipeline
from strategies.registry import production

SIX_HOURS = 21_600_000


def set_state(s, key, value):
    row = s.get(AgentState, key)
    if row:
        row.value = value
    else:
        s.add(AgentState(key=key, value=value))
    s.commit()


def review_rules(s, now_ms, limit=50):
    reviewed = select(Lesson.journal_id)
    pending = list(s.scalars(select(JournalEntry).where(
        JournalEntry.exit_ts.is_not(None), JournalEntry.id.not_in(reviewed))
        .order_by(JournalEntry.exit_ts).limit(limit)))
    for trade in pending:
        pnl, fees = trade.pnl or 0., trade.fees or 0.
        gross = pnl + fees - (trade.funding or 0.)
        tags = ['rules_observation']
        observation = f'Результат {pnl:+.2f} USDT; комиссии {fees:.2f}; причина выхода {trade.exit_reason}.'
        hypothesis = 'Нужна серия независимых сделок; по одной сделке правила не меняются.'
        if gross > 0 and pnl <= 0:
            tags.append('costs')
            hypothesis = 'Комиссии и фандинг превратили валовую прибыль в убыток. Проверить снижение оборота на отдельной выборке.'
        elif (trade.mfe_r or 0) >= 1 and (trade.r or 0) < 0:
            tags.append('profit_giveback')
            hypothesis = 'Цена давала не менее 1R, но сделка закрылась в минус. Проверить альтернативный выход без изменения текущих стопов.'
        elif trade.exit_reason == 'stop':
            tags.append('stop')
            hypothesis = 'Стоп сработал. Убыток сам по себе не доказывает ошибку; сравнить ширину стопа на новых данных.'
        s.add(Lesson(ts=now_ms, journal_id=trade.id, strategy=trade.strategy,
                     symbol=trade.symbol, outcome='win' if pnl > 0 else ('loss' if pnl < 0 else 'flat'),
                     what_worked=observation, mistake='Причина убытка не установлена; это наблюдение, не причинный вывод.',
                     lesson=hypothesis, tags=tags))
    s.commit()
    return len(pending)


def research(s, evaluate, symbols, now_ms, clock=None, exchange=None, report_dir=None):
    from learning.lab import run_research
    return run_research(s,evaluate,symbols,now_ms,clock=clock,exchange=exchange,report_dir=report_dir)
