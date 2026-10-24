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


def research(s, evaluate, symbols, now_ms):
    lessons = review_rules(s, now_ms)
    previous = s.get(AgentState, 'research_last_run')
    if previous and now_ms-int(previous.value) < SIX_HOURS:
        return {'status':'not_due','lessons':lessons}
    fresh = s.scalar(select(func.max(Ohlcv.ts)).where(Ohlcv.timeframe=='1h', Ohlcv.symbol.in_(symbols)))
    if not fresh or now_ms-fresh > 7_200_000:
        result={'status':'ожидание свежих данных','finished_ts':now_ms,'summary':'История устарела; новые гипотезы не создавались.'}
        set_state(s,'research_status',json.dumps(result,ensure_ascii=False))
        return result
    proposed = 0
    cache={}
    def cached_evaluate(version,start):
        key=(version.id,start)
        if key not in cache:
            cache[key]=evaluate(version,start)
        return cache[key]
    # Refresh baseline evidence for the current universe, even when the bounded
    # hypothesis budget has been exhausted. Scores never certify unseen coins.
    for parent,_ in production(s):
        evidence=cached_evaluate(parent,None)
        parent.metrics={**parent.metrics,'wf':evidence,
                        'wf_symbols':evidence.get('symbols',list(symbols)),
                        'wf_checked_ts':now_ms}
    s.commit()
    for parent, strategy in production(s):
        if parent.family not in pipeline.FAMILIES:
            continue
        pending = s.scalar(select(StrategyVersion.id).where(
            StrategyVersion.family==parent.family, StrategyVersion.status.in_(['candidate','paper_trial'])).limit(1))
        if pending:
            continue
        # Only two predeclared, small hypotheses per parent version. This is not
        # an unbounded optimizer repeatedly searching a reused validation set.
        search_keys = ['stop_atr'] + [k for k in ('channel','window','lookback') if k in parent.params]
        previous_params = [v.params for v in s.scalars(select(StrategyVersion).where(StrategyVersion.parent_id==parent.id))]
        for key in search_keys:
            if key not in parent.params:
                continue
            value = float(parent.params[key])*1.1
            if key in pipeline.INT_PARAMS:
                value = round(value)
            expected = dict(parent.params, **{key:value})
            if expected in previous_params:
                continue
            proposal = ParamProposal(family=parent.family,
                changes=[ParamChange(name=key,value=value)],
                rationale=f'Предзаданный исследовательский вариант: {key} +10%. Требует OOS и новых данных; преимущество не доказано.')
            if pipeline.propose_params(s,proposal):
                proposed += 1
            break
    decisions = pipeline.advance(s,cached_evaluate,now_ms)
    for (version_id,start),evidence in cache.items():
        if start is None:
            version=s.get(StrategyVersion,version_id)
            version.metrics={**version.metrics,'wf_symbols':evidence.get('symbols',list(symbols)),
                             'wf_checked_ts':now_ms}
    s.commit()
    result={'status':'исследование завершено','finished_ts':now_ms,'proposed':proposed,'lessons':lessons,
            'summary':f'Разборов: {lessons}; новых гипотез: {proposed}. '+(' '.join(decisions) if decisions else 'Новых доказательств для смены правил нет.')}
    set_state(s,'research_last_run',str(now_ms))
    set_state(s,'research_status',json.dumps(result,ensure_ascii=False))
    return result
