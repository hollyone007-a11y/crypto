"""Hard risk limits from config/risk.yaml. Loaded once into a frozen object;
this module never writes the file. The file hash is remembered so a runtime
modification is detected and trading fails closed."""
import hashlib
import math
from dataclasses import dataclass
from pathlib import Path

import yaml

from config.settings import ROOT

RISK_FILE = ROOT / "config" / "risk.yaml"
FIELDS = ("max_risk_per_trade_pct", "max_leverage", "max_open_positions",
          "daily_loss_limit_pct", "max_drawdown_pct", "require_stop_loss")


class RiskConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RiskLimits:
    max_risk_per_trade_pct: float
    max_leverage: float
    max_open_positions: int
    daily_loss_limit_pct: float
    max_drawdown_pct: float
    require_stop_loss: bool
    source_path: str = ""
    source_sha256: str = ""


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_limits(path: Path = RISK_FILE) -> RiskLimits:
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    missing = [f for f in FIELDS if f not in raw]
    if missing:
        raise RiskConfigError(f"risk.yaml missing: {missing}")
    extra = set(raw) - set(FIELDS)
    if extra:
        raise RiskConfigError(f"risk.yaml unknown keys: {sorted(extra)}")
    if raw["require_stop_loss"] is not True:
        raise RiskConfigError("require_stop_loss must be true")
    vals = {}
    for f in FIELDS[:-1]:
        v = raw[f]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
            raise RiskConfigError(f"{f} must be a positive number, got {v!r}")
        vals[f] = v
    if vals["max_risk_per_trade_pct"] >= 100 or vals["daily_loss_limit_pct"] >= 100 \
            or vals["max_drawdown_pct"] >= 100:
        raise RiskConfigError("percent limits must be < 100")
    if int(vals["max_open_positions"]) != vals["max_open_positions"]:
        raise RiskConfigError("max_open_positions must be an integer")
    return RiskLimits(
        max_risk_per_trade_pct=float(vals["max_risk_per_trade_pct"]),
        max_leverage=float(vals["max_leverage"]),
        max_open_positions=int(vals["max_open_positions"]),
        daily_loss_limit_pct=float(vals["daily_loss_limit_pct"]),
        max_drawdown_pct=float(vals["max_drawdown_pct"]),
        require_stop_loss=True,
        source_path=str(path), source_sha256=file_sha256(path))
