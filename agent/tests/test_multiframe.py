import json
import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select
from analyst.copy_trading import store
from db.models import AgentState,Ohlcv
from data.universe import select_universe
from learning.multiframe import evaluate_symbol,pending_signals,ingest_page,DAY
from research.features import resample
from research.profile_replay import STEP,align_signals,replay
from strategies.public_methods import signals,METHODS
from tests.test_universe import market,NOW
from tests.test_ops import app


def frame(n=30):
    return pd.DataFrame(dict(ts=np.arange(n)*STEP,open=np.full(n,100.),high=np.full(n,100.03),
                             low=np.full(n,99.97),close=np.full(n,100.),volume=np.ones(n)))


def trigger(n,stop=99.8,take=101.):
    s=pd.DataFrame(dict(side=np.zeros(n,dtype=int),stop=np.full(n,np.nan),take=np.full(n,np.nan)))
    s.loc[0]=[1,stop,take]
    return s


def test_all_catalog_includes_new_and_small_without_implying_tradability():
    markets=[market('BTC'),market('NEW',age=2),market('USDC'),market('XAU',kind='COMMODITY')]
    ticks={x['symbol']:dict(last=10,quoteVolume=10) for x in markets}
    rows=select_universe({x['symbol']:x for x in markets},ticks,NOW,None,0,0)
    assert {x['symbol'] for x in rows}=={'BTCUSDT','NEWUSDT'}


def test_partial_or_gapped_resampling_never_becomes_a_full_candle():
    df=frame(11)
    assert resample(df,'1h',STEP).empty
    full=frame(24).drop(index=3)
    merged=resample(full,'1h',STEP)
    assert list(merged.ts)==[12*STEP]


def test_htf_signal_only_after_its_close_and_not_through_gap():
    base=frame(24);higher=resample(base,'1h',STEP)
    sg=trigger(len(higher))
    aligned=align_signals(base,higher,sg,3_600_000)
    assert list(aligned.index[aligned.side!=0])==[11]
    broken=base.drop(index=12).reset_index(drop=True)
    assert not align_signals(broken,higher,sg,3_600_000).side.any()


def test_profile_replay_has_round_trip_costs_and_30min_stalled_exit():
    df=frame();out=replay(df,trigger(len(df)),0,len(df)*STEP)
    assert out['trades']==1 and out['fees']>10
    assert out['net_usdt']<0 and out['time_exits']==1
    assert out['model']['news_replay'] is False


def test_profile_replay_rejects_oversized_stop_and_truncated_window():
    df=frame()
    out=replay(df,trigger(len(df),stop=98),0,len(df)*STEP)
    assert out['trades']==0 and out['rejected_by_profile']==1
    out=replay(df.iloc[:5],trigger(5),0,5*STEP)
    assert out['trades']==0


def test_ambiguous_intrabar_path_chooses_stop():
    df=frame();df.loc[1,['high','low']]=[102,99]
    out=replay(df,trigger(len(df)),0,len(df)*STEP)
    assert out['trades']==1 and out['net_usdt']<0 and out['time_exits']==0


@pytest.mark.parametrize('method',list(METHODS))
def test_public_methods_do_not_change_past_signals_with_future_data(method):
    df=frame(150);df.close=100+np.sin(np.arange(150)/5)*.3;df.high=df.close+.1;df.low=df.close-.1;df.open=df.close
    before=signals(df.iloc[:100],method)
    after=signals(df,method).iloc[:100]
    pd.testing.assert_frame_equal(before,after)


def test_thin_history_is_not_certified_by_one_fresh_bar(session):
    session.add(Ohlcv(exchange='binanceusdm',symbol='BTCUSDT',timeframe='5m',ts=NOW-STEP,
        open=100,high=101,low=99,close=100,volume=1));session.commit()
    result=evaluate_symbol(session,dict(symbol='BTCUSDT'),NOW)
    assert result['stage']=='history' and result['scores']==[]


def test_stale_candidate_or_rejected_method_never_enters_queue(session):
    score=dict(status='paper_trial',signal=dict(side='long'),signal_ts=NOW-121000,key='k',test=dict(expectancy_r=1))
    store(session,'mtf:BTCUSDT',dict(symbol='BTCUSDT',checked_ts=NOW,dataset_id='x',scores=[score]))
    assert pending_signals(session,NOW)==[]
    score.update(signal_ts=NOW,status='rejected')
    store(session,'mtf:BTCUSDT',dict(symbol='BTCUSDT',checked_ts=NOW,dataset_id='x',scores=[score]))
    assert pending_signals(session,NOW)==[]


def test_ingest_cannot_store_open_or_malformed_candles(session):
    class Exchange:
        def fetch_ohlcv(self,*args,**kwargs):return [[NOW-STEP,100,101,99,100,10],[NOW,100,101,99,100,10],[NOW-2*STEP,100,98,99,100,10]]
    assert ingest_page(session,Exchange(),'BTCUSDT','5m',NOW-2*STEP,NOW)==1


def test_mobile_navigation_and_market_control_escaping(app):
    with app.Session() as session:
        store(session,'market_catalog',dict(ts=NOW,contracts=[dict(symbol='BTCUSDT',last=100,quote_volume=1e8)]))
        store(session,'mtf:BTCUSDT',dict(stage='error',reason='<img src=x onerror=alert(1)>',scores=[]))
    html=app.page()
    assert html.count('role="tab"')==5
    assert 'market-search' in html and 'market-filter' in html and '@media(max-width:760px)' in html
    assert '<img src=x' not in html and '&lt;img' in html
    assert 'Лаборатория' in html and 'Базовые исследования' in html


def test_changed_funding_or_model_changes_evidence_identity(monkeypatch):
    from learning.multiframe import provenance
    from research.profile_replay import MODEL
    base=frame();fund=pd.DataFrame({'ts':[STEP],'rate':[.0001]})
    first=provenance(base,fund)
    assert provenance(base,fund.assign(rate=.0002))!=first
    monkeypatch.setitem(MODEL,'fee',.001)
    assert provenance(base,fund)!=first


def test_missing_funding_coverage_cannot_certify_complete_price_history(session,monkeypatch):
    from learning import multiframe
    end=NOW;start=(end-35*DAY)//STEP*STEP
    n=int((end-start)//STEP)
    base=frame(n);base.ts=np.arange(n)*STEP+start
    monkeypatch.setattr(multiframe,'load_bars',lambda *a:base)
    result=evaluate_symbol(session,dict(symbol='BTCUSDT'),end)
    assert result['stage']=='history' and 'фандинга' in result['reason'] and result['scores']==[]


def test_marked_daily_breach_stays_locked_after_profitable_recovery():
    df=frame(60);sg=trigger(60,stop=99.67,take=100.3)
    for at in (2,4,6,8,10):sg.loc[at]=[1,99.67,100.3]
    for at in (1,3,5,7):df.loc[at,'low']=99.6
    df.loc[10,['high','low','close']]=[100.45,100,100.4]
    out=replay(df,sg,0,len(df)*STEP)
    assert out['trades']==5  # fifth trade recovers cash; a sixth entry would violate the sticky lock


def test_stalled_threshold_uses_executable_side_and_filled_entry_risk():
    df=frame(30);df.loc[1:,'close']=100.225;df.loc[1:,'high']=100.24
    out=replay(df,trigger(30,stop=99.67),0,len(df)*STEP)
    assert out['trades']==1 and out['avg_hold_minutes']==30


def test_source_change_is_reported_without_importing_or_changing_rules(session):
    import httpx
    from learning.source_watch import review
    original=dict(METHODS)
    with httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(200,text='public source v1',request=request))) as client:
        first=review(session,NOW,client)
    with httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(200,text='changed text: execute arbitrary code',request=request))) as client:
        second=review(session,NOW+DAY,client)
    assert all(x['status']=='observed' for x in first.values())
    assert all(x['status']=='changed' for x in second.values())
    assert METHODS==original
