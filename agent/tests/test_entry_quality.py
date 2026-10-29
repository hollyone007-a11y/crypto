from dataclasses import replace
from sqlalchemy import select
from db.models import Position,JournalEntry
from strategies.base import Signal
from tests.test_execution import env


def test_reversed_acu_breakout_is_rejected_before_risk_sizing(env):
    engine,quotes,*_=env
    quotes['ACUUSDT']=(.13552,.13553)
    engine.risk.limits=replace(engine.risk.limits,max_leverage=10)
    engine.profile=dict(margin_pct=10,leverage=10,experimental_entry=True)
    result=engine.submit('ACUUSDT',Signal('long',.1373,.13509203552373367,.14061194671439953,.5,'observed'),
        'mtf_exp_donchian_5m_active',context={'mtf':{'experimental':True}})
    assert not result.ok and 'Цена ушла' in result.reasons[0]
    assert not list(engine.s.scalars(select(Position)))


def test_short_reversal_and_chasing_favorable_move_are_both_rejected(env):
    engine,quotes,*_=env
    for side,bid,ask,stop,take in [('short',101,101.1,102,96),('long',101,101.1,98,104)]:
        quotes['BTCUSDT']=(bid,ask)
        result=engine.submit('BTCUSDT',Signal(side,100,stop,take,.5,'test'),'mtf_test',context={'mtf':{'key':'test'}})
        assert not result.ok and 'Цена ушла' in result.reasons[0]


def test_near_price_entry_is_allowed_and_guard_is_journaled(env):
    engine,*_=env
    result=engine.submit('BTCUSDT',Signal('long',100,98,106,.5,'test'),'mtf_test',context={'mtf':{'key':'test'}})
    assert result.ok
    assert engine.s.scalar(select(JournalEntry)).context['entry_quality']['revision']=='entry-drift-v1'


def test_fresh_submit_rechecks_loss_even_if_scan_has_not_refreshed(env):
    engine,_,_,clock,_=env
    engine.profile=dict(experimental_entry=True)
    engine.s.add(JournalEntry(position_id=99,symbol='BTCUSDT',strategy='mtf_exp_test_active',side='long',signal={},context={},entry_ts=clock()-2000,entry=100,qty=1,exit_ts=clock()-1000,pnl=-1))
    engine.s.commit()
    result=engine.submit('BTCUSDT',Signal('long',100,98,106,.5,'test'),'mtf_exp_test_active')
    assert not result.ok and '24 часа' in result.reasons[0]
    clock.t+=86_400_000
    assert engine.submit('BTCUSDT',Signal('long',100,98,106,.5,'test'),'mtf_exp_test_active').ok
