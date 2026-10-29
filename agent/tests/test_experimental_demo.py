import copy
import pandas as pd
from sqlalchemy import select
from analyst.copy_trading import store
from db.models import JournalEntry,Position
from learning.exploration import eligible,pending,key
from learning.multiframe import RULES_REVISION,add_exploration
from tests.test_multiframe import frame
from tests.test_execution import env
from tests.test_ops import app
from strategies.base import Signal
from dataclasses import replace

NOW=100*86400000


def score():
    return dict(key='mtf_bb_cont_5m_active',method='bb_cont',timeframe='5m',policy='active',status='insufficient_sample',
        train=dict(trades=4,net_usdt=-20),test=dict(trades=2,net_usdt=10,expectancy_r=.2,max_dd_pct=-1),
        exploration_signal=dict(side='long',entry=100,stop=99.9,take=101,confidence=.5,rationale='test'),exploration_signal_ts=NOW)


def test_research_eligibility_is_not_strict_validation():
    good=score();assert eligible(good) and good['status']=='insufficient_sample'
    for change in [dict(policy='balanced'),dict(timeframe='4h'),dict(status='rejected'),dict(test=dict(trades=0,net_usdt=10)),dict(test=dict(trades=1,net_usdt=-1,expectancy_r=.1,max_dd_pct=-1))]:
        assert not eligible({**good,**change})


def test_opt_in_freshness_and_strict_queue_separation(session):
    from learning.multiframe import pending_signals
    item=dict(symbol='BTCUSDT',rules_revision=RULES_REVISION,stage='evaluated',checked_ts=NOW,evaluated_ts=NOW,dataset_id='frozen',scores=[score()])
    store(session,'mtf:BTCUSDT',item)
    assert pending(session,NOW)==[]
    store(session,'execution_profile',dict(experimental_demo_enabled=True))
    candidates=pending(session,NOW)
    assert len(candidates)==1 and candidates[0]['experimental'] and candidates[0]['key']=='mtf_exp_bb_cont_5m_active'
    assert pending_signals(session,NOW)==[]
    assert pending(session,NOW+121000)==[]
    item['rules_revision']='old';store(session,'mtf:BTCUSDT',item)
    assert pending(session,NOW)==[]


def test_three_losing_forward_trades_pause_exploration(session,monkeypatch):
    from learning import multiframe as m
    store(session,'execution_profile',dict(experimental_demo_enabled=True))
    s=score();base=frame(30);now=30*m.STEP
    for i in range(3):
        session.add(JournalEntry(position_id=i+1,symbol='BTCUSDT',strategy=key(s),side='long',signal={},context={},entry_ts=i,entry=100,qty=1,exit_ts=i+1,pnl=-1))
    session.commit()
    item=dict(symbol='BTCUSDT',scores=[s])
    result=add_exploration(session,item,base,pd.DataFrame(),now)
    assert result['scores'][0]['exploration_paused'] and 'exploration_signal' not in result['scores'][0]


def test_fresh_exploration_signal_preserves_unqualified_history(session,monkeypatch):
    from learning import multiframe as m
    store(session,'execution_profile',dict(experimental_demo_enabled=True))
    base=frame(30);now=30*m.STEP
    def generated(bars,method):
        return pd.DataFrame(dict(side=[1]*len(bars),stop=[99.9]*len(bars),take=[101.]*len(bars)))
    monkeypatch.setattr(m,'signals',generated)
    original=score();item=dict(symbol='BTCUSDT',scores=[copy.deepcopy(original)])
    result=add_exploration(session,item,base,pd.DataFrame(),now)
    updated=result['scores'][0]
    assert updated['status']=='insufficient_sample' and updated['train']==original['train']
    assert updated['test']==original['test'] and updated['exploration_signal']['take']==100.75
    assert updated['exploration_signal_ts']==now


def test_global_one_position_and_two_entries_even_across_strategies(env):
    eng,quotes,_,clock,_=env
    eng.risk.limits=replace(eng.risk.limits,max_leverage=10)
    eng.profile=dict(margin_pct=10,leverage=10,experimental_mode=True,experimental_entry=True)
    signal=Signal('long',100.1,99.9,101,.5,'test')
    first=eng.submit('BTCUSDT',signal,'mtf_exp_bb_cont_5m_active')
    assert first.ok
    assert not eng.submit('BTCUSDT',signal,'unrelated').ok
    eng.broker.close(eng.s.get(Position,first.position_id),'test')
    second=eng.submit('BTCUSDT',signal,'unrelated')
    assert second.ok
    eng.broker.close(eng.s.get(Position,second.position_id),'test')
    third=eng.submit('BTCUSDT',signal,'third')
    assert not third.ok and '2' in third.reasons[0]


def test_experimental_mode_does_not_bypass_stop_risk(env):
    eng,*_=env
    eng.risk.limits=replace(eng.risk.limits,max_leverage=10)
    eng.profile=dict(margin_pct=10,leverage=10,experimental_mode=True)
    result=eng.submit('BTCUSDT',Signal('long',100.1,98,105,.5,'wide'),'mtf_exp_bb_cont_5m_active')
    assert not result.ok and '0.500%' in result.reasons[0]


def test_real_app_experimental_candidate_opens_and_is_labeled(app,monkeypatch):
    from learning import exploration
    for name,value in dict(multiframe_enabled=True,active_demo_enabled=False,experimental_demo_enabled=True,entry_margin_pct=10,entry_leverage=10,max_hold_minutes=60,stalled_exit_minutes=30).items():
        object.__setattr__(app.settings,name,value)
    app.limits=replace(app.limits,max_leverage=10,max_risk_per_trade_pct=.5)
    candidate=dict(symbol='BTCUSDT',method='bb_cont',timeframe='5m',policy='active',key='mtf_exp_bb_cont_5m_active',signal_ts=app.now_ms(),dataset_id='test',experimental=True,signal=score()['exploration_signal'])
    candidate['signal']['entry']=100.05  # current ask in FakeEx; no entry drift
    monkeypatch.setattr(exploration,'pending',lambda *a:[candidate])
    app.multiframe_execute()
    with app.Session() as s:
        entry=s.scalar(select(JournalEntry))
        assert entry.context['execution_profile']['experimental_entry']
        assert entry.context['execution_profile']['max_hold_minutes']==45
        assert 'малая выборка' in entry.thesis
