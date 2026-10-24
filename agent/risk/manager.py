"""Deterministic risk gate. Every order must pass evaluate() and carry the
single-use Approval it returns; the broker refuses anything else.

Nothing here reads LLM output except as an untrusted OrderRequest: the size is
computed here, never taken from the caller unless it is smaller."""
import math
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from config.settings import ROOT
from db.models import RiskEvent, RiskState
from risk.limits import RiskLimits, file_sha256

KILL_SWITCH = ROOT / "STOP"
APPROVAL_TTL_MS = 60_000


class RiskViolation(Exception):
    pass


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: str                      # long / short
    entry: float                   # reference price (current mark)
    stop: float | None
    take: float | None = None
    strategy: str = ""
    requested_qty: float | None = None
    requested_leverage: float | None = None


@dataclass(frozen=True)
class OpenPos:
    symbol: str
    side: str
    qty: float
    mark: float


@dataclass(frozen=True)
class AccountSnapshot:
    equity: float
    positions: tuple[OpenPos, ...] = ()


@dataclass(frozen=True)
class Approval:
    id: str
    symbol: str
    side: str
    qty: float
    stop: float
    take: float | None
    expires_ts: int


@dataclass
class Decision:
    approved: bool
    reasons: list[str] = field(default_factory=list)
    approval: Approval | None = None
    qty: float = 0.0


def now_ms() -> int:
    return int(time.time() * 1000)


def _finite_pos(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


class RiskManager:
    def __init__(self, session: Session, limits: RiskLimits, kill_switch: Path = KILL_SWITCH,
                 on_alert: Callable[[str], None] | None = None, clock: Callable[[], int] = now_ms):
        self.s = session
        self.limits = limits
        self.kill_switch = Path(kill_switch)
        self.on_alert = on_alert or (lambda msg: None)
        self.clock = clock
        self._issued: dict[str, Approval] = {}

    # ---------- persistent state ----------
    def _get(self, key: str, default: str = "") -> str:
        row = self.s.get(RiskState, key)
        return row.value if row else default

    def _set(self, key: str, value) -> None:
        row = self.s.get(RiskState, key)
        if row:
            row.value = str(value)
        else:
            self.s.add(RiskState(key=key, value=str(value)))
        self.s.commit()

    def _event(self, kind: str, symbol: str = "", **detail) -> None:
        self.s.add(RiskEvent(ts=self.clock(), kind=kind, symbol=symbol, detail=detail))
        self.s.commit()

    def _halt(self, reason: str) -> None:
        if self._get("halted") != "1":
            self._set("halted", "1")
            self._set("halt_reason", reason)
            self._event("halt", reason=reason)
            self.on_alert(f"FULL HALT: {reason}")

    @property
    def halted(self) -> bool:
        return self._get("halted") == "1"

    def _today(self) -> str:
        return datetime.fromtimestamp(self.clock() / 1000, UTC).strftime("%Y-%m-%d")

    # ---------- checks ----------
    def config_intact(self) -> bool:
        try:
            ok = file_sha256(Path(self.limits.source_path)) == self.limits.source_sha256
        except OSError:
            ok = False
        if not ok:
            self._halt("risk.yaml changed or missing at runtime")
        return ok

    def update_equity(self, equity: float) -> dict:
        """Call on every mark. Tracks day-start and peak equity, trips the breakers.
        Returns {'daily_lock': bool, 'halted': bool} so the engine can react."""
        today = self._today()
        if self._get("day") != today:
            self._set("day", today)
            self._set("day_start_equity", equity)
            self._set("daily_lock", "")
        peak = max(float(self._get("peak_equity", "0") or 0), equity)
        self._set("peak_equity", peak)
        day_start = float(self._get("day_start_equity"))
        if self._get("daily_lock") != today and \
                equity <= day_start * (1 - self.limits.daily_loss_limit_pct / 100):
            self._set("daily_lock", today)
            self._event("daily_lock", equity=equity, day_start=day_start)
            self.on_alert(f"Daily loss limit hit: equity {equity:.2f} vs day start {day_start:.2f}. "
                          "No new trades until next UTC day.")
        if equity <= peak * (1 - self.limits.max_drawdown_pct / 100):
            self._halt(f"max drawdown: equity {equity:.2f} vs peak {peak:.2f}")
        return {"daily_lock": self._get("daily_lock") == today, "halted": self.halted}

    def trading_blockers(self) -> list[str]:
        reasons = []
        if not self.config_intact():
            reasons.append("risk config integrity check failed")
        if self.kill_switch.exists():
            reasons.append("kill switch (STOP file) active")
        if self.halted:
            reasons.append(f"trading halted: {self._get('halt_reason')}")
        if self._get("daily_lock") == self._today():
            reasons.append("daily loss limit reached")
        return reasons

    def evaluate(self, req: OrderRequest, account: AccountSnapshot) -> Decision:
        L = self.limits
        reasons = self.trading_blockers()

        if req.side not in ("long", "short"):
            reasons.append(f"invalid side {req.side!r}")
        if not _finite_pos(req.entry):
            reasons.append("invalid entry price")
        if not _finite_pos(account.equity):
            reasons.append("non-positive equity")
        if req.stop is None:
            reasons.append("stop-loss is mandatory")
        elif not _finite_pos(req.stop):
            reasons.append("invalid stop price")
        elif _finite_pos(req.entry) and req.side in ("long", "short"):
            if (req.side == "long" and req.stop >= req.entry) or \
                    (req.side == "short" and req.stop <= req.entry):
                reasons.append("stop on wrong side of entry")
        if req.take is not None and _finite_pos(req.entry):
            if not _finite_pos(req.take) or (req.side == "long" and req.take <= req.entry) or \
                    (req.side == "short" and req.take >= req.entry):
                reasons.append("take-profit on wrong side of entry")
        if any(p.symbol == req.symbol for p in account.positions):
            reasons.append(f"position already open on {req.symbol}")
        if len(account.positions) >= L.max_open_positions:
            reasons.append(f"max open positions ({L.max_open_positions}) reached")
        if req.requested_leverage is not None and \
                (not _finite_pos(req.requested_leverage) or req.requested_leverage > L.max_leverage):
            reasons.append(f"requested leverage {req.requested_leverage} > max {L.max_leverage}")

        qty = 0.0
        if not reasons:
            stop_dist = abs(req.entry - req.stop)
            risk_qty = account.equity * L.max_risk_per_trade_pct / 100 / stop_dist
            used = sum(p.qty * p.mark for p in account.positions)
            lev_qty = max(0.0, L.max_leverage * account.equity - used) / req.entry
            qty = min(risk_qty, lev_qty)
            if req.requested_qty is not None:
                if not _finite_pos(req.requested_qty):
                    reasons.append("invalid requested qty")
                elif req.requested_qty > qty * (1 + 1e-9):
                    reasons.append(f"requested qty {req.requested_qty} exceeds allowed {qty:.6f}")
                else:
                    qty = req.requested_qty
            if qty <= 0:
                reasons.append("no leverage headroom left")

        if reasons:
            self._event("reject", req.symbol, reasons=reasons, request=req.__dict__)
            return Decision(False, reasons)
        appr = Approval(uuid.uuid4().hex, req.symbol, req.side, qty, float(req.stop),
                        req.take, self.clock() + APPROVAL_TTL_MS)
        self._issued[appr.id] = appr
        self._event("approve", req.symbol, approval_id=appr.id, qty=qty, request=req.__dict__)
        return Decision(True, [], appr, qty)

    def consume(self, approval: Approval) -> Approval:
        """Broker calls this before filling. Single-use, unexpired, untampered."""
        orig = self._issued.pop(getattr(approval, "id", None), None)
        if orig is None:
            raise RiskViolation("unknown or already-used approval")
        if orig != approval:
            raise RiskViolation("approval was tampered with")
        if self.clock() > orig.expires_ts:
            raise RiskViolation("approval expired")
        blockers = self.trading_blockers()
        if blockers:
            raise RiskViolation("; ".join(blockers))
        return orig
