import json
import pytest
from sqlalchemy import select
from tests.test_execution import env,LONG
from tests.test_multiframe import frame,trigger
from research.profile_replay import replay
from db.models import JournalEntry,Position,Lesson,AgentState
from execution.holding import enforce
from learning.autonomous import review_rules
from ops.quality import audit
from analyst.copy_trading import store


def active(engine):
    engine.profile=dict(margin_pct=0,leverage=10,active_demo=True,max_hold_minutes=45,stalled_exit_minutes=20)


def test_active_profile_exits_earlier_without_risk_increase():
    bars=frame(30);sg=trigger(30)
    base=replay(bars,sg,0,len(bars)*300000)
    active=replay(bars,sg,0,len(bars)*300000,policy='active')
    assert base['avg_hold_minutes']==30 and active['avg_hold_minutes']==20
    assert base['model']['risk_pct']==active['model']['risk_pct']==.5


def test_active_open_slot_and_two_daily_entries_are_hard_caps(env):
    engine,_,_,clock,_=env
    active(engine)
    first=engine.submit('BTCUSDT',LONG,'mtf_bb_cont_5m_active')
    assert first.ok
    denied=engine.submit('ETHUSDT',LONG,'mtf_bb_cont_15m_active')
    assert not denied.ok and '2 входа' in denied.reasons[0]
    engine.broker.close(engine.s.get(Position,first.position_id),'manual')
    second=engine.submit('BTCUSDT',LONG,'mtf_bb_cont_5m_active')
    assert second.ok
    engine.broker.close(engine.s.get(Position,second.position_id),'manual')
    assert not engine.submit('BTCUSDT',LONG,'mtf_bb_cont_5m_active').ok


def test_legacy_timeout_can_be_enforced_without_relabelling_leverage(env):
    engine,_,_,clock,_=env
    result=engine.submit('BTCUSDT',LONG,'legacy')
    p=engine.s.get(Position,result.position_id)
    engine.profile=dict(max_hold_minutes=60,stalled_exit_minutes=30,apply_holding_to_legacy=True)
    assert enforce(engine.s,engine,p,clock()+61*60000)
    assert p.exit_reason=='legacy_time'
    assert 'execution_profile' not in engine.s.scalar(select(JournalEntry)).context


def test_winning_lesson_does_not_describe_a_nonexistent_loss(env):
    engine,quotes,_,clock,_=env
    result=engine.submit('BTCUSDT',LONG,'test')
    p=engine.s.get(Position,result.position_id)
    quotes['BTCUSDT']=(103,103.1)
    engine.journal.record_close(engine.broker.close(p,'manual'))
    review_rules(engine.s,clock())
    lesson=engine.s.scalar(select(Lesson))
    assert lesson.outcome=='win' and 'Причина убытка' not in lesson.mistake
    lesson.mistake='Причина убытка не установлена; это наблюдение, не причинный вывод.';engine.s.commit()
    review_rules(engine.s,clock())
    assert 'Причина убытка' not in lesson.mistake


def test_quality_report_never_fabricates_predictive_accuracy(session):
    report=audit(session,123000)
    assert report['accuracy_pct'] is None and report['closed_total']==0
    store(session,'priority_research',dict(ts=123000,last_success_ts=123000,symbols=['BTCUSDT'],successful_observations=[dict(symbol='BTCUSDT',latest_closed_ts=120000)]))
    assert audit(session,124000)['feeds'][0]['status']=='на связи'
    assert 'устарела' in audit(session,400000)['feeds'][0]['status']


def test_failed_refresh_cannot_report_exchange_healthy(session):
    store(session,'priority_research',dict(ts=123000,symbols=[],errors=[dict(symbol='BTCUSDT')],successful_observations=[]))
    assert 'без свежих' in audit(session,124000)['feeds'][0]['status']


def test_active_replay_enforces_two_daily_entries():
    bars=frame(80);sg=trigger(80)
    for at in (10,20,30,40,50,60):sg.loc[at]=[1,99.8,101]
    out=replay(bars,sg,0,len(bars)*300000,policy='active')
    assert out['trades']==2


def test_priority_drops_completed_coin_that_is_no_longer_liquid(session,monkeypatch):
    from learning import multiframe
    now=100*86400000
    store(session,'market_catalog',dict(ts=now,contracts=[dict(symbol='AAAUSDT',quote_volume=100_000_000,listed_ts=1),dict(symbol='BBBUSDT',quote_volume=1_000_000,listed_ts=1)]))
    store(session,'mtf:BBBUSDT',dict(symbol='BBBUSDT',stage='evaluated'))
    called=[]
    monkeypatch.setattr(multiframe,'refresh_history',lambda s,e,symbol,t:called.append(symbol))
    monkeypatch.setattr(multiframe,'evaluate_symbol',lambda s,row,t:dict(symbol=row['symbol'],stage='history',latest_5m=t-300000))
    result=multiframe.refresh_priority(session,object(),now)
    assert called==['AAAUSDT'] and result['symbols']==['AAAUSDT']


def test_future_dated_study_cannot_supply_a_live_candidate(session):
    from learning.multiframe import pending_signals,RULES_REVISION
    now=1000000
    store(session,'mtf:BTCUSDT',dict(symbol='BTCUSDT',rules_revision=RULES_REVISION,checked_ts=now+1000,dataset_id='x',scores=[dict(status='paper_trial',signal={'side':'long'},signal_ts=now,key='x',test={'expectancy_r':1})]))
    assert pending_signals(session,now)==[]
