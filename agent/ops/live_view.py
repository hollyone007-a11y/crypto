"""Live UI shell and human-readable, evidence-labelled research reports."""
import json
import re
import time
import hashlib
from config.settings import ROOT
from db.models import AgentState


def state_json(session,key):
    row=session.get(AgentState,key)
    try: return json.loads(row.value) if row else {}
    except (ValueError,TypeError): return {}


def report_panel(session):
    from ops.presentation import e,number,date,table
    report=state_json(session,'research_attempt') or state_json(session,'research_report')
    progress=state_json(session,'research_progress')
    header=f'''<div class="card" id="research-live-report"><h1>Что агент улучшает и почему</h1>
<p><b>Сейчас:</b> {e(progress.get('stage','Ожидается исследовательский цикл'))}</p>
<p class="muted small">Этап обновлён: {date(progress.get('heartbeat_ts',0))}. Отчёт: {date(report.get('finished_ts',0))}.</p>
<p>{e(report.get('change_summary','Улучшение пока не подтверждено.'))}</p>
<p class="muted">{e(report.get('goal','Проверять правила на отдельных данных, не повышая риск.'))}</p>
<p><b>Далее:</b> {e(report.get('next_step','Дождаться свежих данных и достаточной выборки.'))}</p>
<p class="muted small">Следующая полная проверка не раньше {date(report.get('next_due_ts',0))}; готовность проверяется каждые 10 минут.</p>
<a class="tag" href="/research-report.txt" download="research-report.txt">Скачать последний отчёт</a>
<p class="muted small">Проверка новых гипотез не означает смену рабочих правил. Изменением считается только записанное принятие или откат версии.</p></div>'''
    profile=state_json(session,'execution_profile')
    if profile.get('margin_pct'):
        header+='<div class="card"><h2>Область доказательств</h2><p>Исторические тесты относятся к базовым стратегиям. Они не доказывают доходность нового размера 10×/10%, новостного фильтра, копирования и выходов по времени. Автоматическое принятие кандидатов приостановлено до проверки соответствующего профиля; сбор наблюдений и исследования продолжаются.</p></div>'
    if not report: return header
    observations=table(['Версия','Закрытых сделок','Чистый итог USDT','Комиссии USDT','Стопы / отдача 1R'],
        [[x['strategy'],x['sample_size'],number(x['net_usdt']),number(x['fees_usdt']),f"{x['stop_count']} / {x['giveback_count']}"]
         for x in report.get('observations',[])])
    cards=[]
    statuses={'candidate':'Кандидат','paper_trial':'Испытание на новых данных','production':'Рабочая демоверсия',
              'rejected':'Отклонён','retired':'Архив','rolled_back':'Откачен'}
    for experiment in report.get('experiments',[]):
        before=experiment.get('before',{}); after=experiment.get('candidate',{})
        delta=experiment.get('paired_delta')
        rows=[[title,number(before.get(key),3),number(after.get(key),3),number(delta.get(key),3) if delta else 'не сопоставлено']
              for title,key in [('Средний результат, R','expectancy_r'),('Sharpe','sharpe'),('Просадка, %','max_dd_pct')]]
        forward=experiment.get('forward',{})
        source='Наблюдения по закрытым сделкам' if experiment.get('source')=='observations' else 'Заранее заданный тест устойчивости'
        parameters=', '.join(f"{key}: {value['before']} → {value['after']}" for key,value in experiment.get('parameter_diff',{}).items())
        cards.append(f'''<details class="card" data-experiment="{e(experiment['strategy'])}"><summary><strong>{e(experiment['strategy'])}</strong> · <span class="tag">{e(statuses.get(experiment['status'],experiment['status']))}</span><br><span class="muted">{e(experiment['question'])}</span></summary>
<p><b>Что проверяется:</b> {e(experiment['question'])}</p><p><b>Почему:</b> {e(experiment['reason'])}</p>
<p><b>Изменение параметров:</b> {e(parameters or 'нет')}</p>
<p class="muted small">Источник: {source}. Исторических сделок кандидата: {e(after.get('trades',0))}.</p>
{table(['Показатель','Контроль','Кандидат','Разница на одинаковых данных'],rows)}
<p class="muted small">{'Одинаковый снимок данных и издержек подтверждён.' if delta is not None else 'Нет подтверждённой пары одинаковых данных; улучшение по разнице не заявляется.'}</p>
<p>На новых данных: {e(forward.get('trades',0))} сделок; сумма {number(forward.get('sum_r'))} R. Это повторная симуляция поступивших свечей, не отдельный реальный счёт.</p>
<p><b>Следующее условие:</b> {e(experiment['next_step'])}</p></details>''')
    return header+f'<div class="card"><h2>На каких наблюдениях основан отчёт</h2>{observations}<p class="muted small">Малая выборка не позволяет объявлять причину убытков установленной.</p></div>'+''.join(cards)


def decorate(session,html,quotes):
    generated=int(time.time()*1000)
    heartbeat=session.get(AgentState,'heartbeat')
    heartbeat=int(heartbeat.value) if heartbeat else 0
    clocks=f'''<div class="card small" id="live-clocks" data-generated="{generated}" data-heartbeat="{heartbeat}">
<div class="row"><span id="market-live-status">Котировки: подключение…</span><span id="agent-live-status">Состояние агента: проверка…</span><span id="sync-live-status">Синхронизация: проверка…</span></div>
<p class="muted small">Live mark price — поток Binance раз в секунду. Состояние демосчёта — снимок агента примерно раз в 15 секунд. Входы и выходы проверяются каждые 5 секунд. Анализ сигналов остаётся по закрытым свечам.</p></div>'''
    html=re.sub(r'<meta http-equiv="refresh"[^>]*>','',html)
    html=html.replace('Обновление раз в минуту; время в UTC.','Котировки — поток; снимок счёта — около 15 с. Время в UTC.')
    html=re.sub(r'<script>.*?</script>','',html,flags=re.S)
    html=html.replace('<main>','<main>'+clocks,1)
    from ops.profile_panel import render as render_profile
    html=html.replace('<main>','<main>'+render_profile(session),1)
    marker='<section role="tabpanel" aria-labelledby="tab-learning" id="learning" hidden>'
    html=html.replace(marker,marker+report_panel(session),1)
    from ops.context_panel import render as render_context
    universe=state_json(session,'universe')
    symbols=[x['symbol'] for x in universe.get('selected',[])] or ['BTCUSDT','ETHUSDT']
    tab='<button role="tab" id="tab-context" aria-controls="context" data-tab="context" aria-selected="false">Трейдеры и новости</button>'
    html=html.replace('</nav>',tab+'</nav>',1)
    html=html.replace('<p class="footer">',render_context(session,generated,symbols)+'<p class="footer">',1)
    section=re.search(r'(<section[^>]*id="learning"[^>]*>)(.*?)(</section>)',html,re.S)
    if section:
        digest=hashlib.sha256(section.group(2).encode('utf-8')).hexdigest()[:16]
        html=html.replace(section.group(1),section.group(1).replace(' hidden>',f' data-content-version="{digest}" hidden>'),1)
    script=(ROOT/'ops'/'live.js').read_text(encoding='utf-8')
    return html.replace('</body>','<script>'+script+'</script></body>',1)
