"""Evaluate one strategy version. LLM-written code runs ONLY via the subprocess
path below: separate interpreter, environment without API keys, no .env read,
hard timeout.

CLI (used by the subprocess):  python -m learning.evaluate <db_url> <version_id> <start_ts|wf> <symbols>
"""
import json
import os
import subprocess
import sys

from sqlalchemy.orm import Session

from config.settings import ROOT
from db.models import StrategyVersion
from db.session import make_engine, make_session_factory
from learning.walkforward import evaluate_window, has_lookahead, walk_forward
from research.data_loader import load_funding, load_ohlcv
from strategies.registry import build

TIMEOUT_S = 600


def _load(session: Session, symbols: list[str], timeframe: str) -> dict:
    return {s: (load_ohlcv(session, s, timeframe), load_funding(session, s)) for s in symbols}


def evaluate_in_process(session: Session, v: StrategyVersion, symbols: list[str],
                        start_ts: int | None = None) -> dict:
    strat = build(v)
    data = {k: d for k, d in _load(session, symbols, strat.timeframe).items() if not d[0].empty}
    if not data:
        return {"error": "no data"}
    if any(has_lookahead(strat, bars, fund) for bars, fund in data.values()):
        return {"error": "look-ahead detected"}
    if start_ts is None:
        result=walk_forward(strat,data)
        result['symbols']=sorted(data)
        return result
    out = {"trades": 0, "sum_r": 0.0, "max_dd_pct": 0.0, "worst_day_pct": 0.0, "sharpe": []}
    for bars, fund in data.values():
        s = evaluate_window(strat, bars, fund, start_ts)
        out["trades"] += s["trades"]
        out["sum_r"] = round(out["sum_r"] + s["sum_r"], 3)
        out["max_dd_pct"] = min(out["max_dd_pct"], s["max_dd_pct"])
        out["worst_day_pct"] = min(out["worst_day_pct"], s["worst_day_pct"])
        out["sharpe"].append(s["sharpe"])
    out["sharpe"] = round(sum(out["sharpe"]) / len(out["sharpe"]), 3)
    return out


def evaluate_isolated(db_url: str, v: StrategyVersion, symbols: list[str],
                      start_ts: int | None = None) -> dict:
    env = {k: os.environ[k] for k in ("SYSTEMROOT", "PATH", "TEMP", "TMP") if k in os.environ}
    env["PYTHONPATH"] = str(ROOT)
    cmd = [sys.executable, "-m", "learning.evaluate", db_url, str(v.id),
           "wf" if start_ts is None else str(start_ts), ",".join(symbols)]
    try:
        p = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True,
                           timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"error": "evaluation timed out"}
    if p.returncode != 0:
        return {"error": f"evaluation failed: {p.stderr[-500:]}"}
    return json.loads(p.stdout.strip().splitlines()[-1])


def evaluate_version(session: Session, db_url: str, v: StrategyVersion, symbols: list[str],
                     start_ts: int | None = None) -> dict:
    if v.kind == "code":
        return evaluate_isolated(db_url, v, symbols, start_ts)
    return evaluate_in_process(session, v, symbols, start_ts)


def main(argv: list[str]) -> int:
    db_url, vid, mode, symbols = argv
    with make_session_factory(make_engine(db_url))() as s:
        v = s.get(StrategyVersion, int(vid))
        try:
            res = evaluate_in_process(s, v, symbols.split(","), None if mode == "wf" else int(mode))
        except Exception as e:  # candidate code may raise anything; report, don't crash caller
            res = {"error": f"{type(e).__name__}: {e}"[:500]}
    print(json.dumps(res))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
