"""Compact task-first dashboard. Every data-derived value is escaped; zero external assets."""
import hashlib
import json
import time
from sqlalchemy import select, func
from analyst.news_risk import read_state
from config.settings import ROOT
from db.models import AgentState, AnalystRun, EquitySnapshot, JournalEntry, Lesson, News, PaperAccount, Position, RiskEvent
from strategies.public_methods import METHODS,TIMEFRAMES


def table(headings,rows):
    from ops.presentation import e
    body=''.join('<tr>'+''.join(f'<td data-label="{e(title)}">{e(value)}</td>' for title,value in zip(headings,row))+'</tr>' for row in rows)
    return '<div class="scroll"><table><thead><tr>'+''.join('<th>'+e(x)+'</th>' for x in headings)+'</tr></thead><tbody>'+(body or f'<tr><td colspan="{len(headings)}" class="empty">Пока нет записей</td></tr>')+'</tbody></table></div>'


def section(id,title,body):
    digest=hashlib.sha256(body.encode()).hexdigest()[:16]
    return f'<section id="{id}" role="tabpanel" aria-labelledby="tab-{id}" data-content-version="{digest}" hidden><div class="section-heading"><h1>{title}</h1></div>{body}</section>'


def coverage(session):
    from learning.multiframe import RULES_REVISION
    result={}
    for row in session.scalars(select(AgentState).where(AgentState.key.like('mtf:%'))):
        try:
            value=json.loads(row.value)
            if value.get('stage')=='evaluated' and value.get('rules_revision')!=RULES_REVISION:
                value={**value,'stage':'history','scores':[],'reason':'Изменилась версия правил; ожидается повторная проверка.'}
            result[row.key[4:]]=value
        except ValueError: continue
    return result


def market_panel(session,items,catalog,scan,now):
    from ops.trading_research import chart_link
    from ops.presentation import e,number,date
    rows=[]
    labels={'history':'История','evaluated':'Проверено','error':'Ошибка источника','filtered':'Наблюдение'}
    for contract in catalog.get('contracts',[]):
        symbol=contract['symbol'];record=items.get(symbol,{})
        scores=record.get('scores',[]);ready=[x for x in scores if x['status']=='paper_trial']
        best=max(scores,key=lambda x:(x['test']['trades'],x['test']['expectancy_r'])) if scores else None
        details='; '.join(f"{x['method']}/{x['timeframe']} ({x.get('policy','balanced')}): {x['test']['trades']} test, {x['test']['expectancy_r']:+.3f}R, {x['status']}" for x in scores)
        rows.append(f'''<tr data-market-row data-symbol="{e(symbol)}" data-stage="{e(record.get('stage','queue'))}" data-ready="{str(bool(ready)).lower()}">
<td data-label="Монета"><strong>{e(symbol.removesuffix('USDT'))}</strong><small>USDT perpetual · <a href="{e(chart_link(symbol))}" rel="noopener noreferrer" target="_blank">TradingView ↗</a></small></td>
<td data-label="Цена" data-market-price="{e(symbol)}">{number(contract.get('last'),6)}</td>
<td data-label="Объём 24ч">{number(contract.get('quote_volume',0)/1_000_000,1)} млн</td>
<td data-label="Статус"><span class="tag">{'Демокандидат' if ready else e(labels.get(record.get('stage'),'В очереди'))}</span></td>
<td data-label="Сравнение ТФ"><details><summary>{e((best['timeframe']+' · '+str(best['test']['trades'])+' test') if best else 'Ожидание данных')}</summary><p>{e(record.get('reason','Проверка ещё не выполнена.'))}</p><p class="small muted">{e(details)}</p><p class="small muted">{date(record.get('checked_ts',0))}</p></details></td></tr>''')
    done=len(items);evaluated=sum(x.get('stage')=='evaluated' for x in items.values())
    priority=read_state(session,'priority_research');discovery=read_state(session,'market_discovery')
    young=discovery.get('young_contracts',[])
    discovery_html=f'''<div class="card" id="market-discovery"><h2>Поиск новых монет</h2><p>Быстрая очередь: до {e(priority.get('batch_limit',8))} контрактов за цикл, включая ротацию. Посещено уникальных монет: {len(priority.get('visited_symbols',[]))}. Последняя проверка: {date(priority.get('ts',0))}.</p><p class="small muted">Последний проход: {e(', '.join(priority.get('attempted_symbols',[])) or 'ожидается')}. Допущенные стратегии получают приоритет. Каталог обновляется каждую минуту; метаданные биржи — каждые 10 минут.</p><p>Молодых контрактов в наблюдении: {len(young)}. Для действующего теста нужны 38 дней истории; новый листинг не означает готовый вход.</p>{table(['Монета','Возраст, дней','Оборот 24ч, млн USDT','24ч %, снимок'],[[x['symbol'],number(max(0,now-x.get('listed_ts',now))/86_400_000,1),number(x.get('quote_volume',0)/1_000_000,1),number(x.get('change_pct'))] for x in young[:12]])}</div>'''
    controls='''<div class="filters" data-sticky-controls><label>Монета<input id="market-search" type="search" placeholder="BTC, ETH, SOL…" autocomplete="off"></label><label>Статус<select id="market-filter"><option value="all">Все</option><option value="ready">Демокандидаты</option><option value="evaluated">Проверены</option><option value="history">Накопление истории</option><option value="queue">В очереди</option><option value="filtered">Наблюдение</option><option value="error">Ошибка источника</option></select></label></div>'''
    return discovery_html+f'''<div class="metric-strip"><span><b>{len(catalog.get('contracts',[]))}</b> контрактов</span><span><b>{done}</b> получили проверку</span><span><b>{evaluated}</b> с полным сравнением</span></div>
<div class="card"><div class="row"><h2>Все USDT-фьючерсы</h2><span class="tag">5м · 15м · 1ч · 4ч</span></div>
<p class="small muted">Цены видимых монет — live mark price. Общий список — снимок {date(catalog.get('ts',0))}. История сканируется по очереди: {e(scan.get('batch_size',0))} монет за проход.</p>{controls}
<div class="scroll"><table class="market-table"><thead><tr><th>Монета</th><th>Цена</th><th>Объём 24ч</th><th>Статус</th><th>Сравнение ТФ</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<p id="market-empty" class="empty" hidden>По запросу ничего не найдено</p><div class="pager"><button id="market-prev" type="button">← Назад</button><span id="market-page" aria-live="polite"></span><button id="market-next" type="button">Далее →</button></div></div>'''


def laboratory(session,items,scan,now):
    from ops.presentation import e,number,date
    from ops.live_view import report_panel
    complete=[x for x in items.values() if x.get('stage')=='evaluated' and len(x.get('scores',[]))==len(METHODS)*6
              and now-x.get('checked_ts',0)<24*3_600_000]
    # Match identical cutoff windows, not a mixture of differently dated/winning cohorts.
    latest=max((x['cutoff_ts'] for x in complete),default=0)
    paired=[x for x in complete if x['cutoff_ts']==latest]
    rows=[]
    comparisons=[('5m','balanced'),('5m','active'),('15m','balanced'),('15m','active'),('1h','balanced'),('4h','balanced')]
    for tf,policy in comparisons:
        scores=[score for item in paired for score in item['scores'] if score['timeframe']==tf and score.get('policy','balanced')==policy]
        samples=sum(x['test']['trades'] for x in scores)
        mean=sum(x['test']['expectancy_r']*x['test']['trades'] for x in scores)/samples if samples else None
        rows.append([tf+(' · активный' if policy=='active' else ' · базовый'),len(paired),samples,number(mean,3)+' R' if mean is not None else '—',
                     sum(x['status']=='paper_trial' for x in scores),'Исследование' if scores else 'Сбор истории'])
    stages={key:sum(x.get('stage')==key for x in items.values()) for key in ('history','evaluated','filtered','error')}
    lessons=list(session.scalars(select(Lesson).order_by(Lesson.ts.desc()).limit(8)))
    lesson_cards=''.join(f'<details class="card" id="lesson-{x.id}"><summary><strong>{e(x.symbol)} · {e(x.outcome)}</strong><span class="small muted">{date(x.ts)}</span></summary><p>{e(x.what_worked)}</p><p><b>Вывод:</b> {e(x.mistake)}</p><p><b>Проверить:</b> {e(x.lesson)}</p></details>' for x in lessons)
    source_checks=read_state(session,'strategy_sources').get('sources',{})
    methods=''.join(f'<details class="card" id="method-{key}"><summary><strong>{e(value["name"])}</strong><span class="small muted">{e(value["author"])}</span></summary><p>{e(value["rules"])}</p><p class="muted small">{e(value["scope"])}</p><p class="small muted">Проверка источника: {date(source_checks.get(key,{}).get("ts",0))} · {e(source_checks.get(key,{}).get("reason","Ежедневная проверка ожидается."))}</p><a target="_blank" rel="noopener noreferrer" href="{e(value["url"])}">Первичный источник ↗</a></details>' for key,value in METHODS.items())
    from ops.quality import panel as quality_panel
    from ops.trading_research import panel as trader_panel
    from ops.signal_panel import panel as signal_panel
    return signal_panel(session)+quality_panel(session,now)+trader_panel(session)+f'''<div class="card accent"><span class="eyebrow">Текущая работа</span><h2>{'Проверяю стратегии на новых данных' if complete else 'Накопление общей истории для {len(METHODS)*6} гипотез'}</h2><p>История: {stages['history']} · сравнение готово: {stages['evaluated']} · наблюдение без входа: {stages['filtered']} · ошибок: {stages['error']}.</p><p class="small muted">Далее: 38 дней общей истории → 14 дней train → пауза 1ч → 14 дней test → экспериментальное демо. При 10 убыточных по сумме последних демосделках вход в эту пару/ТФ/метод приостанавливается.</p></div>
<div class="card"><h2>На каком ТФ лучше</h2><p class="small muted">Одинаковое окно {date(latest-14*86_400_000) if latest else '—'} — {date(latest)}; одна и та же группа монет и все методы. Минусовые и нулевые результаты включены. Без полной истории победитель не объявляется.</p>{table(['ТФ','Монет','Test-сделок','Средний net R','Демокандидатов','Этап'],rows)}<a class="button" href="/research-report.txt" download="research-report.txt">Скачать отчёт TXT ↗</a></div>
<details class="card" id="replay-method"><summary>Как проводится проверка</summary><p>Сигналы 5м/15м/1ч/4ч исполняются после закрытия свечи. Выходы повторяются на 5-минутных OHLC со стопом первым при двойном касании; это приближение, не тиковая точность. Профиль: залог 10%, 10×, выход 30/60 мин, риск 0.5%, комиссии 0.055% на сторону, проскальзывание 2 bps, спред 1 bps.</p><p>Исторические новости и общая книга портфеля не воспроизводятся. В активном replay максимум два входа за день на отдельный прогон; конкуренция разных монет за общий слот не моделируется. Минимум 20 train и 20 test, положительный чистый результат и t≥3. Затем только экспериментальная демоторговля; принятие базовых стратегий по этим тестам не разблокируется.</p></details>
<h2>Разбор ошибок</h2>{lesson_cards or '<div class="card empty">Разбор появится после закрытия сделки.</div>'}
<h2>Стратегии из первичных источников</h2>{methods}
<details class="card" id="baseline-research"><summary>Базовые исследования и полный отчёт</summary>{report_panel(session)}</details>'''


def render(s,mode,equity,blockers,market_source='',quotes=None,limits=None):
    from ops.presentation import e,number,date,position_card,spark
    quotes=quotes or {};now=int(time.time()*1000)
    heartbeat=s.get(AgentState,'heartbeat');heartbeat=int(heartbeat.value) if heartbeat else 0
    positions=list(s.scalars(select(Position).where(Position.status=='open')))
    journals={x.position_id:x for x in s.scalars(select(JournalEntry).where(JournalEntry.exit_ts.is_(None)))}
    closed=list(s.scalars(select(JournalEntry).where(JournalEntry.exit_ts.is_not(None)).order_by(JournalEntry.exit_ts.desc()).limit(200)))
    count,total,fees=s.execute(select(func.count(),func.sum(JournalEntry.pnl),func.sum(JournalEntry.fees)).where(JournalEntry.exit_ts.is_not(None))).one()
    account=s.get(PaperAccount,1);net=equity-account.initial if account and equity is not None else None
    snapshots=list(s.scalars(select(EquitySnapshot).order_by(EquitySnapshot.ts.desc()).limit(300)))[::-1]
    run=s.scalar(select(AnalystRun).where(AnalystRun.purpose=='decide',AnalystRun.ok==True).order_by(AnalystRun.ts.desc()).limit(1))
    try: decision=json.loads(run.response) if run else {}
    except ValueError: decision={}
    profile=read_state(s,'execution_profile');catalog=read_state(s,'market_catalog');scan=read_state(s,'mtf_scan');items=coverage(s)
    active={x['symbol'] for x in catalog.get('contracts',[])}
    if active: items={key:value for key,value in items.items() if key in active}
    executor=read_state(s,'mtf_decisions')
    execution_status=f'<div class="card" id="execution-status"><h2>Проверка входов</h2><p>{e(executor.get("reason","Ожидание первого цикла исполнения."))}</p><p class="small muted">Последняя проверка: {date(executor.get("ts",0))}. Отсутствие допущенного сигнала не означает остановку процесса.</p></div>'
    position_html=execution_status+(''.join(position_card(p,quotes.get(p.symbol),journals.get(p.id)) for p in positions) or '<div class="card empty"><strong>Открытых сделок нет</strong><p>Агент ждёт проверенный сигнал.</p></div>')
    risks=list(s.scalars(select(RiskEvent).where(RiskEvent.kind!='approve').order_by(RiskEvent.ts.desc()).limit(15)))
    rules=limits or {}
    risk_details=table(['Правило','Значение'],[
        ['Риск на вход',str(rules.get('max_risk_per_trade_pct','—'))+'%'],
        ['Максимальное плечо',str(rules.get('max_leverage','—'))+'×'],
        ['Одновременно позиций',rules.get('max_open_positions','—')],
        ['Лимит потери за день',str(rules.get('daily_loss_limit_pct','—'))+'%'],
        ['Предел просадки',str(rules.get('max_drawdown_pct','—'))+'%'],['Защитный стоп','Обязателен']])+table(['Время','Монета','Причина'],[[date(x.ts),x.symbol,'; '.join(x.detail.get('reasons',[])) or x.detail.get('reason',x.kind)] for x in risks])
    overview=f'''<div class="kpis"><div class="card balance"><span class="eyebrow">Капитал · USDT</span><b>{number(equity)}</b><small>Баланс {number(account.cash if account else None)}</small></div><div class="card"><span class="eyebrow">Результат с начала</span><b class="{'green' if net is not None and net>=0 else 'red'}">{number(net)}</b><small>включая открытые позиции</small></div><div class="card"><span class="eyebrow">Закрытые сделки</span><b>{count}</b><small>Чистый итог {number(total or 0)} USDT</small></div></div>
<div class="columns"><div class="card"><div class="row"><h2>Капитал</h2><span class="muted small">USDT · демо</span></div>{spark([x.equity for x in snapshots])}</div><div class="card"><span class="eyebrow">Последнее решение · {date(run.ts if run else 0)}</span><h2>{'Пауза новых входов' if blockers else 'Агент наблюдает рынок'}</h2><p>{e('; '.join(blockers) or decision.get('thesis','Ожидание анализа.'))}</p><a class="button" href="#positions">Открытые сделки · {len(positions)} ↗</a></div></div>
<details class="card" id="risk-details"><summary>Профиль и ограничения</summary><p>10× · залог 10% капитала · номинал 100% капитала. Базовый режим: до 60 мин, проверка прогресса через 30 мин. Активный эксперимент: до 45 мин, проверка через 20 мин; максимум 1 активная позиция и 2 входа за день. Риск на вход ≤0.5%. Лимит времени также применяется к ранее открытым позициям.</p>{risk_details}</details>'''
    recent=read_state(s,'mtf_decisions').get('events',[])+read_state(s,'copy_state').get('events',[])
    recent=sorted(recent,key=lambda x:x['ts'],reverse=True)[:30]
    news=list(s.scalars(select(News).where(News.published_ts>now-6*3_600_000,News.published_ts<=now).order_by(News.published_ts.desc()).limit(6)))
    news_html=''.join(f'<p><a href="{e(x.url)}" target="_blank" rel="noopener noreferrer">{e(x.title)}</a><small>{e(x.source)} · {date(x.published_ts)}</small></p>' for x in news if x.url.startswith('https://'))
    sources=read_state(s,'copy_sources');news_status=read_state(s,'news_status')
    history=f'''<div class="card"><h2>Последние закрытые сделки</h2>{table(['Выход','Монета','Сторона','Net USDT','R','Причина'],[[date(x.exit_ts),x.symbol,x.side,number(x.pnl),number(x.r),x.exit_reason] for x in closed[:30]])}</div><details class="card" id="decision-journal"><summary>Сигналы и отказы</summary>{table(['Время','Монета','ТФ','Решение','Почему'],[[date(x['ts']),x.get('symbol',''),x.get('timeframe','—'),x['status'],x.get('reason','')] for x in recent])}</details>
<details class="card" id="news-sources"><summary>Новости и копирование</summary><p class="small muted">Новостной фильтр: {date(news_status.get('ts',0))} · источников {len(news_status.get('successful_sources',[]))}. Сообщения о кризисе и важные события USD могут приостановить вход; заголовок не задаёт направление сделки.</p>{news_html}<h2>Публичные кошельки Hyperliquid</h2><p class="small muted">Отбор по историческим результатам, не доказанный навык. Данные {date(sources.get('ts',0))}. Ошибки: {e('; '.join(sources.get('errors',[])) or 'нет')}.</p>{table(['Адрес','ROI месяца','PnL недели USD'],[[x['address'],number(x['month_roi']*100)+'%',number(x['week_pnl'])] for x in sources.get('traders',[])])}</details>'''
    sections=section('overview','Обзор',overview)+section('positions','Открытые сделки',position_html)+section('markets','Рынок',market_panel(s,items,catalog,scan,now))+section('learning','Лаборатория',laboratory(s,items,scan,now))+section('history','Журнал',history)
    tabs=[('overview','Обзор','◫'),('positions','Сделки','↗'),('markets','Рынок','≋'),('learning','Лаборатория','⌘'),('history','Журнал','≡')]
    nav=''.join(f'<button role="tab" id="tab-{id}" aria-controls="{id}" data-tab="{id}" aria-selected="false"><span aria-hidden="true">{icon}</span>{title}</button>' for id,title,icon in tabs)
    css=(ROOT/'ops'/'mobile.css').read_text(encoding='utf-8');script=(ROOT/'ops'/'live.js').read_text(encoding='utf-8')+'\n'+(ROOT/'ops'/'mobile.js').read_text(encoding='utf-8')
    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#0b0e14"><title>Futures Agent · Observatory</title><style>{css}</style></head><body>
<header><div class="topbar"><a class="brand" href="#overview">FUTURES<span>AGENT</span></a><span class="tag paper">PAPER</span><span id="connection" class="live-status" data-heartbeat="{heartbeat}">Агент на связи</span></div><div class="header-meta">10× <i>·</i> залог 10% <i>·</i> 5м / 15м / 1ч / 4ч</div><nav role="tablist" aria-label="Разделы">{nav}</nav></header>
<main><div id="live-clocks" class="syncbar" data-generated="{now}" data-heartbeat="{heartbeat}"><span id="market-live-status">Подключение цен…</span><span id="agent-live-status">Агент: проверка…</span><span id="sync-live-status">Снимок: проверка…</span></div>{sections}<p class="footer">Демосчёт · реальные средства не подключены · доходность не гарантирована. Время UTC.</p></main><script>{script}</script></body></html>'''
