"""Auditable parameter research. Observations are not causal conclusions."""
import json
import math
import uuid
from pathlib import Path
from datetime import UTC, datetime
from sqlalchemy import select, func

from analyst.schema import ParamChange, ParamProposal
from db.models import AgentState, AnalystRun, JournalEntry, Ohlcv, PromotionEvent, StrategyVersion
from learning import pipeline
from learning.autonomous import review_rules, set_state, SIX_HOURS
from strategies.registry import label, production


def clean(value):
    if isinstance(value,float) and not math.isfinite(value): return None
    if isinstance(value,dict): return {str(k):clean(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)): return [clean(v) for v in value]
    return value


def diagnose(session,version):
    trades=list(session.scalars(select(JournalEntry).where(
        JournalEntry.strategy==label(version),JournalEntry.exit_ts.is_not(None))
        .order_by(JournalEntry.exit_ts.desc()).limit(50)))
    net=sum(t.pnl or 0 for t in trades); fees=sum(t.fees or 0 for t in trades)
    funding=sum(t.funding or 0 for t in trades)
    return {'strategy':label(version),'sample_size':len(trades),'journal_ids':[t.id for t in trades],
            'net_usdt':round(net,4),'fees_usdt':round(fees,4),
            'gross_usdt':round(net+fees-funding,4),
            'stop_count':sum(t.exit_reason=='stop' for t in trades),
            'giveback_count':sum((t.mfe_r or 0)>=1 and (t.r or 0)<0 for t in trades),
            'interpretation':'Наблюдение, не доказанная причина. Минимум 20 закрытых сделок для гипотезы по ошибкам.'}


def hypotheses(parent,facts):
    params=parent.params; n=facts['sample_size']; options=[]
    def add(key,factor,question,reason,source):
        if key not in params: return
        value=float(params[key])*factor
        if key in pipeline.INT_PARAMS: value=round(value)
        options.append({'key':key,'before':params[key],'after':value,'question':question,
                        'reason':reason,'source':source,'evidence':facts})
    if n>=20:
        if facts['gross_usdt']>0 and facts['net_usdt']<=0:
            key=next((k for k in ('channel','window','adx_min') if k in params),None)
            if key: add(key,1.1,'Помогает ли более редкий вход снизить долю издержек?',
                        f"На {n} сделках валовой результат положительный, чистый — нет.",'observations')
        if facts['giveback_count']>=max(3,n*.2):
            add('take_atr',.9,'Сохраняет ли более близкая цель чистый результат после отдачи прибыли?',
                f"В {facts['giveback_count']} из {n} сделок было движение 1R, но итог отрицательный.",'observations')
        if facts['stop_count']>=n*.5:
            add('stop_atr',1.1,'Улучшает ли более широкий стоп результат при прежнем риске на сделку?',
                f"Стоп сработал в {facts['stop_count']} из {n} сделок; причинность ещё не установлена.",'observations')
    add('stop_atr',1.1,'Устойчиво ли правило к изменению ширины стопа на 10%?',
        'Предзаданный тест устойчивости. Это не вывод об ошибке из недостаточной выборки.','predeclared')
    key=next((k for k in ('channel','window','lookback','adx_min') if k in params),None)
    if key: add(key,1.1,'Устойчив ли вход к небольшому изменению фильтра?',
                'Ограниченная заранее заданная проверка параметра, без подгонки большой сетки.','predeclared')
    return options


def paired_delta(before,after):
    keys=('dataset_id','cutoff_ts','symbols','cost_model','window_start_ts')
    if not before.get('dataset_id') or any(before.get(k)!=after.get(k) for k in keys): return None
    return {k:round(after[k]-before[k],4) for k in ('sharpe','expectancy_r','max_dd_pct')
            if isinstance(after.get(k),(int,float)) and isinstance(before.get(k),(int,float))
            and math.isfinite(after[k]) and math.isfinite(before[k])}


def report_text(report):
    when=lambda ts:datetime.fromtimestamp(ts/1000,UTC).strftime('%d.%m.%Y %H:%M UTC')
    lines=['ОТЧЁТ ИССЛЕДОВАНИЙ · PAPER',f"Запуск: {report['run_id']}",
           f"Статус: {report['status']}",f"Начало: {when(report['started_ts'])}",
           f"Окончание: {when(report['finished_ts'])}",report.get('goal',''),
           f"Монет: {len(report['coverage']['symbols'])}; устаревших/отсутствующих: {len(report['coverage'].get('missing_or_stale_symbols',[]))}",
           'Наблюдения — не причинные выводы. Исторический результат — не гарантия прибыли.','']
    for observation in report.get('observations',[]):
        lines.append(f"{observation['strategy']}: {observation['sample_size']} закрытых сделок; чистый результат {observation['net_usdt']:+.2f}; комиссии {observation['fees_usdt']:.2f} USDT.")
    def metrics(value):
        return (f"сделок {value.get('trades',0)}, средний результат {value.get('expectancy_r','—')} R, "
                f"Sharpe {value.get('sharpe','—')}, просадка {value.get('max_dd_pct','—')}%")
    for experiment in report.get('experiments',[]):
        lines += ['',f"Вариант {experiment['strategy']} · {experiment['status']}",
                  'Что проверяется: '+experiment['question'],'Почему: '+experiment['reason'],
                  'Источник гипотезы: '+experiment['source'],
                  'Результат кандидата на истории: '+metrics(experiment.get('candidate',{})),
                  'Контроль на той же истории: '+metrics(experiment.get('before',{})),
                  'Разница на совпадающих данных: '+json.dumps(experiment.get('paired_delta'),ensure_ascii=False),
                  'Следующее условие: '+experiment['next_step']]
    lines += ['',f"Реальных смен демоправил в этом запуске: {len(report.get('actual_changes',[]))}.",
              report.get('change_summary',''), 'Далее: '+report.get('next_step','')]
    return '\n'.join(lines)


def save_report(session,report,report_dir=None):
    report=clean(report)
    encoded=json.dumps(report,ensure_ascii=False,allow_nan=False)
    session.add(AnalystRun(ts=report['finished_ts'],purpose='research_report',model='deterministic-lab-v2',
                          ok=report['status']!='failed',request={'run_id':report['run_id']},response=encoded))
    session.commit()
    set_state(session,'research_attempt',encoded)
    if report['status']=='complete': set_state(session,'research_report',encoded)
    if report_dir:
        directory=Path(report_dir); directory.mkdir(parents=True,exist_ok=True)
        (directory/(report['run_id']+'.json')).write_text(encoded,encoding='utf-8')
        (directory/(report['run_id']+'.txt')).write_text(report_text(report),encoding='utf-8')
        (directory/'latest.txt').write_text(report_text(report),encoding='utf-8')
    return report


def run_research(session,evaluate,symbols,now_ms,clock=None,exchange=None,report_dir=None):
    clock=clock or (lambda:now_ms); symbols=list(symbols)
    lessons=review_rules(session,now_ms)
    previous=session.get(AgentState,'research_last_run'); prior_symbols=session.get(AgentState,'research_symbols')
    previous_report=session.get(AgentState,'research_report')
    same=prior_symbols is not None and set(json.loads(prior_symbols.value))==set(symbols)
    if previous and previous_report and same and now_ms-int(previous.value)<SIX_HOURS:
        return {'status':'not_due','lessons':lessons}
    report={'schema':2,'run_id':f'research-{now_ms}-{uuid.uuid4().hex[:6]}','started_ts':now_ms,
            'status':'running','goal':'Повысить устойчивость чистого результата при неизменных лимитах риска.',
            'coverage':{'exchange':exchange,'symbols':symbols,'data_cutoff_ts':now_ms},
            'observations':[],'experiments':[],'actual_changes':[]}
    def progress(stage):
        set_state(session,'research_progress',json.dumps({'run_id':report['run_id'],'status':'running',
            'stage':stage,'started_ts':now_ms,'heartbeat_ts':clock()},ensure_ascii=False))
    try:
        progress('Проверяю полноту и свежесть данных каждой монеты')
        missing=[]; history={}
        for symbol in symbols:
            query=select(func.max(Ohlcv.ts),func.min(Ohlcv.ts),func.count()).where(Ohlcv.symbol==symbol,Ohlcv.timeframe=='1h')
            if exchange: query=query.where(Ohlcv.exchange==exchange)
            latest,first,count=session.execute(query).one()
            history[symbol]={'latest_ts':latest,'first_ts':first,'bars':count}
            if latest is None or now_ms-latest>7_200_000 or count<200: missing.append(symbol)
        report['coverage']['history']=history
        report['coverage']['missing_or_stale_symbols']=missing
        if missing:
            report.update(status='waiting',finished_ts=clock(),next_step='Дождаться свежей истории: '+', '.join(missing),
                          change_summary='Рабочие правила не изменены: сравнение на неполных данных запрещено.')
            set_state(session,'research_progress',json.dumps({'status':'waiting','stage':report['next_step'],'heartbeat_ts':clock()},ensure_ascii=False))
            return save_report(session,report,report_dir)
        event_id=session.scalar(select(func.max(PromotionEvent.id))) or 0
        cache={}
        def cached(version,start):
            key=(version.id,start)
            if key not in cache:
                progress('Проверяю '+label(version)+(' на новых данных' if start is not None else ' на истории'))
                cache[key]=evaluate(version,start)
            return cache[key]
        parents=production(session)
        for parent,_ in parents:
            facts=diagnose(session,parent);report['observations'].append(facts)
            result=cached(parent,None)
            parent.metrics={**parent.metrics,'wf':result,'wf_symbols':result.get('symbols',symbols),'wf_checked_ts':now_ms}
        session.commit()
        proposed=0
        for parent,_ in parents:
            if parent.family not in pipeline.FAMILIES: continue
            pending=session.scalar(select(StrategyVersion.id).where(StrategyVersion.family==parent.family,
                            StrategyVersion.status.in_(['candidate','paper_trial'])).limit(1))
            children=list(session.scalars(select(StrategyVersion).where(StrategyVersion.parent_id==parent.id)))
            if pending: continue
            facts=next(x for x in report['observations'] if x['strategy']==label(parent))
            for idea in hypotheses(parent,facts):
                origin=idea['source']
                used=sum(v.metrics.get('hypothesis',{}).get('source','predeclared')==origin for v in children)
                if used>=2: continue  # two declared tests + at most two observation-led tests
                expected={**parent.params,idea['key']:idea['after']}
                if any(v.params==expected for v in children): continue
                proposal=ParamProposal(family=parent.family,changes=[ParamChange(name=idea['key'],value=idea['after'])],
                                       rationale=idea['question']+' '+idea['reason'])
                candidate=pipeline.propose_params(session,proposal)
                if candidate:
                    candidate.metrics={**candidate.metrics,'hypothesis':idea};session.commit();proposed+=1
                break
        progress('Сравниваю варианты и проверяю условия изменения демоправил')
        decisions=pipeline.advance(session,cached,now_ms)
        trials=list(session.scalars(select(StrategyVersion).where(StrategyVersion.status=='paper_trial')))
        for trial in trials:
            trial.metrics={**trial.metrics,'paper_progress':cached(trial,trial.trial_start_ts)}
        for (version_id,start),result in cache.items():
            if start is None:
                version=session.get(StrategyVersion,version_id)
                version.metrics={**version.metrics,'wf_symbols':result.get('symbols',symbols),'wf_checked_ts':now_ms}
        session.commit()
        versions=list(session.scalars(select(StrategyVersion).where(StrategyVersion.parent_id.is_not(None))
                       .order_by(StrategyVersion.created_ts.desc()).limit(12)))
        for version in versions:
            hypothesis=version.metrics.get('hypothesis',{})
            before=version.metrics.get('wf_benchmark',{})
            after=version.metrics.get('wf',{})
            next_step=('Минимум 14 дней новых данных и 20 закрытых симулированных сделок; положительный результат и соблюдение риска.'
                       if version.status=='paper_trial' else 'Вариант не применяется; сохраняется контрольная версия.'
                       if version.status=='rejected' else 'Наблюдать результат, не повышая риск.')
            report['experiments'].append({'strategy':label(version),'version_id':version.id,'status':version.status,
                'question':hypothesis.get('question',version.rationale),
                'reason':hypothesis.get('reason','Заранее заданная проверка параметра; связь с ошибками сделок не установлена.'),
                'source':hypothesis.get('source','predeclared'),'before':before,'candidate':after,
                'parameter_diff':{k:{'before':session.get(StrategyVersion,version.parent_id).params.get(k),'after':v}
                                  for k,v in version.params.items() if session.get(StrategyVersion,version.parent_id).params.get(k)!=v},
                'paired_delta':paired_delta(before,after),'forward':version.metrics.get('paper_progress',version.metrics.get('paper',{})),
                'forward_start_ts':version.trial_start_ts,'next_step':next_step})
        changes=list(session.scalars(select(PromotionEvent).where(PromotionEvent.id>event_id,
                        PromotionEvent.step.in_(['promote','rollback']),PromotionEvent.passed==True)))
        report['actual_changes']=[{'event_id':x.id,'version_id':x.version_id,'action':x.step,'detail':x.detail} for x in changes]
        finished=clock()
        report.update(status='complete',finished_ts=finished,proposed=proposed,lessons=lessons,
                      change_summary=('Рабочие демоправила изменены по записанным событиям. Финансовое улучшение ещё не доказано.'
                                      if changes else 'Рабочие правила не изменены. Исторический успех не равен улучшению работающего агента.'),
                      next_step='Собирать закрытые сделки и новые рыночные данные; продолжить испытания без повышения риска.',
                      next_due_ts=now_ms+SIX_HOURS,decisions=decisions)
        set_state(session,'research_last_run',str(now_ms));set_state(session,'research_symbols',json.dumps(sorted(symbols)))
        set_state(session,'research_status',json.dumps({'status':'исследование завершено','finished_ts':finished,
                  'summary':report['change_summary'],'proposed':proposed,'lessons':lessons},ensure_ascii=False))
        set_state(session,'research_progress',json.dumps({'status':'complete','stage':report['change_summary'],'heartbeat_ts':finished},ensure_ascii=False))
        return save_report(session,report,report_dir)
    except Exception as error:
        session.rollback()
        if 'event_id' in locals():
            changes=list(session.scalars(select(PromotionEvent).where(PromotionEvent.id>event_id,
                PromotionEvent.step.in_(['promote','rollback']),PromotionEvent.passed==True)))
            report['actual_changes']=[{'event_id':x.id,'version_id':x.version_id,'action':x.step,'detail':x.detail} for x in changes]
        report.update(status='failed',finished_ts=clock(),error=type(error).__name__,
                      change_summary='Нет подтверждения успешного завершения; прежний успешный отчёт сохранён.',
                      next_step='Проверить вычисления и повторить исследование. Не менять риск.')
        set_state(session,'research_progress',json.dumps({'status':'failed','stage':report['change_summary'],'heartbeat_ts':clock()},ensure_ascii=False))
        return save_report(session,report,report_dir)
