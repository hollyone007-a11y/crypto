"""Read-only dashboard; all persisted/external text is escaped."""
import json
import time
from html import escape
from datetime import UTC, datetime

from sqlalchemy import Integer, select, func
from db.models import (AgentState, AnalystRun, EquitySnapshot, JournalEntry, Lesson,
                       PaperAccount, Position, PromotionEvent, RiskEvent, StrategyVersion)


def e(value):
    return escape(str(value if value is not None else "—"))


def number(value, digits=2):
    return "—" if value is None else f"{value:,.{digits}f}"


def date(ms):
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%d.%m %H:%M UTC") if ms else "—"


def table(headings, rows):
    return '<div class="scroll"><table><thead><tr>'+''.join('<th>'+e(h)+'</th>' for h in headings)+'</tr></thead><tbody>'+(''.join('<tr>'+''.join('<td>'+e(c)+'</td>' for c in row)+'</tr>' for row in rows) or '<tr><td colspan="20" class="muted">Данных пока нет. Ничего не придумано.</td></tr>')+'</tbody></table></div>'


def spark(values):
    if len(values) < 2:
        return '<p class="muted">Кривая появится после нескольких снимков капитала.</p>'
    lo, hi = min(values), max(values)
    points=' '.join(f'{i*1000/(len(values)-1):.2f},{150-(v-lo)/(hi-lo or 1)*130:.2f}' for i,v in enumerate(values))
    return f'<svg viewBox="0 0 1000 170" role="img" aria-label="История виртуального капитала"><polyline points="{points}" fill="none" stroke="#57d5ad" stroke-width="2"/></svg>'


def evidence_text(metrics):
    wf=metrics.get('wf',{})
    if not wf:
        return 'История ещё не проверена'
    text=f"История: {wf.get('trades',0)} сделок; средний результат {number(wf.get('expectancy_r'),3)} R; Sharpe {number(wf.get('sharpe'))}; просадка {number(wf.get('max_dd_pct'))}%."
    forward=metrics.get('paper')
    if forward:
        text+=f" Новые данные: {forward.get('trades',0)} сделок; сумма {number(forward.get('sum_r'))} R."
    return text


def event_text(event):
    detail=event.detail
    if event.step=='proposed':
        return 'Предзаданный вариант параметров; текущие ограничения риска сохранены.'
    if event.step=='paper_wait':
        return f"Получено {detail.get('trades',0)} сделок на новых данных; требуется {detail.get('required',20)}."
    reasons=[]
    for reason in detail.get('reasons',[]):
        if 'too few' in reason: reasons.append('Недостаточно сделок для вывода')
        elif 'positive' in reason: reasons.append('Нет требуемого положительного результата и Sharpe')
        elif 'Sharpe' in reason: reasons.append('Sharpe не лучше контрольной версии')
        elif 'maxDD' in reason: reasons.append('Просадка хуже контрольной версии')
        elif 'folds' in reason: reasons.append('Недостаточная устойчивость по периодам')
        else: reasons.append(reason)
    candidate=detail.get('candidate',{})
    stats_text=evidence_text({'wf':candidate}) if candidate and 'error' not in candidate else str(candidate.get('error',''))
    return '; '.join(reasons)+('. '+stats_text if stats_text else '')


def position_card(p, quote, journal):
    profile=(journal.context or {}).get('execution_profile',{}) if journal else {}
    profile_text=(f"Демоплечо {profile['leverage']}× · залог {profile['margin_pct']}% · лимит удержания {profile['max_hold_minutes']} мин" if profile else 'Позиция открыта по прежнему профилю размера и удержания')
    mark = (quote[0] if p.side == "long" else quote[1]) if quote else None
    sign = 1 if p.side == "long" else -1
    pnl = sign * (mark-p.entry)*p.qty - p.fees + p.funding - mark*p.qty*.00055 if mark else None
    risk = abs(p.entry-p.stop)*p.qty
    rr = pnl/risk if pnl is not None and risk else None
    take = p.take
    scale = ''
    if take is not None and take != p.stop:
        entry_pct=max(0,min(100,100*(p.entry-p.stop)/(take-p.stop)))
        mark_pct=max(0,min(100,100*((mark if mark is not None else p.entry)-p.stop)/(take-p.stop)))
        scale=f'''<div class="price-scale" role="progressbar" aria-label="Цена между стопом и тейком" aria-valuemin="0" aria-valuemax="100" aria-valuenow="{mark_pct:.1f}" aria-valuetext="Цена {number(mark)}, вход {number(p.entry)}, стоп {number(p.stop)}, тейк {number(take)}">
<span class="entry-tick" style="left:{entry_pct:.2f}%" title="Цена входа"></span>
<span class="price-dot {'positive' if pnl is not None and pnl >= 0 else 'negative'}" style="left:{mark_pct:.2f}%" title="Текущая цена"></span></div>
<div class="scale-labels"><span>СТОП<br><b>{number(p.stop)}</b></span><span>ВХОД<br><b>{number(p.entry)}</b></span><span>ТЕЙК<br><b>{number(take)}</b></span></div>'''
    return f'''<article class="position" data-symbol="{e(p.symbol)}" data-side="{e(p.side)}" data-entry="{p.entry}" data-stop="{p.stop}" data-take="{p.take}" data-qty="{p.qty}" data-fees="{p.fees}" data-funding="{p.funding}"><div class="row"><div><h3>{e(p.symbol)} <span class="tag">{e('ЛОНГ' if p.side=='long' else 'ШОРТ')}</span></h3><p class="muted small">{e(p.strategy)} · {date(p.opened_ts)}</p></div><div class="pnl {'green' if pnl is not None and pnl>=0 else 'red'}">{number(pnl)} <small>USDT</small><span>{number(rr)} R</span></div></div>
<div class="row quote"><span>Цена сейчас <b>{number(mark)}</b></span><span>Объём <b>{number(p.qty,5)}</b></span></div>{scale}
<p class="muted small">{e(profile_text)}</p><p class="muted small">{e(journal.thesis if journal else '')}</p><p class="muted small">P/L включает комиссии входа, оценку выхода, проскальзывание входа и фандинг. Маркер — индикатор, цены им не изменяются.</p></article>'''


CSS='''
:root{color-scheme:dark;--bg:#0b0f13;--panel:#121820;--line:#26303b;--muted:#a5afbc;--text:#e7ecf1;--amber:#f5a623;--green:#57d5ad;--red:#ff858b}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 system-ui,sans-serif}a{color:inherit}button{font:inherit}header,main{max-width:1280px;margin:auto;padding:24px}header{padding-bottom:0}.row{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}.brand{font-size:20px;font-weight:700;letter-spacing:-.5px}.brand em{font-style:normal;color:var(--amber)}.muted{color:var(--muted)}.small{font-size:12px}.tag{display:inline-block;border:1px solid var(--line);border-radius:6px;font-size:11px;padding:3px 8px;letter-spacing:.6px}.paper{color:var(--amber);border-color:#765623}.live-status{color:var(--green)}.stale{color:var(--red)!important}.notice{border:1px solid var(--line);border-left:3px solid var(--amber);border-radius:8px;padding:10px 16px;margin-top:16px;color:var(--muted);font-size:12px}nav{display:flex;gap:8px;overflow:auto;margin-top:24px;border-bottom:1px solid var(--line)}nav button{color:var(--muted);padding:12px 16px;border:0;border-bottom:2px solid transparent;background:none;cursor:pointer;white-space:nowrap}nav button[aria-selected=true]{color:var(--amber);border-bottom-color:var(--amber)}nav button:hover{color:var(--text)}:focus-visible{outline:2px solid var(--amber);outline-offset:3px}.kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:0 0 24px}.card,.position{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px;margin-bottom:16px}.kpis .card{margin:0}.kpis b{display:block;font-size:26px;font-weight:650;margin-top:8px;font-variant-numeric:tabular-nums}.kpis small{display:block;color:var(--muted);font-size:11px}.columns{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(260px,1fr);gap:16px}h1{font-size:24px;margin:0 0 4px}h2{font-size:16px;margin:0 0 16px}h3{font-size:17px;margin:0}p{margin:8px 0}.pnl{font-size:24px;text-align:right;font-variant-numeric:tabular-nums}.pnl small{font-size:11px;color:var(--muted)}.pnl span{display:block;font-size:13px}.green{color:var(--green)}.red{color:var(--red)}.quote{font-size:12px;margin:24px 0 36px}.quote b{font-size:16px;margin-left:8px}.price-scale{position:relative;height:7px;border-radius:8px;background:linear-gradient(90deg,#87424e,#303d4a 40%,#277661);margin:16px 8px 12px}.entry-tick{position:absolute;top:-9px;width:2px;height:26px;background:#e7ecf1;opacity:.65}.price-dot{position:absolute;top:-5px;width:17px;height:17px;background:var(--amber);border:3px solid var(--panel);border-radius:50%;transform:translateX(-50%);box-shadow:0 0 0 1px var(--amber)}.scale-labels{display:flex;justify-content:space-between;font-size:10px;color:var(--muted)}.scale-labels span:nth-child(2){text-align:center}.scale-labels span:last-child{text-align:right}.scale-labels b{font-size:13px;color:var(--text);font-variant-numeric:tabular-nums}svg{width:100%;height:170px}.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:12px}th,td{padding:12px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top;min-width:70px}th{color:var(--muted);font-weight:500;white-space:nowrap}td{max-width:420px;overflow-wrap:anywhere}.lesson{border-left:2px solid var(--amber);padding:0 0 0 16px;margin:20px 0}.empty{padding:24px 0;color:var(--muted)}.stats-list{display:grid;grid-template-columns:1fr auto;gap:12px}.status-note{font-size:12px;padding:12px;border:1px dashed var(--line);border-radius:8px}.footer{font-size:11px;color:var(--muted);margin:24px 0}section[hidden]{display:none!important}@media(max-width:760px){header,main{padding:16px}.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.kpis b{font-size:22px}.columns{grid-template-columns:1fr}.card,.position{padding:16px}nav button{padding:12px}.row{gap:8px}}
'''


def render_base_dashboard(s, mode, equity, blockers, market_source='', quotes=None, limits=None):
    quotes=quotes or {}; limits=limits or {}
    account=s.get(PaperAccount,1)
    positions=list(s.scalars(select(Position).where(Position.status=='open')))
    journal={j.position_id:j for j in s.scalars(select(JournalEntry).where(JournalEntry.exit_ts.is_(None)))}
    closed=list(s.scalars(select(JournalEntry).where(JournalEntry.exit_ts.is_not(None)).order_by(JournalEntry.exit_ts.desc()).limit(200)))
    count,wins,pnl,fees=s.execute(select(func.count(),func.sum((JournalEntry.pnl>0).cast(Integer)),func.sum(JournalEntry.pnl),func.sum(JournalEntry.fees)).where(JournalEntry.exit_ts.is_not(None))).one()
    winrate=f'{100*(wins or 0)/count:.1f}%' if count else '—'
    net=None if equity is None or not account else equity-account.initial
    hb=s.get(AgentState,'heartbeat'); heartbeat=int(hb.value) if hb else 0
    snaps=list(s.scalars(select(EquitySnapshot).order_by(EquitySnapshot.ts.desc()).limit(500)))[::-1]
    run=s.scalar(select(AnalystRun).where(AnalystRun.purpose=='decide').order_by(AnalystRun.ts.desc()).limit(1))
    analysis={}
    if run:
        try: analysis=json.loads(run.response)
        except (ValueError,TypeError): pass
    lessons=list(s.scalars(select(Lesson).order_by(Lesson.ts.desc()).limit(40)))
    versions=list(s.scalars(select(StrategyVersion).order_by(StrategyVersion.created_ts.desc()).limit(40)))
    events=list(s.scalars(select(PromotionEvent).order_by(PromotionEvent.ts.desc()).limit(30)))
    risk=list(s.scalars(select(RiskEvent).where(RiskEvent.kind!='approve').order_by(RiskEvent.ts.desc()).limit(20)))
    research=s.get(AgentState,'research_status')
    state={}
    if research:
        try: state=json.loads(research.value)
        except (ValueError,TypeError): pass
    stale=not heartbeat or time.time()*1000-heartbeat>180000
    pos_html=''.join(position_card(p,quotes.get(p.symbol),journal.get(p.id)) for p in positions) or '<div class="card empty">Открытых демопозиций нет. Агент ждёт подходящего сигнала.</div>'
    market_rows=[]
    for symbol,context in analysis.get('markets',{}).items():
        market_rows.append([symbol,context.get('regime','—'),number(context.get('rsi14')),number(context.get('atr14'))])
    lesson_html=''.join(f'<article class="lesson"><p class="muted small">{date(l.ts)} · {e(l.symbol)} · {e(l.strategy)}</p><h3>{e(l.lesson)}</h3><p>{e(l.what_worked)}</p><p class="muted">{e(l.mistake)}</p><span class="tag">{e(l.outcome)} · гипотеза, не доказательство</span></article>' for l in lessons) or '<p class="empty">Разбор появится после закрытия первой сделки. Причины и выводы не выдумываются.</p>'
    risk_rows=[[name,limits.get(key,'—')] for name,key in [('Риск на сделку, %','max_risk_per_trade_pct'),('Дневной предел потерь, %','daily_loss_limit_pct'),('Предельная просадка, %','max_drawdown_pct'),('Максимум позиций','max_open_positions'),('Предел плеча','max_leverage')]]
    statuses={'candidate':'Кандидат','paper_trial':'Испытание на новых данных','rejected':'Отклонён','production':'Базовая версия','retired':'Архив','rolled_back':'Отменён'}
    steps={'proposed':'Предложен вариант','walk_forward':'Проверка истории','paper_wait':'Ожидание выборки','paper':'Новые данные','promote':'Принят в демо','rollback':'Откат'}
    summary=(f"Новых гипотез: {state.get('proposed',0)}. Разборов закрытых сделок: {state.get('lessons',0)}. Результаты проверок — ниже." if 'proposed' in state else state.get('summary','Ожидается достаточная история.'))
    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="60"><title>Futures Agent · Observatory</title><style>{CSS}</style></head><body>
<header><div class="row"><div class="brand">FUTURES <em>AGENT</em> <span class="tag paper">{e(mode.upper())}</span></div><span class="live-status {'stale' if stale else ''}" id="connection" data-heartbeat="{heartbeat}">{'Данные устарели' if stale else 'Агент на связи'}</span></div><p class="muted small">{e(market_source)} · Local paper simulation · No exchange account connected</p>
<div class="notice">Виртуальные сделки. Реальные средства не подключены. Доходность не подтверждена. Обновление раз в минуту; время в UTC. Сайт показывает последнюю синхронизацию, а не обещание работы ноутбука.</div>
<nav role="tablist" aria-label="Разделы"><button role="tab" id="tab-overview" aria-controls="overview" data-tab="overview" aria-selected="true">Обзор</button><button role="tab" id="tab-positions" aria-controls="positions" data-tab="positions" aria-selected="false">Сделки · {len(positions)}</button><button role="tab" id="tab-history" aria-controls="history" data-tab="history" aria-selected="false">История</button><button role="tab" id="tab-learning" aria-controls="learning" data-tab="learning" aria-selected="false">Обучение и ошибки</button><button role="tab" id="tab-risk" aria-controls="risk" data-tab="risk" aria-selected="false">Риск и состояние</button></nav></header>
<main><div class="kpis"><div class="card"><span class="muted">Капитал · USDT</span><b>{number(equity)}</b><small>Баланс {number(account.cash if account else None)}</small></div><div class="card"><span class="muted">С начала · USDT</span><b class="{'green' if net is not None and net>=0 else 'red'}">{number(net)}</b><small>Включая открытые позиции</small></div><div class="card"><span class="muted">Закрытые сделки</span><b>{count}</b><small>Прибыльных {winrate}</small></div><div class="card"><span class="muted">Чистый итог закрытых</span><b>{number(pnl or 0)}</b><small>Учтённые комиссии {number(fees or 0)} USDT</small></div></div>
<section role="tabpanel" aria-labelledby="tab-overview" id="overview"><div class="columns"><div><div class="card"><h2>Капитал в динамике</h2>{spark([x.equity for x in snaps])}</div><h2>Открытые позиции</h2>{pos_html}</div><div><div class="card"><h2>Последний анализ</h2><span class="tag">{date(run.ts if run else 0)}</span><p>{e(analysis.get('thesis') or 'Ожидается первый анализ.')}</p><p class="muted small">Кандидатов: {e(analysis.get('candidates','—'))}. Нет сигнала — нет сделки.</p></div><div class="card"><h2>Состояние рынка</h2>{table(['Пара','Режим','RSI 14','ATR 14'],market_rows)}</div></div></div></section>
<section role="tabpanel" aria-labelledby="tab-positions" id="positions" hidden><h1>Состояние сделок</h1><p class="muted">Слева стоп, справа цель. Белая отметка — вход; круг — текущая цена. Для шорта цены на шкале убывают.</p>{pos_html}</section>
<section role="tabpanel" aria-labelledby="tab-history" id="history" hidden><div class="card"><h2>Последние 200 закрытых сделок</h2>{table(['Выход','Пара','Направление','Вход → выход','P/L USDT','R','Причина','Стратегия'],[[date(j.exit_ts),j.symbol,j.side,f'{number(j.entry)} → {number(j.exit)}',number(j.pnl),number(j.r),j.exit_reason,j.strategy] for j in closed])}</div></section>
<section role="tabpanel" aria-labelledby="tab-learning" id="learning" hidden><div class="columns"><div><div class="card"><h2>Наблюдения и работа над ошибками</h2><p class="muted">Это статистическое исследование правил, не обучение нейросети. Единичная потеря не доказывает ошибку стратегии.</p>{lesson_html}</div></div><div><div class="card"><h2>Исследовательский цикл</h2><p>{e(state.get('status','Ожидается первый запуск'))}</p><p class="muted small">{date(state.get('finished_ts',0))}</p><p>{e(summary)}</p><div class="status-note">Никакого самовольного повышения риска. Кандидат → проверка истории → минимум 14 дней новых данных и 20 сделок → только затем возможность смены демоправил.</div></div></div></div><div class="card"><h2>Варианты стратегий</h2>{table(['Семейство','Версия','Статус','Идея','OOS / проверки'],[[v.family,f'v{v.version}',statuses.get(v.status,v.status),v.rationale,evidence_text(v.metrics)] for v in versions])}</div><div class="card"><h2>Что проверено и что отклонено</h2>{table(['Время','Версия','Проверка','Результат','Обоснование'],[[date(x.ts),x.version_id,steps.get(x.step,x.step),'пройдена' if x.passed else 'ожидание / отклонена',event_text(x)] for x in events])}</div></section>
<section role="tabpanel" aria-labelledby="tab-risk" id="risk" hidden><div class="columns"><div class="card"><h2>Ограничения</h2>{table(['Правило','Значение'],risk_rows)}<p class="{'red' if blockers else 'green'}">{e('; '.join(blockers) if blockers else 'Новые демовходы разрешены при наличии сигнала')}</p></div><div class="card"><h2>Связь с агентом</h2><p>Последний рабочий цикл: <b>{date(heartbeat)}</b></p><p class="muted">Пауза: файл STOP на ноутбуке. Удалённая панель доступна только для чтения.</p><p class="muted">Если ноутбук выключен или не в сети, сайт остаётся доступен с последним снимком и отмечает данные как устаревшие.</p><p class="muted">Ни сайт, ни локальная программа не подключены к реальному биржевому счёту.</p></div></div><div class="card"><h2>Журнал ограничений и отказов</h2>{table(['Время','Событие','Пара','Причина'],[[date(x.ts),x.kind,x.symbol,json.dumps(x.detail,ensure_ascii=False)[:700]] for x in risk])}</div></section>
<p class="footer">Futures Agent · Binance public market data · PAPER · Снимок {date(int(time.time()*1000))} · Метрики истории не являются прогнозом.</p></main>
<script>const tabs=[...document.querySelectorAll('[data-tab]')];function show(id){{if(!tabs.some(t=>t.dataset.tab===id))id='overview';tabs.forEach(t=>t.setAttribute('aria-selected',String(t.dataset.tab===id)));document.querySelectorAll('[role=tabpanel]').forEach(p=>p.hidden=p.id!==id)}}tabs.forEach(t=>t.onclick=()=>{{location.hash=t.dataset.tab;show(t.dataset.tab)}});addEventListener('hashchange',()=>show(location.hash.slice(1)));show(location.hash.slice(1));function freshness(){{const el=document.getElementById('connection');const age=Date.now()-Number(el.dataset.heartbeat);el.classList.toggle('stale',age>180000);el.textContent=age>180000?'Данные устарели · агент не подтверждён':'Агент на связи · '+Math.max(0,Math.floor(age/1000))+' с назад'}}freshness();setInterval(freshness,5000);</script></body></html>'''


def render_dashboard(s, mode, equity, blockers, market_source='', quotes=None, limits=None):
    from ops.mobile_dashboard import render
    return render(s,mode,equity,blockers,market_source,quotes,limits)
