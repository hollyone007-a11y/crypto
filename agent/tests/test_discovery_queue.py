from analyst.copy_trading import store
from analyst.news_risk import read_state
from db.models import Ohlcv
from learning import multiframe as m
from tests.test_ops import app
from tests.test_universe import market,NOW
from sqlalchemy import select


def catalog(n=12):
    return [dict(symbol=f'C{i:02}USDT',quote_volume=100_000_000-i,listed_ts=NOW-100*m.DAY) for i in range(n)]


def test_rotation_visits_contracts_beyond_fixed_liquid_leaders():
    rows=catalog();cursor=0;seen=set()
    for _ in range(5):
        chosen,rotated,size=m.priority_queue(rows,{},cursor,NOW,4)
        assert len(chosen)==4 and len({x['symbol'] for x in chosen})==4
        seen.update(x['symbol'] for x in chosen)
        cursor=(cursor+len(rotated))%size
    assert seen=={x['symbol'] for x in rows}


def test_qualified_contract_precedes_high_volume_and_invalid_revision_does_not():
    rows=catalog()
    records={rows[-1]['symbol']:dict(stage='evaluated',rules_revision=m.RULES_REVISION,scores=[dict(status='paper_trial')])}
    chosen,_,_=m.priority_queue(rows,records,0,NOW,4)
    assert chosen[0]['symbol']==rows[-1]['symbol']
    records[rows[-1]['symbol']]['rules_revision']='obsolete'
    chosen,_,_=m.priority_queue(rows,records,0,NOW,4)
    assert rows[-1]['symbol'] not in [x['symbol'] for x in chosen]


def test_young_and_illiquid_are_not_sent_to_full_validation():
    rows=catalog(4);rows[2]['listed_ts']=NOW-2*m.DAY;rows[3]['quote_volume']=1
    chosen,_,_=m.priority_queue(rows,{},0,NOW,8)
    assert {x['symbol'] for x in chosen}=={rows[0]['symbol'],rows[1]['symbol']}


def test_catalog_reloads_metadata_and_detects_new_contracts(session):
    class Exchange:
        reloads=[]
        def load_markets(self,reload=False):
            self.reloads.append(reload)
            return {x['symbol']:x for x in [market('BTC'),market('NEW',age=2)]}
        def fetch_tickers(self):
            return {x['symbol']:dict(last=10,quoteVolume=1e8) for x in [market('BTC'),market('NEW',age=2)]}
    ex=Exchange()
    store(session,'market_catalog',dict(ts=NOW-60001,contracts=[dict(symbol='BTCUSDT')]))
    m.refresh_catalog(session,ex,NOW)
    assert ex.reloads==[True]
    state=read_state(session,'market_discovery')
    assert state['added']==['NEWUSDT'] and state['young_contracts'][0]['symbol']=='NEWUSDT'
    m.refresh_catalog(session,ex,NOW+60001)
    assert ex.reloads==[True,False]
    m.refresh_catalog(session,ex,NOW+600001)
    assert ex.reloads==[True,False,True]


def test_rotation_cursor_persists_even_if_one_exchange_call_fails(session,monkeypatch):
    rows=catalog(8)
    store(session,'market_catalog',dict(ts=NOW,contracts=rows))
    def refresh(session,exchange,symbol,now):
        if symbol=='C02USDT': raise ValueError('network')
    monkeypatch.setattr(m,'refresh_history',refresh)
    monkeypatch.setattr(m,'evaluate_symbol',lambda s,r,n:dict(symbol=r['symbol'],latest_5m=n-m.STEP,stage='history',scores=[]))
    first=m.refresh_priority(session,None,NOW,4)
    second=m.refresh_priority(session,None,NOW+1,4)
    assert first['errors']==[dict(symbol='C02USDT',error='ValueError')]
    assert first['rotation_cursor']==2 and second['rotation_cursor']==4
    assert first['attempted_symbols'][2:]!=second['attempted_symbols'][2:]
    assert len(second['visited_symbols'])==5


def test_interior_candle_gap_is_repaired_without_fabrication(session,monkeypatch):
    step=m.STEP;now=(NOW//step)*step;start=now-42*m.DAY
    for ts in [start,start+step,start+3*step,now-step]:
        session.add(Ohlcv(exchange='binanceusdm',symbol='BTCUSDT',timeframe='5m',ts=ts,open=100,high=101,low=99,close=100,volume=1))
    session.commit()
    calls=[]
    class Exchange:
        def fetch_ohlcv(self,symbol,tf,since,limit):
            calls.append(since)
            return [[start+2*step,100,101,99,100,1]] if since==start+2*step else []
        def fetch_funding_rate_history(self,*a,**k):return []
    m.refresh_history(session,Exchange(),'BTCUSDT',now)
    assert start+2*step in calls
    assert session.scalar(select(Ohlcv.id).where(Ohlcv.ts==start+2*step)) is not None


def test_discovery_is_visible_and_escaped(app):
    with app.Session() as s:
        store(s,'market_discovery',dict(young_contracts=[dict(symbol='<img>',listed_ts=NOW,quote_volume=1e8)]))
    html=app.page()
    assert 'id="market-discovery"' in html and '&lt;img&gt;' in html and '<img>' not in html
