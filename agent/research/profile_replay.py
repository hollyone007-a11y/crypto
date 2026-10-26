"""5-minute conservative OHLC replay of the requested duration/size profile.
Not a tick execution or historical news simulation; never certifies baseline production.
"""
import math
import numpy as np
import pandas as pd
from research.backtest import BtConfig, BtResult, metrics

STEP=300_000
MODEL={'version':'profile-5m-v1','fee':.00055,'slippage_bps':2.,'spread_bps':1.,
       'margin_pct':10.,'leverage':10.,'risk_pct':.5,'max_hold_minutes':60,'stalled_exit_minutes':30,
       'execution_resolution':'5m conservative OHLC','news_replay':False,'portfolio_replay':False}


def align_signals(base, higher, generated, timeframe_ms):
    result=pd.DataFrame({'side':0,'stop':float('nan'),'take':float('nan')},index=base.index)
    indices={int(ts):i for i,ts in enumerate(base.ts)}
    for i in generated.index[generated.side!=0]:
        at=int(higher.loc[i,'ts'])+timeframe_ms-STEP
        j=indices.get(at)
        if j is not None and j+1<len(base) and int(base.ts.iloc[j+1])==at+STEP:
            result.loc[j]=generated.loc[i,['side','stop','take']]
    return result


def replay(base, generated, start, end, funding=None):
    fee=MODEL['fee']; slip=MODEL['slippage_bps']/10000; spread=MODEL['spread_bps']/20000
    equity=10_000.;peak=equity;day=None;day_start=equity;daily_lock=False;halted=False;pos=None
    curve=[];trades=[];rejected=0
    frows=[] if funding is None else list(funding.itertuples(index=False))
    f_by_bar={}
    base_times=base.ts.to_numpy()
    for f in frows:
        j=int(np.searchsorted(base_times,f.ts,side='left'))-1
        if 0<=j<len(base) and int(base_times[j])<f.ts<=int(base_times[j])+STEP:
            f_by_bar.setdefault(j,[]).append(f)
    def close(i,px,reason):
        nonlocal pos,equity
        s=pos['side'];px=px*(1-s*(slip+spread));exit_fee=px*pos['qty']*fee
        gross=s*(px-pos['entry'])*pos['qty'];net=gross-pos['fees']-exit_fee+pos['funding']
        equity+=gross-exit_fee
        trades.append(dict(entry_ts=pos['ts'],exit_ts=int(base.ts.iloc[i])+STEP,side='long' if s>0 else 'short',
            entry=pos['entry'],exit=px,qty=pos['qty'],stop=pos['stop'],take=pos['take'],gross=gross,
            fees=pos['fees']+exit_fee,funding=pos['funding'],pnl=net,r=net/pos['risk'],
            mae_r=0.,mfe_r=0.,bars=i-pos['index']+1,reason=reason))
        pos=None
    for i,row in enumerate(base.itertuples(index=False)):
        ts=int(row.ts)
        if ts<start or ts>=end: continue
        today=ts//86_400_000
        marked_open=equity+(pos['side']*(row.open-pos['entry'])*pos['qty'] if pos else 0)
        if day!=today: day=today;day_start=marked_open;daily_lock=False
        peak=max(peak,marked_open)
        if marked_open<=peak*.9: halted=True
        if marked_open<=day_start*.98: daily_lock=True
        if pos is None and i and generated.side.iloc[i-1] and not halted and not daily_lock:
            s=int(generated.side.iloc[i-1]);stop=float(generated['stop'].iloc[i-1]);take=float(generated['take'].iloc[i-1])
            ref=row.open*(1+s*spread);qty=equity/ref;dist=abs(ref-stop)
            valid=(int(base.ts.iloc[i-1])+STEP==ts and math.isfinite(stop) and math.isfinite(take)
                   and s*(ref-stop)>0 and s*(take-ref)>0
                   and qty*(dist+ref*(2*fee+2*slip))<=equity*.005)
            # Do not include truncated end-of-window positions in evidence.
            if valid and ts+MODEL['max_hold_minutes']*60_000<=end:
                entry=ref*(1+s*slip);cost=entry*qty*fee;equity-=cost
                pos=dict(side=s,stop=stop,take=take,qty=qty,entry=entry,fees=cost,funding=0.,
                         ts=ts,index=i,risk=abs(entry-stop)*qty)
            else: rejected+=1
        if pos:
            s=pos['side']
            for f in f_by_bar.get(i,[]):
                if max(ts,pos['ts'])<f.ts<=ts+STEP:
                    amount=-s*f.rate*pos['qty']*row.close;equity+=amount;pos['funding']+=amount
            stop_hit=row.low<=pos['stop'] if s>0 else row.high>=pos['stop']
            take_hit=row.high>=pos['take'] if s>0 else row.low<=pos['take']
            age=(ts+STEP-pos['ts'])/60_000
            executable=row.close*(1-s*spread)
            net=s*(executable-pos['entry'])*pos['qty']-pos['fees']-executable*pos['qty']*fee+pos['funding']
            if stop_hit: close(i,min(row.open,pos['stop']) if s>0 else max(row.open,pos['stop']),'stop')
            elif take_hit: close(i,pos['take'],'take')
            elif age>=60: close(i,row.close,'time_limit')
            elif age>=30 and net<pos['risk']*.25: close(i,row.close,'stalled')
        mtm=pos['side']*(row.close-pos['entry'])*pos['qty'] if pos else 0
        marked=equity+mtm;peak=max(peak,marked)
        if marked<=day_start*.98: daily_lock=True
        if marked<=peak*.9:
            halted=True
            if pos:
                close(i,row.close,'halt');mtm=0
        curve.append((ts,equity+mtm))
    cfg=BtConfig(max_leverage=10,bar_ms=STEP)
    columns=['entry_ts','exit_ts','side','entry','exit','qty','stop','take','gross','fees','funding','pnl','r','mae_r','mfe_r','bars','reason']
    result=BtResult(pd.DataFrame(trades,columns=columns),pd.Series([v for _,v in curve],index=[ts for ts,_ in curve],dtype=float),cfg)
    out=metrics(result)
    out['max_dd_pct']=round(float((result.equity/result.equity.cummax()-1).min())*100,3) if len(result.equity) else 0.
    out.update(net_usdt=round(sum(x['pnl'] for x in trades),4),rejected_by_profile=rejected,
               time_exits=sum(x['reason'] in ('stalled','time_limit') for x in trades),model=MODEL,
               avg_hold_minutes=round(sum(x['bars']*5 for x in trades)/len(trades),2) if trades else None,
               start_ts=start,end_ts=end)
    return out
