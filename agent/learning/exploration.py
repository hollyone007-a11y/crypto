"""Explicitly opted-in forward paper research; never strict validation."""
import math


def eligible(score):
    test=score.get('test',{})
    values=[test.get('net_usdt',0),test.get('expectancy_r',0),test.get('max_dd_pct',-100),score.get('train',{}).get('net_usdt')]
    return (score.get('policy')=='active' and score.get('timeframe') in ('5m','15m')
        and score.get('status')=='insufficient_sample' and test.get('trades',0)>=1
        and all(isinstance(x,(int,float)) and math.isfinite(x) for x in values)
        and values[0]>0 and values[1]>0 and values[2]>-10 and values[0]+values[3]>0)


def key(score):
    return 'mtf_exp_'+score['method']+'_'+score['timeframe']+'_active'


def pending(session,now):
    import json
    from sqlalchemy import select
    from analyst.news_risk import read_state
    from db.models import AgentState
    from learning.multiframe import RULES_REVISION
    if not read_state(session,'execution_profile').get('experimental_demo_enabled'):return []
    found=[]
    for row in session.scalars(select(AgentState).where(AgentState.key.like('mtf:%'))):
        item=json.loads(row.value)
        if (item.get('rules_revision')!=RULES_REVISION or item.get('stage')!='evaluated'
            or not 0<=now-item.get('checked_ts',0)<=120_000
            or not 0<=now-item.get('evaluated_ts',item.get('checked_ts',0))<6*3_600_000):continue
        for score in item.get('scores',[]):
            signal=score.get('exploration_signal')
            if eligible(score) and signal and not score.get('exploration_paused') and 0<=now-score.get('exploration_signal_ts',0)<=120_000:
                found.append({**score,'symbol':item['symbol'],'dataset_id':item['dataset_id'],
                    'key':key(score),'signal':signal,'signal_ts':score['exploration_signal_ts'],'experimental':True})
    # Coverage first; a tiny lucky backtest is not treated as the best trader.
    return sorted(found,key=lambda x:(x.get('exploration_closed',0),x['signal_ts'],x['symbol'],x['key']))
