"""Durable signal inbox and prospective research. Never submits an account order."""
import hashlib
import math
from collections import defaultdict,Counter
from sqlalchemy import select,func,case
from analyst.copy_trading import store
from analyst.news_risk import read_state
from config.settings import ROOT
from db.models import SignalObservation as Observation,Ohlcv,FundingRate
from db.session import insert_ignore
from strategies.public_methods import METHODS,TIMEFRAMES,signals
from research.features import resample
from research.profile_replay import STEP,POLICIES

DAY=86_400_000
FEE=.00055
SLIP=.0002
SPREAD=.00005
REVISION=hashlib.sha256((ROOT/'learning'/'observatory.py').read_bytes()+
    (ROOT/'strategies'/'public_methods.py').read_bytes()+
    (ROOT/'research'/'supertrend.py').read_bytes()).hexdigest()[:24]


def capture(session,symbol,base,now):
    if base.empty:return 0
    learned=read_state(session,'signal_learning')
    priors={(x['method'],x['timeframe'],x['policy']):x for x in learned.get('cohorts',[])
        if x['mode']=='forward' and learned.get('revision')==REVISION}
    frames={'5m':base,'15m':resample(base,'15min',STEP),
        '1h':resample(base,'1h',STEP),'4h':resample(base,'4h',STEP)}
    created=0
    for tf,bars in frames.items():
        if len(bars)<60:continue
        step=TIMEFRAMES[tf];latest=int(bars.ts.iloc[-1])+step
        state=read_state(session,'signal_cursor:'+symbol+':'+tf)
        cursor=state.get('ts',now-DAY) if state.get('revision')==REVISION else now-DAY
        if latest<=cursor and state.get('bars')==len(bars):continue
        # Re-scan a bounded window so a repaired interior gap cannot silently
        # hide signals behind the cursor. Unique IDs preserve first forecasts.
        cursor=now-DAY
        rows=[]
        contiguous=bars.ts.diff().eq(step).rolling(59,min_periods=59).sum().eq(59)
        for method in METHODS:
            generated=signals(bars,method)
            indices=generated.index[(generated.side!=0)&contiguous&(bars.ts+step>cursor)&(bars.ts+step<=now)]
            for i in indices:
                signal=generated.loc[i];at=int(bars.loc[i,'ts'])+step
                side=int(signal.side);reference=float(bars.loc[i,'close']);stop=float(signal['stop'])
                for policy in ('balanced','active') if tf in ('5m','15m') else ('balanced',):
                    take=reference+(float(signal['take'])-reference)*POLICIES[policy]['target_multiplier']
                    valid=all(math.isfinite(v) and v>0 for v in (reference,stop,take)) and side*(reference-stop)>0 and side*(take-reference)>0
                    mode='forward' if 0<=now-at<=120_000 else 'replay'
                    # A forward decision is persisted BEFORE its planned entry bar.
                    due=(now//STEP+1)*STEP if mode=='forward' else at
                    uid=hashlib.sha256(f'{REVISION}:{symbol}:{tf}:{method}:{policy}:{at}'.encode()).hexdigest()
                    prior=priors.get((method,tf,policy),{})
                    rows.append(dict(id=uid,revision=REVISION,symbol=symbol,method=method,timeframe=tf,policy=policy,
                        mode=mode,status='pending' if valid else 'invalid',created_ts=now,signal_ts=at,
                        entry_due_ts=due,cursor_ts=0,side=side,reference=reference,stop=stop,take=take,
                        funding_pct=0.,prediction_r=prior.get('mean_r') if mode=='forward' else None,
                        prediction_n=prior.get('count',0) if mode=='forward' else 0,
                        reason='' if valid else 'Invalid signal geometry'))
        if rows:created+=insert_ignore(session,Observation,rows)
        store(session,'signal_cursor:'+symbol+':'+tf,dict(revision=REVISION,ts=latest,bars=len(bars)))
    return created


def advance(session,item,bars,funding,coverage,now):
    """Conservative closed 5m bars, stop first; no generated/missing candles."""
    for row in bars:
        ts=int(row.ts)
        if ts<item.entry_due_ts or ts<=item.cursor_ts or ts+STEP>now:continue
        expected=item.cursor_ts+STEP if item.cursor_ts else item.entry_due_ts
        if ts!=expected:
            item.status='gap';item.reason='Missing 5m bar; outcome excluded';return
        if coverage.get('start_ts',now)>ts or coverage.get('complete_to',0)<ts+STEP:
            item.reason='Waiting for funding coverage';return
        side=item.side
        if item.entry is None:
            price=row.open*(1+side*(SLIP+SPREAD))
            if side*(price-item.stop)<=0 or side*(item.take-price)<=0:
                item.status='invalid';item.reason='Entry crossed stop or target';return
            item.entry=price;item.entry_ts=ts;item.status='open'
        for f in funding:
            if ts<f.ts<=ts+STEP:
                item.funding_pct+=-side*f.rate*row.close/item.entry*100
        age=(ts+STEP-item.entry_ts)/60_000
        stop=row.low<=item.stop if side>0 else row.high>=item.stop
        take=row.high>=item.take if side>0 else row.low<=item.take
        executable=row.close*(1-side*(SLIP+SPREAD))
        net=side*(executable/item.entry-1)-FEE*(1+executable/item.entry)+item.funding_pct/100
        risk=abs(item.entry-item.stop)/item.entry
        controls=POLICIES[item.policy];price=None;reason=''
        if stop:
            price=min(row.open,item.stop) if side>0 else max(row.open,item.stop);reason='stop'
        elif take:price=item.take;reason='take'
        elif age>=controls['max_hold_minutes']:price=row.close;reason='time_limit'
        elif age>=controls['stalled_exit_minutes'] and net<risk*.25:price=row.close;reason='stalled'
        item.cursor_ts=ts
        if price is not None:
            exit_price=price*(1-side*(SLIP+SPREAD))
            fees=FEE*(1+exit_price/item.entry)
            net=side*(exit_price/item.entry-1)-fees+item.funding_pct/100
            item.net_pct=net*100;item.net_r=net/risk if risk else None
            item.fees_pct=fees*100;item.exit_ts=ts+STEP;item.status='closed';item.reason=reason
            return
        item.reason='Waiting for next closed 5m bar'


def resolve(session,now,limit=250):
    query=select(Observation).where(Observation.revision==REVISION,Observation.status.in_(['pending','open']),Observation.entry_due_ts+STEP<=now)
    pending=list(session.scalars(query.order_by(case((Observation.mode=='forward',0),else_=1),Observation.signal_ts).limit(limit)))
    cache={};changed=0
    for item in pending:
        if item.symbol not in cache:
            start=min(x.entry_due_ts for x in pending if x.symbol==item.symbol)
            bars=list(session.execute(select(Ohlcv.ts,Ohlcv.open,Ohlcv.high,Ohlcv.low,Ohlcv.close).where(
                Ohlcv.exchange=='binanceusdm',Ohlcv.symbol==item.symbol,Ohlcv.timeframe=='5m',Ohlcv.ts>=start,
                Ohlcv.ts+STEP<=now).order_by(Ohlcv.ts)))
            funding=list(session.execute(select(FundingRate.ts,FundingRate.rate).where(FundingRate.exchange=='binanceusdm',
                FundingRate.symbol==item.symbol,FundingRate.ts>=start,FundingRate.ts<=now)))
            cache[item.symbol]=(bars,funding,read_state(session,'funding_coverage:'+item.symbol))
        advance(session,item,*cache[item.symbol],now)
        changed+=1
        if changed%20==0:session.commit()
    session.commit()
    return changed


def learn(session,now):
    counts=dict(session.execute(select(Observation.status,func.count()).where(Observation.revision==REVISION).group_by(Observation.status)).all())
    modes=dict(session.execute(select(Observation.mode,func.count()).where(Observation.revision==REVISION).group_by(Observation.mode)).all())
    rows=list(session.scalars(select(Observation).where(Observation.revision==REVISION,Observation.status=='closed')
        .order_by(Observation.exit_ts.desc()).limit(10000)))
    groups=defaultdict(list)
    for row in rows:groups[(row.mode,row.method,row.timeframe,row.policy)].append(row)
    cohorts=[]
    for (mode,method,tf,policy),samples in groups.items():
        ordered=sorted(samples,key=lambda x:(x.signal_ts,x.id));split=len(ordered)//2
        first=ordered[:split];last=ordered[split:]
        mean=sum(x.net_r or 0 for x in samples)/len(samples)
        heldout=sum(x.net_pct or 0 for x in last)
        reason=('Нужно 20 будущих наблюдений; исторический расчёт не подтверждает навык.' if mode!='forward' or len(samples)<20 else
            'Последняя половина наблюдений убыточна: проверять издержки и выходы.' if heldout<=0 else
            'Положительный результат на отложенной половине; нужен отдельный тест профиля исполнения.')
        cohorts.append(dict(mode=mode,method=method,timeframe=tf,policy=policy,count=len(samples),mean_r=round(mean,4),
            first_mean_pct=round(sum(x.net_pct or 0 for x in first)/len(first),4) if first else None,
            heldout_mean_pct=round(heldout/len(last),4) if last else None,
            stop_count=sum(x.reason=='stop' for x in samples),reason=reason))
    predictions=[x for x in rows if x.mode=='forward' and x.prediction_r is not None and x.net_r is not None]
    previous=read_state(session,'signal_learning')
    report=dict(ts=now,revision=REVISION,counts=counts,modes=modes,cohorts=cohorts,
        closed_modes=dict(Counter(x.mode for x in rows)),
        passes=previous.get('passes',0)+1,actual_rule_changes=0,
        predictions_scored=len(predictions),prediction_mae_r=round(sum(abs(x.prediction_r-x.net_r) for x in predictions)/len(predictions),4) if predictions else None,
        scope='Контрольные наблюдения единичного номинала, не сделки и не прибыль счёта. Комиссии, спред, проскальзывание и фандинг включены; 5м OHLC — приближение, нет общего портфеля и новостей. Сигналы могут быть коррелированы.',
        next_step='Накопить будущие наблюдения и сравнить методы/ТФ/выходы на отложенной половине. Риск и рабочие правила автоматически не меняются.')
    store(session,'signal_learning',report)
    return report


def cycle(session,now):
    resolved=resolve(session,now)
    report=learn(session,now);report['processed_this_cycle']=resolved
    store(session,'signal_learning',report)
    return report
