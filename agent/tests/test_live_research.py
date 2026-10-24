import json
import math
import pytest
import httpx
from sqlalchemy import select,func
from db.models import AgentState,AnalystRun,Ohlcv,StrategyVersion
from learning.lab import diagnose,hypotheses,paired_delta,run_research
from learning.walkforward import beats
from learning.evaluate import frozen_evaluator
from ops.feed import LiveFeed,StaleQuote
from ops import dashboard
from tests.test_ops import app,Clock,FakeEx
from tests.test_learning import stats,prop,evaluator,NOW,DAY
from learning import pipeline


def bars(session,symbol,latest=NOW-3600000):
    for i in range(210):
        session.add(Ohlcv(exchange='binanceusdm',symbol=symbol,timeframe='1h',ts=latest-i*3600000,
                          open=100,high=101,low=99,close=100,volume=1))
    session.commit()


def test_feed_reuses_closed_bars_within_minute():
    clock=Clock();ex=FakeEx(clock);calls=[]
    original=ex.fetch_ohlcv
    def count(*args,**kwargs):calls.append(args);return original(*args,**kwargs)
    ex.fetch_ohlcv=count
    feed=LiveFeed(ex,['BTCUSDT'],clock=clock)
    feed.refresh();clock.t+=5;feed.refresh()
    assert len(calls)==1
    clock.t+=60;feed.refresh()
    assert len(calls)==2


def test_one_fresh_coin_does_not_hide_stale_coin(session):
    bars(session,'BTCUSDT');bars(session,'ETHUSDT',latest=NOW-4*3600000)
    def forbidden(*args):raise AssertionError('must not evaluate incomplete universe')
    report=run_research(session,forbidden,['BTCUSDT','ETHUSDT'],NOW,exchange='binanceusdm')
    assert report['status']=='waiting'
    assert report['coverage']['missing_or_stale_symbols']==['ETHUSDT']
    assert report['actual_changes']==[]


def test_zero_trades_means_predeclared_hypothesis_not_learned_error(session):
    from strategies.registry import seed
    seed(session);parent=pipeline.current_prod(session,'trend_donchian')
    facts=diagnose(session,parent);options=hypotheses(parent,facts)
    assert facts['sample_size']==0
    assert options and all(x['source']=='predeclared' for x in options)


def test_different_snapshots_do_not_produce_improvement_delta():
    base={**stats(.5),'dataset_id':'a','cutoff_ts':NOW,'symbols':['BTCUSDT'],'cost_model':{}}
    new={**base,'dataset_id':'b','sharpe':2.}
    assert paired_delta(base,new) is None


def test_nan_control_is_rejected():
    control=stats(.5);control['sharpe']=math.nan
    assert not beats(stats(1),control)[0]


def test_oos_pass_is_not_reported_as_applied_change(session):
    bars(session,'BTCUSDT')
    candidate=pipeline.propose_params(session,prop(channel=40))
    def ev(version,start):
        if start is not None:return stats(0.,trades=0,sum_r=0.)
        return stats(1. if version.id==candidate.id else .5)
    report=run_research(session,ev,['BTCUSDT'],NOW,exchange='binanceusdm')
    assert report['status']=='complete' and candidate.status=='paper_trial'
    assert report['actual_changes']==[]


def test_failed_attempt_preserves_successful_report(session):
    bars(session,'BTCUSDT')
    session.add(AgentState(key='research_report',value='{"run_id":"previous"}'));session.commit()
    def broken(*args):raise RuntimeError('private detail should not be exposed')
    report=run_research(session,broken,['BTCUSDT'],NOW,exchange='binanceusdm')
    assert report['status']=='failed'
    assert json.loads(session.get(AgentState,'research_report').value)['run_id']=='previous'
    assert 'private detail' not in json.dumps(report)


def test_live_shell_no_meta_refresh_and_report_endpoint(app):
    html=app.page()
    assert 'http-equiv="refresh"' not in html
    assert '/api/live?since=' in html and 'markPrice@1s' in html
    assert 'Что агент улучшает и почему' in html
    server=dashboard.serve(app.page,port=0,snapshot_fn=app.live_snapshot)
    try:
        base=f'http://127.0.0.1:{server.server_address[1]}'
        assert httpx.get(base+'/api/live').json()['mode']=='paper'
        assert httpx.get(base+'/research-report.txt').status_code==200
    finally:server.shutdown()


def test_report_text_escapes_persisted_hypothesis(app):
    report={'started_ts':NOW,'finished_ts':NOW,'status':'complete','run_id':'x','goal':'x',
            'coverage':{'symbols':[]},'observations':[],'actual_changes':[],
            'experiments':[{'strategy':'x','status':'rejected','question':'<img src=x onerror=alert(1)>',
             'reason':'<script>alert(1)</script>','source':'predeclared','candidate':{},'before':{},
             'paired_delta':None,'forward':{},'next_step':'wait'}]}
    with app.Session() as s:
        s.add(AgentState(key='research_attempt',value=json.dumps(report)));s.commit()
    html=app.page()
    assert '<img src=x' not in html and '&lt;img' in html


def test_stale_exchange_timestamp_is_not_a_fresh_quote():
    clock=Clock();ex=FakeEx(clock)
    ex.fetch_ticker=lambda symbol:{'bid':99,'ask':101,'timestamp':int((clock()-31)*1000)}
    feed=LiveFeed(ex,['BTCUSDT'],clock=clock)
    with pytest.raises(StaleQuote):feed.refresh()
    assert not feed.book


def test_frozen_dataset_is_unchanged_when_database_changes(session):
    bars(session,'BTCUSDT')
    from strategies.registry import seed
    seed(session);parent=pipeline.current_prod(session,'meanrev_z')
    evaluate=frozen_evaluator(session,['BTCUSDT'],NOW)
    first=evaluate(parent,None)
    candle=session.scalar(select(Ohlcv).order_by(Ohlcv.ts).limit(1))
    candle.close=500;session.commit()
    assert evaluate(parent,None)['dataset_id']==first['dataset_id']
    assert frozen_evaluator(session,['BTCUSDT'],NOW)(parent,None)['dataset_id']!=first['dataset_id']
