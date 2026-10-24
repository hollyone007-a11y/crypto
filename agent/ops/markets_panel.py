"""Market screening and candidate decisions, with manual TradingView links only."""
import json
from urllib.parse import quote
from sqlalchemy import select
from db.models import AgentState, AnalystRun, StrategyVersion


def render_markets(s,quotes):
    from ops.presentation import e,number,date,table
    row=s.get(AgentState,'universe')
    try:
        snapshot=json.loads(row.value) if row else {}
    except (ValueError,TypeError):
        snapshot={}
    selected=snapshot.get('selected',[])
    versions=list(s.scalars(select(StrategyVersion).where(StrategyVersion.status=='production')))
    rows=[]; links=[]
    for rank,item in enumerate(selected,1):
        symbol=item['symbol']; pair=quotes.get(symbol)
        price=(pair[0]+pair[1])/2 if pair else None
        qualified=[]
        for version in versions:
            evidence=version.metrics.get('wf',{}).get('per_symbol',{}).get(symbol,{})
            if evidence.get('trades',0)>=20 and evidence.get('expectancy_r',0)>0 and evidence.get('sharpe',0)>0:
                qualified.append(version.family)
        rows.append([rank,symbol,number(price,8),number(item['quote_volume']/1_000_000),
                     number(item.get('change_pct')),', '.join(qualified) or 'Проверка истории ещё не пройдена'])
        url='https://ru.tradingview.com/chart/?symbol='+quote('BINANCE:'+symbol+'.P',safe='')
        links.append(f'<a class="tag" href="{url}" target="_blank" rel="noopener noreferrer">{e(symbol)} ↗</a>')
    run=s.scalar(select(AnalystRun).where(AnalystRun.purpose=='decide').order_by(AnalystRun.ts.desc()).limit(1))
    try:
        response=json.loads(run.response) if run and run.ok else {}
    except (ValueError,TypeError):
        response={}
    decisions=[[x['symbol'],x['strategy'],x['side'],number(x['entry'],6),number(x['stop'],6),
                number(x['take'],6),x['status'],x['reason']] for x in response.get('opportunities',[])]
    return f'''<section role="tabpanel" aria-labelledby="tab-markets" id="markets" hidden>
<div class="card"><h1>Рынок и возможные сделки</h1><p class="muted">Проверено контрактов: {e(snapshot.get('screened','—'))}. В списке: {len(selected)}. Пересмотр раз в 6 часов; последний — {date(snapshot.get('updated_ts',0))}.</p>
<p class="muted small">Только криптовалютные USDT perpetual, не моложе 30 дней, оборот от 50 млн USDT за 24 часа. Акции, товары и стейблкоины исключены. Цена — свежая котировка; оборот и изменение — снимок на момент отбора. Лимиты риска общие для всех монет.</p>
{table(['#','Монета','Цена USDT','Оборот, млн USDT','24ч %, снимок','Проверенные правила'],rows)}</div>
<div class="card"><h2>Кандидаты последнего анализа</h2><p class="muted">{date(run.ts if run else 0)} · Все найденные входы и точная причина исполнения или отказа. Отсутствие кандидата означает, что условия входа не сложились.</p>
{table(['Монета','Правило','Сторона','Вход','Стоп','Цель','Решение','Причина'],decisions)}</div>
<div class="card"><h2>TradingView · ручная проверка графиков</h2><p class="muted">Ссылки для просмотра. Данные и чужие сигналы TradingView автоматически не собираются и в решения агента не поступают.</p><div class="row">{''.join(links) or '<p class="empty">Список формируется после отбора рынка.</p>'}</div></div>
<div class="card"><h2>Какие подходы исследуются</h2>{table(['Подход','Таймфрейм','Правила'],[
['Donchian / Turtle-inspired','4h','Пробой 55 свечей + EMA200 + ATR. Адаптация, не оригинальная дневная система Turtle.'],
['EMA + ADX','1h','Пересечение EMA20/50, направление EMA200 и ADX > 25; стоп и цель по ATR.'],
['Возврат к среднему','1h','Z-score и RSI в боковом режиме.'],
['Экстремальный фандинг','1h','Проверка гипотезы против перегруженного позиционирования.']])}<p class="muted small">Это публично описываемые идеи, не личные стратегии или сделки конкретного трейдера. Рейтинги доходности не заявляются. Каждый вариант проходит отдельную проверку истории.</p></div></section>'''
