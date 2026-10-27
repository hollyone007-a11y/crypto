import pandas as pd
from learning.multiframe import refresh_live_signals
from tests.test_multiframe import frame
from db.models import JournalEntry


def record(now):
    return dict(symbol='BTCUSDT',stage='evaluated',dataset_id='frozen-validation',checked_ts=now-10000,
        cutoff_ts=now-10000,window={'test_end':now-10000},scores=[dict(key='mtf_bb_cont_5m',method='bb_cont',
        timeframe='5m',policy='balanced',status='rejected',test={'net_usdt':-5},train={'net_usdt':10},
        signal={'side':'long'},signal_ts=now-10000)])


def test_live_update_preserves_historical_provenance_and_clears_stale_entry(session):
    now=30*300000;old=record(now);base=frame(30)
    updated=refresh_live_signals(session,old,base,pd.DataFrame(),now,'new-live-data')
    assert updated['dataset_id']=='frozen-validation' and updated['cutoff_ts']==old['cutoff_ts']
    assert updated['evaluated_ts']==old['checked_ts'] and updated['checked_ts']==now
    assert updated['live_dataset_id']=='new-live-data'
    assert updated['scores'][0]['test']=={'net_usdt':-5} and 'signal' not in updated['scores'][0]
    assert 'signal' in old['scores'][0]


def test_realized_loss_guard_runs_even_when_historical_scores_are_cached(session):
    now=30*300000;old=record(now);old['scores'][0]['status']='paper_trial'
    for i in range(10):
        session.add(JournalEntry(position_id=i+1,symbol='BTCUSDT',strategy='mtf_bb_cont_5m',side='long',
            thesis='',signal={},regime='',context={},entry_ts=i,entry=100,qty=1,exit_ts=i+1,exit=99,pnl=-1))
    session.commit()
    updated=refresh_live_signals(session,old,frame(30),pd.DataFrame(),now,'new')
    assert updated['scores'][0]['status']=='rejected' and updated['scores'][0]['paper_net']==-10


def prepared_history(session,monkeypatch,age):
    from learning import multiframe
    from analyst.copy_trading import store
    now=100*86400000;start=now-42*86400000
    base=frame(42*288);base.ts=base.ts+start
    monkeypatch.setattr(multiframe,'load_bars',lambda *args:base)
    monkeypatch.setattr(multiframe,'provenance',lambda *args:'fresh-live-data')
    old=record(now);old.update(rules_revision=multiframe.RULES_REVISION,checked_ts=now-age)
    store(session,'mtf:BTCUSDT',old)
    store(session,'funding_coverage:BTCUSDT',dict(start_ts=start,complete_to=now))
    return multiframe,now


def test_recent_validation_is_not_replayed_on_every_live_poll(session,monkeypatch):
    module,now=prepared_history(session,monkeypatch,60000)
    monkeypatch.setattr(module,'replay',lambda *args: (_ for _ in ()).throw(AssertionError('history must be cached')))
    result=module.evaluate_symbol(session,dict(symbol='BTCUSDT'),now)
    assert result['historical_reused'] and result['dataset_id']=='frozen-validation'
    assert result['checked_ts']==now


def test_six_hour_old_validation_is_recalculated(session,monkeypatch):
    module,now=prepared_history(session,monkeypatch,6*3600000)
    calls=[]
    def fake_replay(*args):
        calls.append(1)
        return dict(trades=0,net_usdt=0,expectancy_r=0,t_stat=0,max_dd_pct=0)
    monkeypatch.setattr(module,'replay',fake_replay)
    result=module.evaluate_symbol(session,dict(symbol='BTCUSDT'),now)
    assert len(calls)==36 and result['evaluated_ts']==now
