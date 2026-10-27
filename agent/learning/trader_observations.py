"""First-seen public executions; no inference of a trader's private algorithm."""
import hashlib
from collections import Counter
from analyst.copy_trading import store
from analyst.news_risk import read_state
from data.copy_sources import number


def observe(session,snapshot,now):
    result=[]
    for address,wallet in snapshot.get('wallets',{}).items():
        key='trader_observations:'+address;state=read_state(session,key)
        last=state.get('last_poll_ts',0);fills=wallet.get('fills',[])
        gaps=state.get('coverage_gaps',0)+int(bool(last and now-last>120_000))+int(len(fills)>=2000)
        seen=state.get('seen',{});records=state.get('records',[])
        directions=Counter(state.get('directions',{}));pnl_total=state.get('reported_closed_pnl',0.)
        count=state.get('fill_count',0);closed=state.get('closing_fill_count',0)
        valid=[x for x in fills if isinstance(x,dict) and isinstance(x.get('time'),int)]
        gaps+=len(fills)-len(valid)
        for fill in sorted(valid,key=lambda x:x['time']):
            try:
                ts=int(fill['time'])
                if not 0<=now-ts<=120_000:continue
                uid=hashlib.sha256(f"{address}:{fill.get('tid')}:{ts}:{fill.get('coin')}:{fill.get('hash')}".encode()).hexdigest()[:32]
                if uid in seen:continue
                price=number(fill['px']);qty=number(fill['sz']);pnl=number(fill.get('closedPnl',0));fee=number(fill.get('fee',0))
                direction=str(fill.get('dir','unknown'))[:64]
                if price<=0 or qty<=0 or direction not in ('Open Long','Open Short','Close Long','Close Short','Long > Short','Short > Long'):continue
                seen[uid]=ts;count+=1;directions[direction]+=1
                closed+=int(direction.startswith('Close ') or ' > ' in direction);pnl_total+=pnl
                records.append(dict(id=uid,observed_ts=now,source_ts=ts,coin=str(fill.get('coin',''))[:32],
                    direction=direction,price=price,qty=qty,reported_closed_pnl=pnl,fee=fee,
                    fee_token=str(fill.get('feeToken','unknown'))[:16],strategy='Неизвестна: исполнение не раскрывает алгоритм.'))
            except (KeyError,ValueError,TypeError):gaps+=1
        state.update(address=address,first_seen_ts=state.get('first_seen_ts',now),last_poll_ts=now,
            coverage_gaps=gaps,fill_count=count,closing_fill_count=closed,reported_closed_pnl=round(pnl_total,6),
            directions=dict(directions),seen={key:ts for key,ts in seen.items() if ts>=now-600_000},records=records[-100:])
        store(session,key,state);result.append(dict(address=address,closing_fills=closed,coverage_gaps=gaps))
    return result
