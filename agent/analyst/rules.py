"""Deterministic paper-only baseline. No paid model or private exchange API."""
import json
import logging

from analyst.cycle import build_candidates
from db.models import AnalystRun, StrategyVersion
from sqlalchemy import select
from journal.journal import market_snapshot

log = logging.getLogger(__name__)


def run_rules(session, engine, symbols, now_ms):
    open_symbols = {p.symbol for p in engine.broker.open_positions()}
    candidates, data = build_candidates(session, symbols, open_symbols, now_ms)
    submitted, skipped = [], []
    opportunities=[]
    for candidate in candidates:
        cid, symbol = candidate["candidate_id"], candidate["symbol"]
        family, _, version = candidate['strategy'].rpartition('@v')
        tested = session.scalar(select(StrategyVersion).where(
            StrategyVersion.family==family, StrategyVersion.version==int(version))) if version.isdigit() else None
        evidence = (tested.metrics or {}).get('wf',{}) if tested else {}
        opportunity={'symbol':symbol,'strategy':candidate['strategy'],
                     **candidate['signal'].to_dict(),'status':'отклонён','reason':''}
        opportunities.append(opportunity)
        if tested is None or symbol not in (tested.metrics or {}).get('wf_symbols',[]):
            opportunity['reason']='Нет проверки стратегии для этой монеты.'
            skipped.append((cid,[opportunity['reason']]))
            continue
        evidence=evidence.get('per_symbol',{}).get(symbol,evidence)
        if evidence.get('trades',0) < 20 or evidence.get('expectancy_r',0) <= 0 or evidence.get('sharpe',0) <= 0:
            opportunity['reason']='История не прошла проверку доходности или размера выборки.'
            skipped.append((cid,['История не прошла проверку: нужны 20+ сделок, положительные результат и Sharpe.']))
            continue
        if symbol in open_symbols:
            opportunity['reason']='По монете уже открыта позиция.'
            skipped.append((cid, ["position already open for symbol"]))
            continue
        snapshot = market_snapshot(candidate["bars"])
        result = engine.submit(
            symbol, candidate["signal"], candidate["strategy"],
            thesis="Rules baseline (simulation only): " + candidate["signal"].rationale,
            regime=snapshot.get("regime", ""), context=snapshot, client_order_id=cid,
        )
        (submitted if result.ok else skipped).append((cid, result.reasons))
        opportunity['status']='демовход' if result.ok else 'отклонён'
        opportunity['reason']='Исполнен в симуляторе' if result.ok else '; '.join(result.reasons)
        if result.ok:
            open_symbols.add(symbol)
    observations = {
        symbol: market_snapshot(bars) for symbol, (bars, _) in data.items()
    }
    thesis = (f"Анализ по правилам: кандидатов {len(candidates)}, "
              f"демовходов {len(submitted)}. "
              "Неподтверждённые стратегии пропускаются. Прибыльность не доказана.")
    result = dict(status="ok", mode="rules", thesis=thesis, markets=observations,
                  candidates=len(candidates), submitted=submitted, skipped=skipped)
    result['opportunities']=opportunities
    session.add(AnalystRun(ts=now_ms, purpose="decide", model="rules-baseline-v1",
                          ok=True, request={"symbols": symbols},
                          response=json.dumps(result, default=str)))
    session.commit()
    log.info("rules cycle: %d candidates, %d entries", len(candidates), len(submitted))
    return result
