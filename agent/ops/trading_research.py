"""Human-visible chart references and independently sourced execution observations."""
import json
from urllib.parse import quote
from sqlalchemy import select
from db.models import AgentState
from analyst.news_risk import read_state


def chart_link(symbol):
    return 'https://www.tradingview.com/chart/?symbol='+quote('BINANCE:'+symbol+'.P',safe='')


def panel(session):
    from ops.presentation import e,date,number
    from ops.mobile_dashboard import table
    states=[]
    source=read_state(session,'copy_sources')
    addresses=list(dict.fromkeys([x['address'] for x in source.get('traders',[])]+list(source.get('wallets',{}))))[:6]
    query=select(AgentState).where(AgentState.key.in_(['trader_observations:'+x for x in addresses])) if addresses else select(AgentState).where(AgentState.key.like('trader_observations:%')).limit(6)
    for row in session.scalars(query):
        try:states.append(json.loads(row.value))
        except ValueError:continue
    states=sorted(states,key=lambda x:-x.get('last_poll_ts',0))[:6]
    summary=table(['Кошелёк','Исполнений','Закрывающих исполнений','Reported closedPnL','Неполных окон / пауз'],[
        [x['address'],x.get('fill_count',0),x.get('closing_fill_count',0),number(x.get('reported_closed_pnl',0)),x.get('coverage_gaps',0)] for x in states])
    records=sorted([dict(address=x['address'],**fill) for x in states for fill in x.get('records',[])],key=lambda x:-x['source_ts'])[:25]
    detailed=table(['Время','Кошелёк','Монета','Действие','Цена','Reported closedPnL'],[
        [date(x['source_ts']),x['address'],x['coin'],x['direction'],number(x['price'],6),number(x['reported_closed_pnl'])] for x in records])
    links=' '.join(f'<a class="button" rel="noopener noreferrer" target="_blank" href="{e(chart_link(symbol))}">{symbol[:-4]} · TradingView ↗</a>' for symbol in ['BTCUSDT','ETHUSDT','SOLUSDT'])
    return f'''<details class="card" id="tradingview-research"><summary>TradingView · просмотр и источники</summary><p>Графики открываются для просмотра. Автоматический сбор TradingView, торговые webhooks и импорт его рыночных данных не подключены: стандартные условия ограничивают такие применения.</p>{links}<p class="small muted">Strategy Tester показывает моделируемые сделки. Публикация автора и популярность не подтверждают реальную прибыль и не раскрывают полную историю счёта.</p><a href="https://www.tradingview.com/policies/" target="_blank" rel="noopener noreferrer">Условия TradingView ↗</a></details>
<details class="card" id="trader-observation-report"><summary>Что действительно делают наблюдаемые трейдеры</summary><p>Источник: публичные исполнения Hyperliquid. Топ по историческому фильтру — не подтверждённый навык. Стратегия трейдера неизвестна: по исполнению её нельзя установить.</p>{summary}<p class="small muted">Reported closedPnL — поле источника, не чистая прибыль счёта: полная история, комиссии разных feeToken и фандинг не сопоставлены. Доступны только запрошенные окна исполнений; пропуски опроса отмечаются.</p>{detailed}</details>'''
