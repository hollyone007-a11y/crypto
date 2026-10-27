import copy
import numpy as np
import pandas as pd
from learning.trader_observations import observe
from analyst.news_risk import read_state
from ops.trading_research import chart_link,panel
from research.supertrend import calculate
from strategies.public_methods import signals

A='0x'+'a'*40
NOW=1793000000000


def snapshot():
    return dict(wallets={A:dict(fills=[dict(time=NOW-1000,tid=1,hash='h',coin='BTC',dir='Close Long',px='100',sz='2',closedPnl='4',fee='.1',feeToken='USDC')])})


def test_first_seen_fills_are_idempotent_and_not_counted_as_full_trades(session):
    data=snapshot();observe(session,data,NOW);observe(session,data,NOW+1000)
    state=read_state(session,'trader_observations:'+A)
    assert state['fill_count']==1 and state['closing_fill_count']==1
    assert state['reported_closed_pnl']==4
    assert 'Неизвестна' in state['records'][0]['strategy']
    assert 'net_pnl' not in state


def test_modified_replay_cannot_rewrite_first_observation(session):
    data=snapshot();observe(session,data,NOW)
    data['wallets'][A]['fills'][0]['closedPnl']='500'
    observe(session,data,NOW+1000)
    assert read_state(session,'trader_observations:'+A)['records'][0]['reported_closed_pnl']==4


def test_gaps_and_future_fills_are_not_hidden(session):
    data=snapshot();observe(session,data,NOW)
    data['wallets'][A]['fills'][0].update(time=NOW+500000,tid=2)
    observe(session,data,NOW+180000)
    state=read_state(session,'trader_observations:'+A)
    assert state['fill_count']==1 and state['coverage_gaps']==1


def test_links_and_source_text_do_not_execute_markup(session):
    assert chart_link('BTCUSDT').endswith('BINANCE%3ABTCUSDT.P')
    data=snapshot();data['wallets'][A]['fills'][0]['coin']='<img onerror=alert(1)>'
    observe(session,data,NOW)
    html=panel(session)
    assert '<img onerror' not in html and '&lt;img' in html
    assert 'не подключены' in html and 'Strategy Tester' in html


def market(n):
    close=100+np.sin(np.arange(n)/9)*2
    return pd.DataFrame(dict(ts=np.arange(n)*300000,open=close,high=close+.2,low=close-.2,close=close,volume=1))


def test_supertrend_past_is_unchanged_by_future_bars():
    bars=market(400)
    early=calculate(bars.iloc[:200]);later=calculate(bars)
    pd.testing.assert_series_equal(early[0],later[0].iloc[:200])
    pd.testing.assert_series_equal(early[1],later[1].iloc[:200])
    pd.testing.assert_frame_equal(signals(bars.iloc[:200],'st_flip'),signals(bars,'st_flip').iloc[:200])


def test_supertrend_signals_use_valid_stop_and_target():
    bars=market(400);generated=signals(bars,'st_flip');active=generated[generated.side!=0]
    assert len(active)>0
    for i,row in active.iterrows():
        assert row.side*(bars.close.iloc[i]-row['stop'])>0
        assert row.side*(row['take']-bars.close.iloc[i])>0
