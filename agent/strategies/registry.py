"""Versioned strategy registry backed by strategy_versions.
Baseline families are seeded as v1/production on first use."""
import importlib.util
import time
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from config.settings import ROOT
from db.models import StrategyVersion
from strategies.base import Strategy
from strategies.baseline import ALL

FAMILIES: dict[str, type[Strategy]] = {cls().name: cls for cls in ALL}
CANDIDATE_DIR = ROOT / "strategies" / "candidates"


def label(v: StrategyVersion) -> str:
    return f"{v.family}@v{v.version}"


def seed(session: Session) -> None:
    for fam, cls in FAMILIES.items():
        exists = session.scalar(select(StrategyVersion).where(StrategyVersion.family == fam))
        if exists is None:
            session.add(StrategyVersion(family=fam, version=1, kind="params", params=cls().params,
                                        status="production", created_ts=_now(),
                                        promoted_ts=_now(), rationale="baseline", metrics={}))
    session.commit()


def next_version(session: Session, family: str) -> int:
    return (session.scalar(select(func.max(StrategyVersion.version))
                           .where(StrategyVersion.family == family)) or 0) + 1


def production(session: Session) -> list[tuple[StrategyVersion, Strategy]]:
    seed(session)
    rows = session.scalars(select(StrategyVersion).where(StrategyVersion.status == "production")
                           .order_by(StrategyVersion.family)).all()
    return [(v, build(v)) for v in rows]


def build(v: StrategyVersion) -> Strategy:
    if v.kind == "params":
        cls = FAMILIES[v.family]
        strat = cls()
        strat.params = {**strat.params, **v.params}
    else:
        strat = load_code_strategy(Path(v.code_path), v.params["class_name"])()
    strat.name = label(v)
    return strat


def load_code_strategy(path: Path, class_name: str) -> type[Strategy]:
    """Only for files that passed learning.codecheck. Never call on unchecked code."""
    from learning.codecheck import check_source
    check_source(path.read_text(encoding="utf-8"), class_name)  # re-check on every load
    spec = importlib.util.spec_from_file_location(f"candidate_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cls = getattr(mod, class_name)
    if not (isinstance(cls, type) and issubclass(cls, Strategy)):
        raise TypeError(f"{class_name} is not a Strategy")
    return cls


def _now() -> int:
    return int(time.time() * 1000)
