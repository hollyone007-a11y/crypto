"""Signal research shown separately from actual demo trading."""
from sqlalchemy import select
from analyst.news_risk import read_state
from db.models import SignalObservation


def panel(session):
    from ops.mobile_dashboard import table
    from ops.presentation import e,number,date
    from learning.observatory import REVISION
    report=read_state(session,'signal_learning')
    if report.get('revision')!=REVISION:report={}
    counts=report.get('counts',{});closed=report.get('closed_modes',{})
    rows=sorted(report.get('cohorts',[]),key=lambda x:(x['mode']!='forward',-x['count'],x['method'],x['timeframe'],x['policy']))
    cohorts=table(['Данные','Метод / ТФ / выход','Наблюдений','Средний net R','Последняя половина, средний net %','Что изучает'],[
        ['Будущие' if x['mode']=='forward' else 'Исторический расчёт',x['method']+' / '+x['timeframe']+' / '+x['policy'],x['count'],
         number(x['mean_r'],3),number(x.get('heldout_mean_pct'),3),x['reason']] for x in rows])
    latest=list(session.scalars(select(SignalObservation).where(SignalObservation.revision==REVISION)
        .order_by(SignalObservation.created_ts.desc(),SignalObservation.id).limit(8)))
    inbox=table(['Сигнал UTC','Монета / ТФ','Тип','Состояние','Прогноз / итог R','Причина'],[
        [date(x.signal_ts),x.symbol+' / '+x.timeframe,'Будущее наблюдение' if x.mode=='forward' else 'Исторический расчёт',
         x.status,number(x.prediction_r,3)+' / '+number(x.net_r,3),x.reason] for x in latest])
    return f'''<div class="card accent" id="signal-learning"><h2>Поток сигналов и обучение</h2><p>Записано сигналов: <b>{sum(counts.values())}</b> · ожидают/наблюдаются: <b>{counts.get('pending',0)+counts.get('open',0)}</b> · завершено будущих: <b>{closed.get('forward',0)}</b> · исторических расчётов: <b>{closed.get('replay',0)}</b>.</p><p>Это контрольные наблюдения, а не сделки демосчёта. Все найденные сигналы 4 методов на 5м/15м/1ч/4ч записываются независимо от торгового допуска; необнаруженные или отсутствующие свечи не выдумываются. Первичная загрузка охватывает последние 24 часа. Старые сигналы отмечены как исторические расчёты.</p><p>Будущее наблюдение планируется до входной свечи. По завершённым будущим наблюдениям обновляется средний net R метода/ТФ/выхода; оценка записывается до результата следующего сигнала. Сравнение первой и последней половины показывает, сохраняется ли результат.</p><p class="small muted">Оценённых прогнозов: {report.get('predictions_scored',0)} · средняя абсолютная ошибка: {number(report.get('prediction_mae_r'),3)} R. Последняя обработка {date(report.get('ts',0))}. {e(report.get('scope','Ожидание первого цикла.'))} Статистика ограничена последними 10 000 завершёнными наблюдениями. Автоматических изменений торговых правил: {report.get('actual_rule_changes',0)}.</p><details id="signal-cohorts"><summary>Что изучает агент и почему</summary>{cohorts}<p>{e(report.get('next_step','Собрать завершённые будущие наблюдения.'))}</p></details><details id="signal-inbox"><summary>Последние зарегистрированные сигналы</summary>{inbox}</details></div>'''
