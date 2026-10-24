"""Candidate lifecycle, fully automatic, every step logged to promotion_events:

  candidate --walk-forward OOS beats prod--> paper_trial --14d forward, no breaches,
  >= prod--> production (old version retired) --50 trades worse than parent--> rolled_back
"""
import math
import time
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from analyst.schema import CodeProposal, ParamProposal
from db.models import JournalEntry, PromotionEvent, StrategyVersion
from learning.codecheck import CodeRejected, check_source
from learning.walkforward import beats
from strategies.registry import CANDIDATE_DIR, FAMILIES, label, next_version, seed

DAY_MS = 86_400_000
TRIAL_DAYS = 14
ROLLBACK_TRADES = 50
INT_PARAMS = {"channel", "ema", "atr", "window", "lookback", "rsi_lo", "rsi_hi"}

Evaluator = Callable[[StrategyVersion, int | None], dict]  # (version, start_ts|None) -> stats


def _now() -> int:
    return int(time.time() * 1000)


def log_event(s: Session, v: StrategyVersion, step: str, passed: bool, **detail) -> None:
    s.add(PromotionEvent(ts=_now(), version_id=v.id, step=step, passed=passed, detail=detail))
    s.commit()


def current_prod(s: Session, family: str) -> StrategyVersion | None:
    return s.scalar(select(StrategyVersion).where(StrategyVersion.family == family,
                                                  StrategyVersion.status == "production"))


# ---------------- proposals ----------------

def propose_params(s: Session, p: ParamProposal) -> StrategyVersion | None:
    seed(s)
    prod = current_prod(s, p.family)
    base = prod.params if prod else FAMILIES[p.family]().params
    params, problems = dict(base), []
    for c in p.changes:
        if c.name not in base:
            problems.append(f"unknown param {c.name}")
            continue
        cur = float(base[c.name])
        v = c.value
        if not math.isfinite(v) or v <= 0 or not (0.5 * cur <= v <= 2.0 * cur):
            problems.append(f"{c.name}={v} outside [0.5x, 2x] of {cur}")
            continue
        params[c.name] = int(round(v)) if c.name in INT_PARAMS else float(v)
    v = StrategyVersion(family=p.family, version=next_version(s, p.family), kind="params",
                        params=params, status="candidate" if not problems and params != base else "rejected",
                        parent_id=prod.id if prod else None, rationale=p.rationale,
                        created_ts=_now(), metrics={})
    s.add(v)
    s.commit()
    log_event(s, v, "proposed", v.status == "candidate",
              problems=problems or (["no effective change"] if params == base else []),
              changes=[c.model_dump() for c in p.changes])
    return v if v.status == "candidate" else None


def propose_code(s: Session, p: CodeProposal) -> StrategyVersion | None:
    seed(s)
    if p.family in FAMILIES:
        problem = "code proposals must use a new family name"
    else:
        try:
            check_source(p.code, p.class_name)
            problem = None
        except CodeRejected as e:
            problem = str(e)
    ver = next_version(s, p.family)
    path = CANDIDATE_DIR / f"{p.family}_v{ver}.py"
    if problem is None:
        path.write_text(p.code, encoding="utf-8")
    v = StrategyVersion(family=p.family, version=ver, kind="code",
                        params={"class_name": p.class_name, "timeframe": p.timeframe},
                        code_path=str(path) if problem is None else "",
                        status="candidate" if problem is None else "rejected",
                        rationale=p.rationale, created_ts=_now(), metrics={})
    s.add(v)
    s.commit()
    log_event(s, v, "proposed", problem is None, problem=problem)
    return v if problem is None else None


# ---------------- automatic advancement ----------------

def advance(s: Session, evaluate: Evaluator, now: int | None = None) -> list[str]:
    now = now or _now()
    out = []
    for v in s.scalars(select(StrategyVersion).where(StrategyVersion.status == "candidate")).all():
        out.append(_walk_forward_gate(s, v, evaluate, now))
    for v in s.scalars(select(StrategyVersion).where(StrategyVersion.status == "paper_trial")).all():
        if now - v.trial_start_ts >= TRIAL_DAYS * DAY_MS:
            out.append(_paper_gate(s, v, evaluate, now))
    for v in s.scalars(select(StrategyVersion).where(StrategyVersion.status == "production")).all():
        r = _rollback_check(s, v)
        if r:
            out.append(r)
    return out


def _benchmark(s: Session, v: StrategyVersion) -> StrategyVersion | None:
    """Same family's production version, else (new family) the best production one."""
    if v.family in FAMILIES or current_prod(s, v.family):
        return current_prod(s, v.family)
    prods = s.scalars(select(StrategyVersion).where(StrategyVersion.status == "production")).all()
    scored = [p for p in prods if p.metrics.get("wf")]
    return max(scored, key=lambda p: p.metrics["wf"]["sharpe"]) if scored else (prods[0] if prods else None)


def _walk_forward_gate(s, v, evaluate, now) -> str:
    cand = evaluate(v, None)
    bench = _benchmark(s, v)
    prod = evaluate(bench, None) if bench else {"error": "no benchmark"}
    if "error" in cand or "error" in prod:
        v.status = "rejected"
        log_event(s, v, "walk_forward", False, candidate=cand, production=prod)
        return f"{label(v)} rejected: {cand.get('error') or prod.get('error')}"
    ok, why = beats(cand, prod)
    v.metrics = {**v.metrics, "wf": cand, "wf_benchmark": {"version": label(bench), **prod}}
    if bench:
        bench.metrics = {**bench.metrics, "wf": prod}
    if ok:
        v.status, v.trial_start_ts = "paper_trial", now
    else:
        v.status = "rejected"
    log_event(s, v, "walk_forward", ok, candidate=cand, production=prod, reasons=why)
    return f"{label(v)} walk-forward {'passed -> paper_trial' if ok else 'failed: ' + '; '.join(why)}"


def _paper_gate(s, v, evaluate, now) -> str:
    cand = evaluate(v, v.trial_start_ts)
    bench = _benchmark(s, v)
    prod = evaluate(bench, v.trial_start_ts) if bench else {"sum_r": 0.0}
    if 'error' not in cand and cand.get('trades', 0) < 20:
        v.metrics = {**v.metrics, 'paper':cand, 'paper_benchmark':prod}
        log_event(s, v, 'paper_wait', False, trades=cand.get('trades',0), required=20)
        return f'{label(v)} waiting: fewer than 20 forward trades'
    why = []
    if "error" in cand:
        why.append(cand["error"])
    else:
        if any(not math.isfinite(prod.get(key,float('nan'))) for key in ('sum_r','sharpe','max_dd_pct','worst_day_pct')):
            why.append('non-finite forward benchmark statistics')
        if cand.get('dataset_id') or prod.get('dataset_id'):
            if any(cand.get(key)!=prod.get(key) for key in ('dataset_id','cutoff_ts','symbols','cost_model')):
                why.append('forward candidate and benchmark provenance mismatch')
        if any(not math.isfinite(cand.get(key,float('nan'))) for key in ('sum_r','sharpe','max_dd_pct','worst_day_pct')):
            why.append('non-finite forward statistics')
        if cand.get('sum_r', 0) <= 0 or cand.get('sharpe', 0) <= 0:
            why.append('forward net result and Sharpe must be positive')
        if 'error' in prod:
            why.append('forward benchmark unavailable')
        if cand["max_dd_pct"] <= -10:
            why.append(f"paper maxDD {cand['max_dd_pct']}% breached -10%")
        if cand["worst_day_pct"] <= -2:
            why.append(f"paper worst day {cand['worst_day_pct']}% breached -2%")
        if cand["sum_r"] < prod.get("sum_r", 0.0):
            why.append(f"paper sum R {cand['sum_r']} < production {prod.get('sum_r')}")
    ok = not why
    v.metrics = {**v.metrics, "paper": cand, "paper_benchmark": prod}
    if ok:
        old = current_prod(s, v.family)
        if old:
            old.status = "retired"
        v.status, v.promoted_ts = "production", now
    else:
        v.status = "rejected"
    log_event(s, v, "paper", ok, candidate=cand, production=prod, reasons=why)
    if ok:
        log_event(s, v, "promote", True, replaced=label(old) if old else None)
    return f"{label(v)} paper trial {'passed -> PRODUCTION' if ok else 'failed: ' + '; '.join(why)}"


def _closed_r(s: Session, strategy_label: str, limit: int) -> list[float]:
    q = select(JournalEntry.r).where(JournalEntry.strategy == strategy_label,
                                     JournalEntry.exit_ts.is_not(None), JournalEntry.r.is_not(None)) \
        .order_by(JournalEntry.exit_ts.desc()).limit(limit)
    return [float(r) for r in s.scalars(q)]


def _rollback_check(s, v) -> str | None:
    rs = _closed_r(s, label(v), ROLLBACK_TRADES)
    if len(rs) < ROLLBACK_TRADES:
        return None
    new_exp = sum(rs) / len(rs)
    parent = s.get(StrategyVersion, v.parent_id) if v.parent_id else None
    if parent is None:
        ref, ref_src = 0.0, "zero (new family)"
    else:
        prs = _closed_r(s, label(parent), ROLLBACK_TRADES)
        if len(prs) >= ROLLBACK_TRADES:
            ref, ref_src = sum(prs) / len(prs), f"{label(parent)} last {len(prs)} trades"
        else:
            ref = float(parent.metrics.get("wf", {}).get("expectancy_r", 0.0))
            ref_src = f"{label(parent)} walk-forward expectancy"
    if new_exp >= ref:
        return None
    v.status = "rolled_back"
    if parent is not None:
        parent.status = "production"
    log_event(s, v, "rollback", True, new_expectancy_r=round(new_exp, 3), reference=round(ref, 3),
              reference_source=ref_src, restored=label(parent) if parent else None)
    return f"{label(v)} rolled back ({new_exp:.3f}R < {ref:.3f}R vs {ref_src})"
