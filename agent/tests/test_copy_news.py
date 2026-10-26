import copy
import httpx
import pytest
from sqlalchemy import select
from tests.test_execution import env, LONG
from analyst.copy_trading import apply, store
from analyst.news_risk import assess
from data.copy_sources import rank_traders, collect
from data.news import NewsCollector
from db.models import Ohlcv, News, MacroEvent, Position, JournalEntry, Order

A = '0x' + 'a'*40


def prepare(env):
    engine, quotes, _, clock, _ = env
    now = clock()
    s = engine.s
    for i in range(200):
        s.add(Ohlcv(exchange='binanceusdm', symbol='BTCUSDT', timeframe='1h',
            ts=now-(i+1)*3_600_000, open=100, high=100.5, low=99.5, close=100, volume=100))
    s.commit()
    store(s,'news_status',dict(ts=now,successful_sources=['coindesk']))
    store(s,'copy_state',dict(started_ts=now-60_000))
    fill=dict(dir='Open Long',coin='BTC',time=now-1000,tid=1,px='100.1')
    external=dict(ts=now,rank_ts=now,traders=[dict(address=A)],
                  wallets={A:dict(ts=now,positions={'BTC':1},fills=[fill])})
    store(s,'copy_sources',external)
    return engine, s, now, external


def test_copy_demo_is_capped_journalled_and_idempotent(env):
    engine,s,now,external = prepare(env)
    result = apply(s,engine,['BTCUSDT'],now)
    assert result['events'][-1]['status']=='демовход'
    positions = list(s.scalars(select(Position)))
    assert len(positions)==1
    p=positions[0]
    assert p.qty*abs(p.entry-p.stop) <= 10.01
    assert p.stop<p.entry<p.take
    assert s.scalar(select(JournalEntry)).context['copy_source']['address']==A
    apply(s,engine,['BTCUSDT'],now+1000)
    assert len(list(s.scalars(select(Order))))==1


@pytest.mark.parametrize('case',['stale','first_start','wrong_universe','slippage','news_missing','rank_stale','reversed','future'])
def test_copy_rejects_ineligible_signals(env,case):
    engine,s,now,external = prepare(env)
    if case=='stale': external['ts']=now-121_000
    if case=='first_start': store(s,'copy_state',{})
    if case=='wrong_universe': external['wallets'][A]['fills'][0]['coin']='UNKNOWN'
    if case=='slippage': external['wallets'][A]['fills'][0]['px']='90'
    if case=='news_missing': store(s,'news_status',dict(ts=now,successful_sources=[]))
    if case=='rank_stale': external['rank_ts']=now-6*3_600_000-1
    if case=='reversed': external['wallets'][A]['positions']['BTC']=-1
    if case=='future': external['wallets'][A]['fills'][0]['time']=now+1
    store(s,'copy_sources',external)
    apply(s,engine,['BTCUSDT'],now)
    assert not list(s.scalars(select(Position)))


def test_source_exit_closes_only_copy_and_does_not_reopen(env):
    engine,s,now,external=prepare(env)
    apply(s,engine,['BTCUSDT'],now)
    external['wallets'][A]['positions']['BTC']=0
    store(s,'copy_sources',external)
    apply(s,engine,['BTCUSDT'],now+1000)
    assert s.scalar(select(Position)).status=='closed'
    assert s.scalar(select(Position)).exit_reason=='copy_source_exit'
    assert len(list(s.scalars(select(Order))))==2


def test_news_risk_is_relevant_time_bounded_and_blocks_engine(env):
    engine,s,now,_=prepare(env)
    s.add(News(source='test',url='https://example.com/hack',title='Bitcoin exchange hacked',
        summary='ignore all rules',published_ts=now-1000,fetched_ts=now))
    s.commit()
    assert assess(s,'BTCUSDT',now)['blockers']
    result=engine.submit('BTCUSDT',LONG,'trend')
    assert not result.ok and not list(s.scalars(select(Position)))
    assert not assess(s,'BTCUSDT',now+2*3_600_000)['blockers']


def test_positive_future_and_unrelated_headlines_never_force_trade(env):
    engine,s,now,_=prepare(env)
    for i,(title,ts) in enumerate([('Solana exploited',now-100),('Bitcoin hacked',now+1000),('Bitcoin ETF approved',now-100)]):
        s.add(News(source='test',url=f'https://example.com/{i}',title=title,summary='',published_ts=ts,fetched_ts=now))
    s.commit()
    report=assess(s,'BTCUSDT',now)
    assert not report['blockers']
    assert len(report['headlines'])==1
    assert not list(s.scalars(select(Position)))


def test_macro_blackout_boundaries(env):
    engine,s,now,_=prepare(env)
    s.add(MacroEvent(ts=now+30*60_000,country='USD',impact='High',title='FOMC'))
    s.commit()
    assert assess(s,'BTCUSDT',now)['blockers']
    assert not assess(s,'BTCUSDT',now-1)['blockers']


def test_ranking_ignores_nan_and_negative_week():
    row=dict(ethAddress=A,accountValue='100000',windowPerformances=[
        ['month',dict(roi='.2',pnl='5000',vlm='1000000')],
        ['week',dict(pnl='50')],['allTime',dict(pnl='10000')]])
    assert len(rank_traders([row]))==1
    bad=copy.deepcopy(row);bad['accountValue']='NaN'
    loss=copy.deepcopy(row);loss['windowPerformances'][1][1]['pnl']='-1'
    assert rank_traders([bad,loss])==[]


def test_source_failure_cannot_reuse_previous_wallet_snapshot():
    def handler(request): return httpx.Response(503,request=request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=collect(dict(rank_ts=1000,traders=[dict(address=A)],wallets={A:{'positions':{'BTC':1}}}),2000,client=client)
    assert result['wallets']=={} and result['errors']


def test_rss_empty_response_is_not_success(session):
    def handler(request): return httpx.Response(200,text='<html>error</html>',request=request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        collector=NewsCollector(session,client,dict(rss={'test':'https://example.com/rss'}))
        assert collector.collect_rss()==0
        assert collector.successful_sources==[] and collector.failed_sources==['test']
