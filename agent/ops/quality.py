"""Observable evidence audit. Accuracy is unknown until enough independent forward outcomes exist."""
import json
from collections import Counter
from sqlalchemy import select,func
from analyst.news_risk import read_state
from db.models import AgentState,JournalEntry,Lesson,Position,PromotionEvent


def audit(session,now):
    from learning.multiframe import RULES_REVISION
    trades=list(session.scalars(select(JournalEntry).where(JournalEntry.exit_ts.is_not(None))))
    reviewed=set(session.scalars(select(Lesson.journal_id)))
    actual=[x for x in trades if (x.context or {}).get('execution_profile')]
    active=[x for x in actual if (x.context or {}).get('execution_profile',{}).get('active_demo')]
    records=[]
    for row in session.scalars(select(AgentState).where(AgentState.key.like('mtf:%'))):
        try:records.append(json.loads(row.value))
        except ValueError:continue
    valid=[x for x in records if x.get('rules_revision')==RULES_REVISION and x.get('stage')=='evaluated']
    fresh=[x for x in valid if 0<=now-x.get('checked_ts',0)<=120_000]
    score_counts=Counter(score.get('status') for item in valid for score in item.get('scores',[]))
    priority=read_state(session,'priority_research')
    profile=read_state(session,'execution_profile')
    news=read_state(session,'news_status');sources=read_state(session,'strategy_sources')
    heartbeat=session.get(AgentState,'heartbeat')
    beats=int(heartbeat.value) if heartbeat else 0
    observations=priority.get('successful_observations',[])
    healthy=bool(observations) and 0<=now-priority.get('ts',0)<=180_000 and all(0<=now-x.get('latest_closed_ts',0)<=900_000 for x in observations)
    connection=('частично: есть ошибки опроса' if priority.get('errors') else 'на связи') if healthy else ('опрос завершился без свежих данных' if priority.get('errors') else 'приоритетная проверка устарела')
    statuses=[dict(name='Binance Futures',kind='Котировки/свечи/фандинг',cadence='котировки 5с; до 8 монет за 60с: допущенные, ликвидные и ротация; остальная история — очередь',
        ts=priority.get('last_success_ts',0),status=connection)]
    statuses.append(dict(name='CoinDesk · Cointelegraph · CNBC',kind='RSS-новости',cadence='5 минут',ts=news.get('ts',0),
        status='источников: '+str(len(news.get('successful_sources',[]))) if 0<=now-news.get('ts',0)<=900_000 else 'нет свежего опроса'))
    copies=read_state(session,'copy_sources')
    statuses.append(dict(name='Hyperliquid',kind='Публичные исполнения кошельков, не стратегии',cadence='60 секунд',ts=copies.get('ts',0),
        status='кошельков на связи: '+str(len(copies.get('wallets',{}))) if 0<=now-copies.get('ts',0)<=120_000 else 'данные устарели'))
    for key,value in sources.get('sources',{}).items():
        statuses.append(dict(name=key,kind='Статическое описание метода',cadence='проверка изменения раз в сутки',ts=value.get('ts',0),
            status='недоступен; правила не обновлены' if value.get('status')=='unavailable' else 'изменился; нужна отдельная проверка' if value.get('status')=='changed' else 'документ проверен; не live-сигнал'))
    result=dict(generated_ts=now,accuracy_pct=None,accuracy_reason='Точность будущих сигналов не установлена: закрытых сделок нового профиля недостаточно.',
        closed_total=len(trades),new_profile_closed=len(actual),active_closed=len(active),reviewed=sum(x.id in reviewed for x in trades),
        missing_reviews=sum(x.id not in reviewed for x in trades),net_closed=round(sum(x.pnl or 0 for x in trades),4),
        evaluated_current_revision=len(valid),evaluated_fresh=len(fresh),contracts_observed=len(records),
        studies=sum(score_counts.values()),qualified_studies=score_counts.get('paper_trial',0),
        insufficient_studies=score_counts.get('insufficient_sample',0),rejected_studies=score_counts.get('rejected',0),
        source_errors=sum(x.get('stage')=='error' for x in records),priority=priority,feeds=statuses,
        heartbeat_age_s=max(0,(now-beats)//1000),active_demo_enabled=profile.get('active_demo_enabled',False),
        active_limits='5м/15м; цель ближе на 25%; выход 20/45 мин; риск ≤0.5%; 1 открытая активная позиция, 2 входа за UTC-день.',
        caveats=['5м OHLC — приближение, не тиковая точность.','Исторические новости и общий портфель не воспроизводятся.',
                 'Confidence в сигнале — фиксированная оценка правила, не откалиброванная вероятность прибыли.',
                 'Перебор стратегий/ТФ создаёт смещение выбора; нужна независимая будущая выборка.',
                 'Подробный отчёт об ошибке не доказывает, что изменение стратегии улучшило результат.'])
    return result


def panel(session,now):
    from ops.presentation import e,date
    from ops.mobile_dashboard import table
    item=audit(session,now)
    return f'''<div class="card accent" id="quality-audit"><div class="row"><h2>Качество анализа</h2><span class="tag">Точность пока не установлена</span></div>
<p>Закрытых сделок: <b>{item['closed_total']}</b> · нового профиля: <b>{item['new_profile_closed']}</b> · активных: <b>{item['active_closed']}</b> · разобрано: <b>{item['reviewed']}</b> · без разбора: <b>{item['missing_reviews']}</b>.</p>
<p><b>Почему нет новых входов:</b> допуск получили {item['qualified_studies']} из {item['studies']} вариантов. Недостаточно сделок для проверки: {item['insufficient_studies']}; тест не пройден: {item['rejected_studies']}.</p>
<p class="small muted">Свежий анализ сигналов: {item['evaluated_fresh']} из {item['evaluated_current_revision']}. История перепроверяется раз в 6 часов, её оценки хранятся отдельно от текущего сигнала. Ошибок источника в очереди: {item['source_errors']}. Процент прибыльных сделок на маленькой выборке не измеряет точность прогноза.</p>
<details id="quality-sources"><summary>Откуда данные и насколько они свежие</summary>{table(['Источник','Что это','Обновление','Последняя проверка','Статус'],[[x['name'],x['kind'],x['cadence'],date(x['ts']),x['status']] for x in item['feeds']])}</details></div>
<div class="card"><h2>Активное демо · {'включено' if item['active_demo_enabled'] else 'отключено'}</h2><p>{e(item['active_limits'])}</p><p class="small muted">Только после тех же проверок: 20+ train и test, положительный net, t≥3. Это эксперимент, улучшение результата не подтверждено.</p></div>'''
