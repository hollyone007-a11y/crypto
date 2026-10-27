"""Bounded rolling all-contract research; predeclared methods, paired calendar, no code generation."""
import hashlib
import copy
import json
import shutil
import time
import pandas as pd
from sqlalchemy import select, func, delete
from analyst.copy_trading import store
from analyst.news_risk import read_state
from config.settings import ROOT, to_ccxt_symbol
from data.copy_sources import number
from db.models import AgentState, Ohlcv, JournalEntry, FundingRate
from db.session import insert_ignore
from research.features import resample
from research.profile_replay import STEP, MODEL, POLICIES, align_signals, replay
from strategies.public_methods import METHODS, TIMEFRAMES, signals

DAY=86_400_000
HISTORY_DAYS=42
RULES_REVISION=hashlib.sha256((ROOT/'strategies'/'public_methods.py').read_bytes()+
                            (ROOT/'research'/'profile_replay.py').read_bytes()).hexdigest()[:20]


def load_bars(session,symbol,tf,now):
    q=select(Ohlcv.ts,Ohlcv.open,Ohlcv.high,Ohlcv.low,Ohlcv.close,Ohlcv.volume).where(
        Ohlcv.exchange=='binanceusdm',Ohlcv.symbol==symbol,Ohlcv.timeframe==tf,
        Ohlcv.ts>=now-HISTORY_DAYS*DAY,Ohlcv.ts+(STEP if tf=='5m' else 3_600_000)<=now).order_by(Ohlcv.ts)
    return pd.DataFrame(session.execute(q).all(),columns=['ts','open','high','low','close','volume'])


def ingest_page(session,exchange,symbol,tf,since,now,limit=1000):
    step=TIMEFRAMES[tf]
    page=exchange.fetch_ohlcv(to_ccxt_symbol(symbol),tf,since=since,limit=limit)
    rows=[]
    for bar in page:
        ts=int(bar[0]); values=[number(x) for x in bar[1:6]]
        if ts%step or ts+step>now or ts<now-(HISTORY_DAYS+1)*DAY: continue
        o,h,l,c,v=values
        if min(o,h,l,c)<=0 or v<0 or h<max(o,c,l) or l>min(o,c,h): continue
        rows.append(dict(exchange='binanceusdm',symbol=symbol,timeframe=tf,ts=ts,
                         open=o,high=h,low=l,close=c,volume=v))
    return insert_ignore(session,Ohlcv,rows)


def refresh_history(session,exchange,symbol,now):
    """At most two 5m pages and one funding page; higher TFs derive from the same 5m data."""
    if shutil.disk_usage(ROOT).free<2_000_000_000:
        raise RuntimeError('Менее 2 GB свободного места: сбор остановлен, имеющиеся данные сохранены.')
    for tf in ('5m',):
        step=TIMEFRAMES[tf]
        first,last=session.execute(select(func.min(Ohlcv.ts),func.max(Ohlcv.ts)).where(
            Ohlcv.exchange=='binanceusdm',Ohlcv.symbol==symbol,Ohlcv.timeframe==tf)).one()
        since=max(now-HISTORY_DAYS*DAY,(last-step if last else now-1000*step))
        ingest_page(session,exchange,symbol,tf,since,now)
        first=session.scalar(select(func.min(Ohlcv.ts)).where(Ohlcv.exchange=='binanceusdm',
            Ohlcv.symbol==symbol,Ohlcv.timeframe==tf))
        if first and first>now-HISTORY_DAYS*DAY+step:
            ingest_page(session,exchange,symbol,tf,max(now-HISTORY_DAYS*DAY,first-1000*step),now)
    key='funding_coverage:'+symbol
    coverage=read_state(session,key)
    since=coverage.get('cursor',now-HISTORY_DAYS*DAY)
    page=exchange.fetch_funding_rate_history(to_ccxt_symbol(symbol),since=since,limit=200)
    rows=[dict(exchange='binanceusdm',symbol=symbol,ts=int(x['timestamp']),rate=number(x['fundingRate']))
          for x in page if x.get('timestamp') and since<=int(x['timestamp'])<=now]
    insert_ignore(session,FundingRate,rows)
    cursor=max((x['ts']+1 for x in rows),default=since)
    store(session,key,dict(start_ts=coverage.get('start_ts',since),cursor=cursor,
                          complete_to=now if len(page)<200 else cursor-1,checked_ts=now))


def evaluate_symbol(session,row,now):
    symbol=row['symbol'];cutoff=(now//STEP)*STEP
    base=load_bars(session,symbol,'5m',cutoff)
    hourly=resample(base,'1h',STEP) if not base.empty else base.copy()
    result=dict(symbol=symbol,checked_ts=now,cutoff_ts=cutoff,stage='history',scores=[],
                latest_5m=int(base.ts.max()) if len(base) else 0,base_bars=len(base),reason='Собирается общая история 5м; старшие ТФ строятся из неё.',model=MODEL,rules_revision=RULES_REVISION)
    # Identical scored dates across every strategy and timeframe. Ten days precede training for warm-up.
    test_start=cutoff-14*DAY;train_start=cutoff-28*DAY;warmup_start=cutoff-38*DAY
    result['window']=dict(train_start=train_start,train_end=test_start-3_600_000,test_start=test_start,test_end=cutoff)
    if base.empty or hourly.empty or int(base.ts.min())>warmup_start or int(hourly.ts.min())>warmup_start:
        return result
    if cutoff-int(base.ts.max())>STEP or cutoff-int(hourly.ts.max())>2*3_600_000:
        result['reason']='Данные устарели; новые входы запрещены.';return result
    expected=base.loc[base.ts>=warmup_start,'ts'].diff().dropna()
    if not expected.eq(STEP).all():
        result['reason']='Пропуски 5-минутных свечей: сравнение не засчитано.';return result
    funding=pd.DataFrame(session.execute(select(FundingRate.ts,FundingRate.rate).where(
        FundingRate.exchange=='binanceusdm',FundingRate.symbol==symbol,
        FundingRate.ts>=cutoff-HISTORY_DAYS*DAY,FundingRate.ts<cutoff).order_by(FundingRate.ts)).all(),columns=['ts','rate'])
    funding_coverage=read_state(session,'funding_coverage:'+symbol)
    if funding_coverage.get('start_ts',cutoff)>train_start or funding_coverage.get('complete_to',0)<cutoff:
        result['reason']='История фандинга не покрывает scored-окно; нулевой фандинг не предполагается.'
        return result
    digest=provenance(base,funding)
    previous=read_state(session,'mtf:'+symbol)
    evaluated_ts=previous.get('evaluated_ts',previous.get('checked_ts',0))
    if (previous.get('stage')=='evaluated' and previous.get('rules_revision')==RULES_REVISION
            and 0<=now-evaluated_ts<6*3_600_000):
        return refresh_live_signals(session,previous,base,hourly,now,digest)
    result.update(stage='evaluated',dataset_id=digest,reason='Сравнение на общей истории; 5м replay, без исторического новостного фильтра и общей книги портфеля.')
    result['evaluated_ts']=now
    for tf,step in TIMEFRAMES.items():
        bars=base.copy() if tf=='5m' else resample(base,'15min',STEP) if tf=='15m' else hourly.copy() if tf=='1h' else resample(hourly,'4h',3_600_000)
        if bars.empty or len(bars[bars.ts<train_start])<60:
            result['reason']='Недостаточно общей прогревочной истории для '+tf;result['stage']='history';return result
        variants=[(method,policy) for method in METHODS for policy in ('balanced','active') if policy=='balanced' or tf in ('5m','15m')]
        for method,policy in variants:
            generated=signals(bars,method)
            generated['take']=bars.close+(generated['take']-bars.close)*POLICIES[policy]['target_multiplier']
            aligned=align_signals(base,bars,generated,step)
            train=replay(base,aligned,train_start,test_start-3_600_000,funding,policy)
            test=replay(base,aligned,test_start,cutoff,funding,policy)
            eligible=(train['trades']>=20 and test['trades']>=20 and train['net_usdt']>0
                      and test['net_usdt']>0 and test['expectancy_r']>0 and test['t_stat']>=3
                      and test['max_dd_pct']>-10)
            key=f'mtf_{method}_{tf}'+('_active' if policy=='active' else '')
            closed=list(session.scalars(select(JournalEntry).where(JournalEntry.strategy==key,
                JournalEntry.symbol==symbol,JournalEntry.exit_ts.is_not(None)).order_by(JournalEntry.exit_ts.desc()).limit(20)))
            actual_net=sum(x.pnl or 0 for x in closed)
            if len(closed)>=10 and actual_net<0: eligible=False
            status='paper_trial' if eligible else 'rejected' if test['trades']>=20 else 'insufficient_sample'
            score=dict(method=method,timeframe=tf,policy=policy,key=key,train=train,test=test,status=status,
                       paper_closed=len(closed),paper_net=round(actual_net,4),dataset_id=digest,
                       reason='Только экспериментальная демоторговля; независимые будущие результаты ещё не подтверждены.' if eligible else
                       'Отрицательная серия демосделок: повторный вход приостановлен.' if len(closed)>=10 and actual_net<0 else
                       'Нужны 20+ train и 20+ test, положительный чистый результат и t≥3; перебор гипотез не доказывает будущую доходность.')
            if eligible:
                last=generated.iloc[-1];close_ts=int(bars.ts.iloc[-1])+step
                if last.side and 0<=now-close_ts<=120_000:
                    score['signal']=dict(side='long' if last.side>0 else 'short',entry=float(bars.close.iloc[-1]),
                        stop=float(last['stop']),take=float(last['take']),confidence=.55,
                        rationale=METHODS[method]['name']+' · '+tf+' · эксперимент после отложенной проверки')
                    score['signal_ts']=close_ts
            result['scores'].append(score)
    return result


def refresh_live_signals(session,previous,base,hourly,now,live_dataset_id):
    """Keep validation evidence frozen; update executable signals and realized-loss guards only."""
    result=copy.deepcopy(previous)
    result.update(checked_ts=now,evaluated_ts=previous.get('evaluated_ts',previous.get('checked_ts',0)),
                  live_cutoff_ts=(now//STEP)*STEP,live_dataset_id=live_dataset_id,
                  latest_5m=int(base.ts.max()),base_bars=len(base),historical_reused=True)
    frames={'5m':base,'15m':resample(base,'15min',STEP),'1h':hourly,'4h':resample(hourly,'4h',3_600_000)}
    generated_cache={}
    for score in result.get('scores',[]):
        score.pop('signal',None);score.pop('signal_ts',None)
        closed=list(session.scalars(select(JournalEntry).where(JournalEntry.symbol==result['symbol'],
            JournalEntry.strategy==score['key'],JournalEntry.exit_ts.is_not(None)).order_by(JournalEntry.exit_ts.desc()).limit(20)))
        score.update(paper_closed=len(closed),paper_net=round(sum(x.pnl or 0 for x in closed),4))
        if len(closed)>=10 and score['paper_net']<0:
            score.update(status='rejected',reason='Отрицательная серия демосделок: повторный вход приостановлен.')
        if score['status']!='paper_trial':continue
        tf,method,policy=score['timeframe'],score['method'],score.get('policy','balanced')
        bars=frames[tf]
        if bars.empty:continue
        key=(tf,method)
        if key not in generated_cache: generated_cache[key]=signals(bars,method)
        last=generated_cache[key].iloc[-1];close_ts=int(bars.ts.iloc[-1])+TIMEFRAMES[tf]
        if last.side and 0<=now-close_ts<=120_000:
            entry=float(bars.close.iloc[-1]);take=entry+(float(last['take'])-entry)*POLICIES[policy]['target_multiplier']
            score['signal']=dict(side='long' if last.side>0 else 'short',entry=entry,stop=float(last['stop']),
                take=take,confidence=.55,rationale=METHODS[method]['name']+' · '+tf+' · свежий сигнал после отдельной исторической проверки')
            score['signal_ts']=close_ts
    return result


def provenance(base,funding):
    digest=hashlib.sha256(pd.util.hash_pandas_object(base,index=False).values.tobytes()+
                          pd.util.hash_pandas_object(funding,index=False).values.tobytes())
    digest.update(json.dumps(MODEL,sort_keys=True).encode())
    digest.update((ROOT/'strategies'/'public_methods.py').read_bytes())
    digest.update((ROOT/'research'/'profile_replay.py').read_bytes())
    return digest.hexdigest()[:20]


def run_batch(session,exchange,now,batch_size=3,budget_seconds=40):
    session.execute(delete(Ohlcv).where(Ohlcv.exchange=='binanceusdm',Ohlcv.timeframe=='5m',Ohlcv.ts<now-45*DAY))
    session.commit()
    catalog_state=read_state(session,'market_catalog')
    if now-catalog_state.get('ts',0)>=60_000:
        from data.universe import select_universe
        markets=exchange.load_markets()
        tickers=exchange.fetch_tickers()
        catalog=select_universe(markets,tickers,now,size=None,min_volume=0,min_listing_days=0)
        if catalog:
            catalog_state=dict(ts=now,contracts=catalog,scope='Binance crypto USDT perpetual')
            store(session,'market_catalog',catalog_state)
    catalog=catalog_state.get('contracts',[])
    state=read_state(session,'mtf_scan');cursor=state.get('cursor',0)%max(1,len(catalog))
    started=time.monotonic();done=0
    # Preserve a stable catalog order; every eligible contract receives a turn, not only winners.
    contracts=sorted(catalog,key=lambda x:x['symbol'])
    for offset in range(min(batch_size,len(contracts))):
        row=contracts[(cursor+offset)%len(contracts)];symbol=row['symbol']
        try:
            if row.get('quote_volume',0)<5_000_000 or now-row.get('listed_ts',now)<30*DAY:
                result=dict(symbol=symbol,checked_ts=now,stage='filtered',scores=[],
                            reason='Наблюдение: объём ниже 5 млн USDT/сутки или листинг моложе 30 дней.')
            else:
                refresh_history(session,exchange,symbol,now)
                result=evaluate_symbol(session,row,now)
            store(session,'mtf:'+symbol,result)
        except Exception as exc:
            store(session,'mtf:'+symbol,dict(symbol=symbol,checked_ts=now,stage='error',scores=[],
                reason=str(exc)[:250] if isinstance(exc,RuntimeError) else type(exc).__name__))
        done+=1
        if time.monotonic()-started>=budget_seconds: break
    state.update(ts=now,cursor=(cursor+done)%max(1,len(contracts)),total=len(contracts),
                 batches=state.get('batches',0)+1,visited=state.get('visited',0)+done,
                 batch_size=done,methods=len(METHODS),timeframes=list(TIMEFRAMES),model=MODEL)
    store(session,'mtf_scan',state)
    return state


def pending_signals(session,now):
    found=[]
    for row in session.scalars(select(AgentState).where(AgentState.key.like('mtf:%'))):
        item=json.loads(row.value)
        if item.get('rules_revision')!=RULES_REVISION: continue
        if not 0<=now-item.get('checked_ts',0)<=120_000: continue
        for score in item.get('scores',[]):
            if score['status']=='paper_trial' and score.get('signal') and 0<=now-score['signal_ts']<=120_000:
                found.append(dict(symbol=item['symbol'],dataset_id=item['dataset_id'],**score))
    return sorted(found,key=lambda x:(-x['test']['expectancy_r'],x['symbol'],x['key']))[:3]


def refresh_priority(session,exchange,now,limit=4):
    """Frequent small live queue, independent of the slow all-contract history sweep."""
    catalog=read_state(session,'market_catalog').get('contracts',[])
    by_symbol={x['symbol']:x for x in catalog}
    eligible=[x for x in catalog if x.get('quote_volume',0)>=50_000_000 and now-x.get('listed_ts',now)>=38*DAY]
    eligible_symbols={x['symbol'] for x in eligible}
    complete=[]
    for row in session.scalars(select(AgentState).where(AgentState.key.like('mtf:%'))):
        item=json.loads(row.value)
        if item.get('stage')=='evaluated' and item.get('symbol') in eligible_symbols: complete.append(by_symbol[item['symbol']])
    selected=list({x['symbol']:x for x in sorted(complete,key=lambda x:-x.get('quote_volume',0))+eligible}.values())[:limit]
    done=[];errors=[];observations=[]
    for item in selected:
        try:
            refresh_history(session,exchange,item['symbol'],now)
            record=evaluate_symbol(session,item,now)
            store(session,'mtf:'+item['symbol'],record)
            if not 0<=now-record.get('latest_5m',0)<=2*STEP: raise ValueError('stale_candles')
            done.append(item['symbol']);observations.append(dict(symbol=item['symbol'],latest_closed_ts=record['latest_5m']))
        except Exception as exc: errors.append(dict(symbol=item['symbol'],error=type(exc).__name__))
    previous=read_state(session,'priority_research')
    result=dict(ts=now,symbols=done,errors=errors,successful_observations=observations,interval_seconds=60,
                last_success_ts=now if done else previous.get('last_success_ts',0))
    store(session,'priority_research',result)
    return result
