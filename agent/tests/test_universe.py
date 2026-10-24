import math
from data.universe import DAY, select_universe
from ops.feed import LiveFeed
from learning.walkforward import walk_forward
from strategies.baseline import EmaAdxTrend
from tests.fakes import synthetic_market
from sqlalchemy import select
from db.models import Ohlcv, StrategyVersion
from tests.test_ops import app,open_long
from tests.test_binance_paper import rules_app,candidate
from analyst import rules
from learning.autonomous import research
from tests.test_learning import stats

NOW=1_792_454_400_000


def market(base,age=100,active=True,kind='COIN'):
    name=base+'/USDT:USDT'
    return {'symbol':name,'id':base+'USDT','base':base,'active':active,'linear':True,
            'swap':True,'quote':'USDT','settle':'USDT',
            'info':{'onboardDate':NOW-age*DAY,'underlyingType':kind,
                    'contractType':'PERPETUAL' if kind=='COIN' else 'TRADIFI_PERPETUAL'}}


def test_universe_ranks_volume_and_excludes_non_crypto_new_stable_and_inactive():
    rows=[market('BTC'),market('ETH'),market('NEW',age=3),market('USDC'),
          market('DEAD',active=False),market('XAU',kind='COMMODITY'),market('SOXL',kind='EQUITY')]
    markets={m['symbol']:m for m in rows}
    tickers={m['symbol']:{'quoteVolume':100_000_000+i*10_000_000,'last':10.} for i,m in enumerate(rows)}
    result=select_universe(markets,tickers,NOW)
    assert [r['symbol'] for r in result]==['ETHUSDT','BTCUSDT']


def test_universe_does_not_use_nonfinite_or_illiquid_prices():
    rows=[market('BTC'),market('ETH'),market('ALT')]
    ticks={rows[0]['symbol']:{'quoteVolume':math.inf,'last':10},
           rows[1]['symbol']:{'quoteVolume':1,'last':10},
           rows[2]['symbol']:{'quoteVolume':90_000_000,'last':math.nan}}
    assert select_universe({r['symbol']:r for r in rows},ticks,NOW)==[]


def test_batch_feed_keeps_healthy_coin_when_another_fails():
    class Exchange:
        def fetch_quote_batch(self,symbols):
            return {symbols[0]:{'bid':99,'ask':101}}
        def fetch_ohlcv(self,*args,**kwargs): return []
    feed=LiveFeed(Exchange(),['BTCUSDT','ETHUSDT'])
    feed.refresh()
    assert feed.quotes('BTCUSDT')==(99,101)
    assert 'ETHUSDT' not in feed.book


def test_validation_keeps_per_coin_evidence():
    bars,funding=synthetic_market(1500)
    result=walk_forward(EmaAdxTrend(),{'BTCUSDT':(bars,funding)})
    assert result['per_symbol']['BTCUSDT']['trades']==result['trades']


def test_removed_coin_with_open_position_remains_monitored(app):
    open_long(app)
    app.settings=app.settings.model_copy(update={'exchange':'binanceusdm','universe_enabled':True,'universe_size':2})
    rows=[market('ETH'),market('SOL')]
    for row in rows:
        row['info']['onboardDate']=app.now_ms()-100*DAY
    app.market_exchange.load_markets=lambda **kwargs:{r['symbol']:r for r in rows}
    app.market_exchange.fetch_tickers=lambda:{r['symbol']:{'last':100,'quoteVolume':100_000_000} for r in rows}
    app.refresh_universe()
    assert set(app.symbols)=={'BTCUSDT','SOLUSDT','ETHUSDT'}
    assert not app.market_ready


def test_profitable_pool_does_not_certify_losing_coin(rules_app,monkeypatch):
    monkeypatch.setattr(rules,'build_candidates',lambda *args:([candidate()],{}))
    with rules_app.Session() as s:
        for v in s.scalars(select(StrategyVersion)):
            v.metrics={'wf':{'trades':100,'sharpe':1.,'expectancy_r':.2,
                            'per_symbol':{'BTCUSDT':{'trades':30,'sharpe':-1.,'expectancy_r':-.1}}},
                       'wf_symbols':['BTCUSDT']}
        s.commit()
    result=rules_app.analyst_cycle()
    assert not result['submitted'] and result['opportunities'][0]['status']=='отклонён'


def test_research_evaluator_uses_fixed_symbol_snapshot(app,monkeypatch):
    monkeypatch.setattr('ops.app.frozen_evaluator',lambda s,symbols,cutoff:(lambda v,start:list(symbols)))
    original=list(app.symbols)
    evaluate=app._evaluator(None)
    app.symbols=['SOLUSDT']
    assert evaluate(None,None)==original


def test_changed_universe_is_not_blocked_by_previous_research_timer(session):
    for symbol in ['BTCUSDT','ETHUSDT']:
        for i in range(210):
            session.add(Ohlcv(exchange='binanceusdm',symbol=symbol,timeframe='1h',ts=NOW-(i+1)*3600000,
                              open=100,high=101,low=99,close=100,volume=1))
    session.commit()
    ev=lambda v,start:stats(.5)
    assert research(session,ev,['BTCUSDT'],NOW)['status']!='not_due'
    assert research(session,ev,['BTCUSDT'],NOW+1)['status']=='not_due'
    assert research(session,ev,['ETHUSDT'],NOW+2)['status']!='not_due'
