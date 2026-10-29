from types import SimpleNamespace
import pandas as pd
from sqlalchemy import select,func
from analyst.copy_trading import store
from db.models import SignalObservation as Observation,PaperAccount,Position,Order,Ohlcv
from learning import observatory as lab
from tests.test_multiframe import frame
from tests.test_ops import app

NOW=100*86400000


def observation(**kwargs):
    data=dict(id='one',revision=lab.REVISION,symbol='BTCUSDT',method='bb_cont',timeframe='5m',policy='active',mode='forward',
        status='pending',created_ts=NOW-1,signal_ts=NOW-1000,entry_due_ts=NOW,cursor_ts=0,side=1,reference=100,
        stop=99,take=102,funding_pct=0.,prediction_n=0)
    return Observation(**{**data,**kwargs})


def bar(ts=NOW,high=100.1,low=99.9,close=100):
    return SimpleNamespace(ts=ts,open=100.,high=high,low=low,close=close)


def coverage(end):return dict(start_ts=NOW-lab.DAY,complete_to=end)


def test_capture_is_idempotent_and_predictions_are_before_outcomes(session,monkeypatch):
    monkeypatch.setattr(lab,'METHODS',{'bb_cont':{}})
    def generated(bars,method):
        out=pd.DataFrame(dict(side=[0]*len(bars),stop=[99.]*len(bars),take=[102.]*len(bars)))
        out.loc[len(bars)-1,'side']=1
        return out
    monkeypatch.setattr(lab,'signals',generated)
    base=frame(300);base.ts+=NOW-300*lab.STEP
    store(session,'signal_learning',dict(revision=lab.REVISION,cohorts=[dict(mode='forward',method='bb_cont',timeframe='5m',policy='active',count=5,mean_r=.2)]))
    assert lab.capture(session,'BTCUSDT',base,NOW+500)==4
    assert lab.capture(session,'BTCUSDT',base,NOW+500)==0
    items=list(session.scalars(select(Observation)))
    assert all(x.mode=='forward' and x.entry_due_ts>x.created_ts for x in items)
    chosen=next(x for x in items if x.timeframe=='5m' and x.policy=='active')
    assert chosen.prediction_r==.2 and chosen.prediction_n==5
    assert session.scalar(select(func.count()).select_from(Position))==0


def test_late_capture_is_explicitly_retrospective(session,monkeypatch):
    monkeypatch.setattr(lab,'METHODS',{'bb_cont':{}})
    def generated(bars,method):
        out=pd.DataFrame(dict(side=[0]*len(bars),stop=[99.]*len(bars),take=[102.]*len(bars)))
        out.loc[len(bars)-3,'side']=1
        return out
    monkeypatch.setattr(lab,'signals',generated)
    base=frame(300);base.ts+=NOW-300*lab.STEP
    lab.capture(session,'BTCUSDT',base,NOW)
    assert all(x.mode=='replay' and x.prediction_r is None for x in session.scalars(select(Observation)))


def test_stop_first_fees_funding_and_no_double_charge():
    item=observation();fund=[SimpleNamespace(ts=NOW+1,rate=.001)]
    lab.advance(None,item,[bar(high=103,low=98)],fund,coverage(NOW+lab.STEP),NOW+lab.STEP)
    assert item.status=='closed' and item.reason=='stop' and item.net_pct<0 and item.fees_pct>.1
    assert item.funding_pct<0
    first=item.funding_pct
    lab.advance(None,item,[bar(high=103,low=98)],fund,coverage(NOW+lab.STEP),NOW+lab.STEP)
    assert item.funding_pct==first


def test_future_and_unfunded_bars_never_become_completed_evidence():
    item=observation()
    lab.advance(None,item,[bar(high=103)],[],coverage(NOW+lab.STEP),NOW)
    assert item.entry is None and item.status=='pending'
    lab.advance(None,item,[bar(high=103)],[],{},NOW+lab.STEP)
    assert item.entry is None and 'funding' in item.reason


def test_gap_is_excluded_and_never_filled_by_a_fabricated_bar():
    item=observation()
    lab.advance(None,item,[bar(ts=NOW+lab.STEP)],[],coverage(NOW+2*lab.STEP),NOW+2*lab.STEP)
    assert item.status=='gap' and item.net_pct is None


def test_learning_updates_future_only_prior_and_never_changes_account(session):
    session.add(PaperAccount(id=1,cash=10000,initial=10000))
    for i in range(24):
        session.add(observation(id=str(i),status='closed',mode='forward' if i<20 else 'replay',
            entry=100.,entry_ts=NOW,exit_ts=NOW+lab.STEP,net_r=.2 if i<10 else -.1,net_pct=.1 if i<10 else -.05,
            fees_pct=.11,prediction_r=.3 if i<20 else None,prediction_n=5,reason='stalled',signal_ts=NOW+i))
    session.commit()
    report=lab.learn(session,NOW+lab.STEP)
    forward=next(x for x in report['cohorts'] if x['mode']=='forward')
    assert forward['count']==20 and forward['heldout_mean_pct']<0
    assert report['predictions_scored']==20 and report['prediction_mae_r'] is not None
    assert report['actual_rule_changes']==0 and session.get(PaperAccount,1).cash==10000
    assert session.scalar(select(func.count()).select_from(Order))==0


def test_resolver_finishes_virtual_outcome_without_account_trade(session):
    session.add(observation())
    session.add(Ohlcv(exchange='binanceusdm',symbol='BTCUSDT',timeframe='5m',ts=NOW,open=100,high=103,low=99.9,close=102,volume=1))
    session.commit();store(session,'funding_coverage:BTCUSDT',coverage(NOW+lab.STEP))
    lab.cycle(session,NOW+lab.STEP)
    item=session.get(Observation,'one')
    assert item.status=='closed' and item.reason=='take' and item.net_r>0
    assert session.scalar(select(func.count()).select_from(Position))==0


def test_ui_separates_observations_and_demo_trades(app):
    html=app.page()
    assert 'id="signal-learning"' in html and 'не сделки демосчёта' in html
    scheduler=__import__('ops.run',fromlist=['build_scheduler']).build_scheduler(app)
    assert scheduler.get_job('signal_learning').executor=='observations'
