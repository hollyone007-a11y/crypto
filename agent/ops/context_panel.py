"""Public-source provenance and every demo-copy decision, with explicit limitations."""
from analyst.news_risk import assess, read_state
from analyst.copy_trading import POLICY


def render(session, now, symbols):
    from ops.presentation import e, number, date, table
    external = read_state(session, 'copy_sources')
    state = read_state(session, 'copy_state')
    status = read_state(session, 'news_status')
    fresh = 0 <= now-external.get('ts', 0) <= 120_000 and bool(external.get('wallets'))
    rows = []
    for row in external.get('traders', []):
        address = row['address']
        rows.append([row['name'], address, number(row['month_roi']*100)+'%',
                     number(row['month_pnl']), number(row['week_pnl']), number(row['equity']),
                     'получены' if address in external.get('wallets', {}) else 'нет свежих данных'])
    events = list(reversed(state.get('events', [])))[:30]
    context = [assess(session, symbol, now) for symbol in symbols]
    headline_map = {}
    for item in context:
        for headline in item['headlines']:
            row = headline_map.setdefault(headline['url'], {**headline, 'symbols': []})
            row['symbols'].append(item['symbol'])
    stories = []
    for headline in sorted(headline_map.values(), key=lambda x: x['published_ts'], reverse=True)[:20]:
        url = headline['url']
        link = f'<a target="_blank" rel="noopener noreferrer" href="{e(url)}">{e(headline["title"])}</a>' if url.startswith('https://') else e(headline['title'])
        stories.append(f'<div class="card"><p>{link}</p><p class="muted small">{e(headline["source"])} · опубликовано {date(headline["published_ts"])} · получено {date(headline["fetched_ts"])} · {e(", ".join(headline["symbols"]))}</p><p>{"Найдено ключевое слово риска: временная пауза новых входов, если событие свежее и релевантное." if headline["kind"]=="crisis_keyword" else "Информационный контекст; направление цены не установлено."}</p></div>')
    blocks = [[x['symbol'], '; '.join(x['blockers'])] for x in context if x['blockers']]
    return f'''<section role="tabpanel" aria-labelledby="tab-context" id="context" hidden>
<div class="card"><h1>Трейдеры и новости · демо</h1><p><b>Источник исполнений:</b> Hyperliquid, публичный read-only API. Адаптированные сделки исполняются в локальном симуляторе Binance USDT, не на счёте источника.</p>
<p>{e(POLICY)}</p><p>Опрос сделок: 60 с; проверка очереди: 15 с. Данные: {date(external.get('ts',0))} · {'источник на связи' if fresh else 'нет свежих данных — новые копии запрещены'}. Отбор: {date(external.get('rank_ts',0))}.</p>
<p>Только новые исполнения после включения наблюдения, возраст ≤120 с, разница цены ≤0.5%, свежие новости, 200 закрытых часовых свечей. Свой стоп 1–5%, цель 2R, риск копии ≤0.1% капитала, общий предел 3 позиции. Частичное сокращение у источника закрывает всю нашу копию; таймаут 24 ч. Объём и плечо трейдера не копируются.</p>
<p class="muted">Отбор по прошлой прибыли подвержен смещению выживших. Настоящие стопы и мотивы источника неизвестны. Задержка, другая биржа и собственные выходы делают результат отличным от трейдера. Доходность не подтверждена.</p>
<p class="red">{e('; '.join(external.get('errors',[])))}</p></div>
<div class="card"><h2>Кого наблюдаем</h2>{table(['Имя','Публичный адрес','ROI месяца','PnL месяца USD','PnL недели USD','Капитал USD','Исполнения'],rows)}</div>
<div class="card"><h2>Решения по копиям</h2><p class="muted">Пустой журнал означает, что новых подходящих исполнений ещё не поступило.</p>{table(['Время','Пара','Кошелёк','Результат','Причина'],[[date(x['ts']),x.get('symbol',''),x.get('address',''),x['status'],x.get('reason','')] for x in events])}</div>
<div class="card"><h2>Новостной фильтр</h2><p>Опрос каждые 5 минут. Последняя попытка: {date(status.get('ts',0))}. Успешно: {e(', '.join(status.get('successful_sources',[])) or 'нет')}. Ошибки: {e(', '.join(status.get('failed_sources',[])) or 'нет')}.</p>
<p>Проверяются релевантные заголовки за 6 ч; риск взлома, банкротства, делистинга или отвязки — пауза входов на 2 ч от публикации. Важная статистика USD: пауза за 30 мин до и 15 мин после. Это словарный фильтр, возможны ложные срабатывания и пропуски. Новость сама по себе не создаёт вход и не доказывает причину движения.</p>
{table(['Пара','Причина паузы'],blocks)}</div>{''.join(stories) or '<div class="card">Свежих релевантных заголовков нет.</div>'}</section>'''
