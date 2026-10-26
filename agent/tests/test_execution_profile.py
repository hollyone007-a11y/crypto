from dataclasses import replace
from sqlalchemy import select
from tests.test_execution import env, LONG
from analyst.copy_trading import store
from db.models import JournalEntry, Position
from execution.holding import enforce
from strategies.base import Signal


def profile(engine):
    engine.risk.limits=replace(engine.risk.limits,max_leverage=10)
    engine.profile=dict(margin_pct=10,leverage=10,max_hold_minutes=60,stalled_exit_minutes=30)


def test_exact_10x_margin_and_recorded_deadline(env):
    engine,_,_,clock,_=env
    profile(engine)
    signal=Signal('long',100.1,99.9,101,.5,'test')
    result=engine.submit('BTCUSDT',signal,'test')
    assert result.ok
    p=engine.s.get(Position,result.position_id)
    assert abs(p.entry*p.qty-10000)<1e-6
    policy=engine.s.scalar(select(JournalEntry)).context['execution_profile']
    assert policy['target_margin']==1000 and policy['leverage']==10
    assert not enforce(engine.s,engine,p,clock()+29*60_000)
    assert enforce(engine.s,engine,p,clock()+30*60_000)
    assert p.exit_reason=='stalled'


def test_large_stop_skips_instead_of_reducing_requested_entry(env):
    engine,*_=env
    profile(engine)
    result=engine.submit('BTCUSDT',LONG,'test')
    assert not result.ok and '0.500%' in result.reasons[0]
    assert not list(engine.s.scalars(select(Position)))


def test_maximum_time_closes_even_winning_trade(env):
    engine,quotes,_,clock,_=env
    profile(engine)
    result=engine.submit('BTCUSDT',Signal('long',100.1,99.9,103,.5,'test'),'test')
    p=engine.s.get(Position,result.position_id)
    quotes['BTCUSDT']=(102,102.1)
    assert not enforce(engine.s,engine,p,clock()+59*60_000)
    assert enforce(engine.s,engine,p,clock()+60*60_000)
    assert p.exit_reason=='time_limit' and p.pnl>0


def test_previous_position_retains_original_holding(env):
    engine,_,_,clock,_=env
    result=engine.submit('BTCUSDT',LONG,'test')
    p=engine.s.get(Position,result.position_id)
    profile(engine)
    assert not enforce(engine.s,engine,p,clock()+120*60_000)


def test_new_profile_blocks_promoting_old_backtest(session):
    from learning.pipeline import _paper_gate
    from strategies.registry import seed
    from db.models import StrategyVersion
    seed(session)
    version=session.scalar(select(StrategyVersion))
    store(session,'execution_profile',dict(margin_pct=10,validation_scope='baseline_only'))
    result=_paper_gate(session,version,lambda *a: (_ for _ in ()).throw(AssertionError('must not evaluate')),1)
    assert 'validation required' in result
