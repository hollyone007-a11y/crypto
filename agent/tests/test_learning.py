import pandas as pd
import pytest
from sqlalchemy import select

from analyst.llm import LLM
from analyst.schema import CodeProposal, ParamChange, ParamProposal
from db.models import JournalEntry, Lesson, Ohlcv, PromotionEvent, StrategyVersion
from db.session import init_db, insert_ignore, make_engine, make_session_factory
from learning import pipeline
from learning.codecheck import CodeRejected, check_source
from learning.evaluate import evaluate_in_process, evaluate_isolated, evaluate_version
from learning.reviewer import review_pending
from learning.walkforward import beats, has_lookahead, walk_forward
from learning.weekly import weekly_review
from strategies.baseline import TrendFollowing
from strategies.registry import build, production, seed
from tests.fakes import H, FakeClient, synthetic_market

DAY = 86_400_000
NOW = 1_792_454_400_000

GOOD_CODE = '''
from dataclasses import dataclass, field
from research.features import ema, atr
from strategies.base import Strategy, empty_signals


@dataclass
class EmaCross(Strategy):
    name: str = "ema_cross"
    timeframe: str = "1h"
    max_hold_bars: int = 48
    params: dict = field(default_factory=lambda: {"fast": 20, "slow": 100})

    def signals(self, df, funding=None):
        out = empty_signals(df)
        f, s, a = ema(df["close"], self.params["fast"]), ema(df["close"], self.params["slow"]), atr(df)
        up = (f > s) & (f.shift(1) <= s.shift(1)) & (a > 0)
        out.loc[up, "side"] = 1
        out.loc[up, "stop"] = df["close"] - 2 * a
        out.loc[up, "take"] = df["close"] + 4 * a
        out.loc[up, "confidence"] = 0.5
        out.loc[up, "rationale"] = "ema cross up"
        return out
'''

CHEAT_CODE = GOOD_CODE.replace("class EmaCross", "class Cheat").replace(
    'up = (f > s) & (f.shift(1) <= s.shift(1)) & (a > 0)',
    'up = (df["close"].shift(-3) > df["close"] * 1.01) & (a > 0)')


# ---------------- code check ----------------

def test_good_code_passes():
    check_source(GOOD_CODE, "EmaCross")


@pytest.mark.parametrize("mutation", [
    "import os\n", "import subprocess\n", "from config.settings import get_settings\n",
    "x = open('C:/x')\n", "y = eval('1')\n", "z = __import__('os')\n",
    "def f(df):\n    return df.__class__\n", "def f():\n    import os\n",
    "def f(pd):\n    return pd.read_csv('a')\n", "def f(df):\n    df.to_csv('a')\n",
    "def f(df):\n    return df.query('a>1')\n", "def f():\n    while True:\n        pass\n",
    "def f(o):\n    return getattr(o, 'x')\n", "s = '__globals__'\n", "print('side effect')\n",
    "from . import x\n",
])
def test_dangerous_code_rejected(mutation):
    with pytest.raises(CodeRejected):
        check_source(GOOD_CODE + "\n" + mutation, "EmaCross")


def test_structure_rules():
    with pytest.raises(CodeRejected, match="exactly one class"):
        check_source(GOOD_CODE, "Other")
    with pytest.raises(CodeRejected, match="subclass Strategy"):
        check_source(GOOD_CODE.replace("(Strategy)", "(dict)"), "EmaCross")
    with pytest.raises(CodeRejected, match="signals"):
        check_source(GOOD_CODE.replace("def signals", "def other"), "EmaCross")
    with pytest.raises(CodeRejected, match="syntax"):
        check_source("def (", "X")


# ---------------- walk-forward ----------------

def test_lookahead_detection():
    bars, fund = synthetic_market(1500)
    assert not has_lookahead(TrendFollowing(timeframe="1h"), bars, fund)
    ns = {}
    exec(compile(CHEAT_CODE, "cheat", "exec"), ns)  # trusted test code
    assert has_lookahead(ns["Cheat"](), bars, fund)


def test_walk_forward_and_beats():
    bars, fund = synthetic_market(3000)
    s = TrendFollowing(timeframe="1h")
    wf = walk_forward(s, {"X": (bars, fund)}, folds=4)
    assert len(wf["folds"]) == 4 and wf["trades"] == sum(f["trades"] for f in wf["folds"])
    better = {**wf, "sharpe": wf["sharpe"] + 1, "trades": 100,
              "folds": [{**f, "sharpe": f["sharpe"] + 1} for f in wf["folds"]]}
    assert beats(better, wf)[0]
    ok, why = beats(wf, better)
    assert not ok and any("Sharpe" in w for w in why)
    worse_dd = {**better, "max_dd_pct": wf["max_dd_pct"] - 5}
    assert not beats(worse_dd, wf)[0]
    few = {**better, "trades": 3}
    assert "too few" in beats(few, wf)[1][0]


# ---------------- proposals ----------------

def prop(**changes):
    return ParamProposal(family="trend_donchian", rationale="r",
                         changes=[ParamChange(name=k, value=v) for k, v in changes.items()])


def test_param_proposals_bounded(session):
    v = pipeline.propose_params(session, prop(channel=40.4, stop_atr=2.5))
    assert v.status == "candidate" and v.version == 2 and v.params["channel"] == 40
    assert v.params["stop_atr"] == 2.5 and v.parent_id is not None
    assert pipeline.propose_params(session, prop(channel=500)) is None          # > 2x
    assert pipeline.propose_params(session, prop(leverage=10)) is None          # unknown param
    assert pipeline.propose_params(session, prop(channel=55)) is None           # no change
    rej = session.scalars(select(StrategyVersion).where(StrategyVersion.status == "rejected")).all()
    assert len(rej) == 3
    assert session.scalars(select(PromotionEvent).where(PromotionEvent.step == "proposed")).all()


def test_code_proposals(session, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "CANDIDATE_DIR", tmp_path)
    ok = pipeline.propose_code(session, CodeProposal(class_name="EmaCross", family="ema_cross",
                                                     timeframe="1h", code=GOOD_CODE, rationale="r"))
    assert ok.status == "candidate" and (tmp_path / "ema_cross_v1.py").exists()
    bad = pipeline.propose_code(session, CodeProposal(class_name="EmaCross", family="ema_evil",
                                                      timeframe="1h", code="import os\n" + GOOD_CODE,
                                                      rationale="r"))
    assert bad is None and not (tmp_path / "ema_evil_v1.py").exists()
    clash = pipeline.propose_code(session, CodeProposal(class_name="EmaCross", family="meanrev_z",
                                                        timeframe="1h", code=GOOD_CODE, rationale="r"))
    assert clash is None
    strat = build(ok)
    assert strat.name == "ema_cross@v1" and strat.params["fast"] == 20


# ---------------- lifecycle ----------------

def stats(sharpe, trades=50, dd=-5.0, sum_r=5.0, worst=-1.0):
    return {"trades": trades, "sharpe": sharpe, "max_dd_pct": dd, "expectancy_r": 0.1,
            "sum_r": sum_r, "worst_day_pct": worst,
            "folds": [{"sharpe": sharpe, "trades": trades // 4, "sum_r": 1, "max_dd_pct": dd}] * 4}


def evaluator(table):
    """table: version label -> (wf_stats, paper_stats)"""
    def ev(v, start):
        wf, paper = table[f"{v.family}@v{v.version}"]
        return wf if start is None else paper
    return ev


def test_full_promotion_path(session):
    cand = pipeline.propose_params(session, prop(channel=40))
    ev = evaluator({"trend_donchian@v1": (stats(0.5), stats(0.3, sum_r=1.0)),
                    "trend_donchian@v2": (stats(1.0), stats(0.6, sum_r=2.0))})
    out = pipeline.advance(session, ev, NOW)
    assert cand.status == "paper_trial" and "passed" in out[0]
    assert pipeline.advance(session, ev, NOW + 13 * DAY) == []               # trial still running
    out = pipeline.advance(session, ev, NOW + 14 * DAY)
    assert cand.status == "production" and "PRODUCTION" in out[0]
    old = session.scalar(select(StrategyVersion).where(StrategyVersion.version == 1,
                                                       StrategyVersion.family == "trend_donchian"))
    assert old.status == "retired"
    steps = [e.step for e in session.scalars(select(PromotionEvent).where(
        PromotionEvent.version_id == cand.id).order_by(PromotionEvent.id))]
    assert steps == ["proposed", "walk_forward", "paper", "promote"]
    labels = [s.name for _, s in production(session)]
    assert "trend_donchian@v2" in labels and "trend_donchian@v1" not in labels


@pytest.mark.parametrize("wf,paper,status", [
    (stats(0.1), None, "rejected"),                                   # worse OOS Sharpe
    ({"error": "look-ahead detected"}, None, "rejected"),
    (stats(1.0), stats(1.0, dd=-12), "rejected"),                     # paper DD breach
    (stats(1.0), stats(1.0, worst=-2.5), "rejected"),                 # paper daily-loss breach
    (stats(1.0), stats(1.0, sum_r=0.5), "rejected"),                  # worse than prod in paper
])
def test_gates_reject(session, wf, paper, status):
    cand = pipeline.propose_params(session, prop(channel=40))
    ev = evaluator({"trend_donchian@v1": (stats(0.5), stats(0.3, sum_r=1.0)),
                    "trend_donchian@v2": (wf, paper)})
    pipeline.advance(session, ev, NOW)
    pipeline.advance(session, ev, NOW + 15 * DAY)
    assert cand.status == status
    prod = pipeline.current_prod(session, "trend_donchian")
    assert prod.version == 1                                           # production untouched


_pid = iter(range(10_000, 10**9))


def _journal(session, strategy, rs):
    for i, r in enumerate(rs):
        session.add(JournalEntry(position_id=next(_pid), symbol="BTCUSDT",
                                 strategy=strategy, side="long", thesis="", signal={}, context={},
                                 entry_ts=NOW + i, entry=1, qty=1, exit_ts=NOW + i + 1, r=r))
    session.commit()


@pytest.mark.parametrize("new_r,rolled", [(-0.2, True), (0.5, False)])
def test_rollback_after_50_trades(session, new_r, rolled):
    seed(session)
    v1 = pipeline.current_prod(session, "meanrev_z")
    v1.metrics = {"wf": {"expectancy_r": 0.1}}
    v1.status = "retired"
    v2 = StrategyVersion(family="meanrev_z", version=2, kind="params", params=v1.params,
                         status="production", parent_id=v1.id, created_ts=NOW, metrics={})
    session.add(v2)
    session.commit()
    _journal(session, "meanrev_z@v2", [new_r] * 49)
    assert pipeline.advance(session, evaluator({}), NOW) == []          # only 49 trades
    _journal(session, "meanrev_z@v2", [new_r])
    out = pipeline.advance(session, evaluator({}), NOW)
    assert (v2.status == "rolled_back") == rolled and (v1.status == "production") == rolled
    if rolled:
        assert "rolled back" in out[0]


# ---------------- reviewer & weekly ----------------

LESSON = {"outcome": "loss", "what_worked": "entry timing", "mistake": "ignored CPI",
          "lesson": "Skip meanrev entries within 2h of high-impact US data.", "tags": ["macro"]}


def test_review_pending(session):
    _journal(session, "meanrev_z@v1", [-1.0, 0.5])
    client = FakeClient(LESSON, "bad json")
    assert review_pending(session, LLM(session, client)) == 1
    assert session.scalar(select(Lesson)).lesson.startswith("Skip meanrev")
    assert client.calls[0]["model"] == "claude-sonnet-5-5"
    client2 = FakeClient(LESSON)
    assert review_pending(session, LLM(session, client2)) == 1          # retries the failed one
    assert review_pending(session, LLM(session, FakeClient())) == 0     # nothing pending


def test_weekly_review_creates_candidates_and_advances(session, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "CANDIDATE_DIR", tmp_path)
    resp = {"summary": "meanrev bleeding in trends",
            "param_proposals": [{"family": "meanrev_z", "changes": [{"name": "z", "value": 2.5}],
                                 "rationale": "fewer, stronger stretches"},
                                {"family": "trend_donchian", "changes": [{"name": "channel", "value": 999}],
                                 "rationale": "bad"}],
            "code_proposals": [{"class_name": "EmaCross", "family": "ema_cross", "timeframe": "1h",
                                "code": GOOD_CODE, "rationale": "new idea"}]}
    client = FakeClient(resp)
    seen = []

    def ev(v, start):
        seen.append(f"{v.family}@v{v.version}")
        return stats(0.2)
    res = weekly_review(session, LLM(session, client), ev, NOW)
    assert client.calls[0]["model"] == "claude-opus-5-5"
    assert res["created"] == ["meanrev_z@v2", "ema_cross@v1"]
    assert len(res["decisions"]) == 2 and "meanrev_z@v2" in seen


def test_weekly_review_invalid_output_creates_nothing(session):
    res = weekly_review(session, LLM(session, FakeClient("nope")), lambda v, s: stats(0), NOW)
    assert res["summary"] is None and res["created"] == []


# ---------------- evaluation (in-process and isolated subprocess) ----------------

@pytest.fixture
def file_db(tmp_path):
    url = f"sqlite:///{(tmp_path / 'eval.db').as_posix()}"
    engine = make_engine(url)
    init_db(engine)
    bars, _ = synthetic_market(1500, start_ts=NOW - 1500 * H)
    with make_session_factory(engine)() as s:
        insert_ignore(s, Ohlcv, [dict(exchange="bybit", symbol="BTCUSDT", timeframe="1h", **r)
                                 for r in bars.to_dict("records")])
        seed(s)
    return url, engine


def _code_version(s, tmp_path, code, cls, family):
    path = tmp_path / f"{family}.py"
    path.write_text(code, encoding="utf-8")
    v = StrategyVersion(family=family, version=1, kind="code", params={"class_name": cls},
                        code_path=str(path), status="candidate", created_ts=NOW, metrics={})
    s.add(v)
    s.commit()
    return v


def test_evaluate_in_process_params(file_db):
    url, engine = file_db
    with make_session_factory(engine)() as s:
        v = pipeline.current_prod(s, "meanrev_z")
        wf = evaluate_version(s, url, v, ["BTCUSDT"])
        assert "folds" in wf
        fwd = evaluate_in_process(s, v, ["BTCUSDT"], start_ts=NOW - 300 * H)
        assert {"trades", "sum_r", "max_dd_pct", "worst_day_pct", "sharpe"} <= set(fwd)
        assert evaluate_in_process(s, v, ["NOPE"]) == {"error": "no data"}


def test_evaluate_isolated_runs_code_and_catches_lookahead(file_db, tmp_path, monkeypatch):
    url, engine = file_db
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    with make_session_factory(engine)() as s:
        good = _code_version(s, tmp_path, GOOD_CODE, "EmaCross", "ema_cross")
        res = evaluate_version(s, url, good, ["BTCUSDT"])
        assert "folds" in res, res
        cheat = _code_version(s, tmp_path, CHEAT_CODE, "Cheat", "cheat")
        assert evaluate_isolated(url, cheat, ["BTCUSDT"]) == {"error": "look-ahead detected"}
