"""Glue: proposal -> RiskManager -> broker -> journal. The only entry point the
rest of the agent (strategies, LLM analyst) is allowed to use for trading."""
import uuid
from dataclasses import dataclass
from typing import Callable

from sqlalchemy.orm import Session

from journal.journal import Journal
from risk.manager import OrderRequest, RiskManager, RiskViolation
from strategies.base import Signal


@dataclass
class SubmitResult:
    ok: bool
    reasons: list[str]
    position_id: int | None = None
    qty: float = 0.0


class ExecutionEngine:
    def __init__(self, session: Session, risk: RiskManager, broker, journal: Journal,
                 alert: Callable[[str], None] | None = None):
        self.s = session
        self.risk = risk
        self.broker = broker
        self.journal = journal
        self.alert = alert or (lambda m: None)
        self.profile = None  # supplied only by the running App, never inferred from a signal

    def startup(self) -> list[str]:
        issues = self.broker.reconcile()
        for i in issues:
            self.alert(f"reconcile: {i}")
        self.guard()
        return issues

    def submit(self, symbol: str, signal: Signal, strategy: str, thesis: str = "",
               regime: str = "", context: dict | None = None,
               requested_qty: float | None = None, requested_leverage: float | None = None,
               client_order_id: str | None = None) -> SubmitResult:
        coid = client_order_id or uuid.uuid4().hex
        prior = self.broker.order_by_coid(coid)
        if prior is not None:  # idempotent replay
            return SubmitResult(True, ["duplicate client_order_id: returning existing"],
                                prior.position_id, prior.qty)
        from analyst.news_risk import assess
        news = assess(self.s, symbol, self.broker.clock())
        if news['blockers']:
            return SubmitResult(False, news['blockers'])
        context = {**(context or {}), 'news': news}
        if self.profile and self.profile.get('experimental_mode'):
            from sqlalchemy import select,func
            from db.models import Position
            start=(self.broker.clock()//86_400_000)*86_400_000
            opened=self.s.scalar(select(func.count()).select_from(Position).where(Position.status=='open'))
            today=self.s.scalar(select(func.count()).select_from(Position).where(Position.opened_ts>=start))
            if opened or today>=2:
                reasons=['Учебное демо: максимум 1 открытая позиция и 2 входа за UTC-день по всему счёту.']
                self.risk._event('reject',symbol,reasons=reasons)
                return SubmitResult(False,reasons)
        if self.profile and self.profile.get('active_demo'):
            from sqlalchemy import select,func
            from db.models import Position
            start=(self.broker.clock()//86_400_000)*86_400_000
            base=select(func.count()).select_from(Position).where(Position.strategy.like('mtf_%_active'))
            opened=self.s.scalar(base.where(Position.status=='open'))
            today=self.s.scalar(base.where(Position.opened_ts>=start))
            if opened or today>=2:
                reasons=['Активное демо: максимум 1 открытая позиция и 2 входа за UTC-день.']
                self.risk._event('reject',symbol,reasons=reasons)
                return SubmitResult(False,reasons)
        bid, ask = self.broker.quotes(symbol)
        ref = ask if signal.side == "long" else bid
        if self.profile and self.profile.get('margin_pct',0):
            profile = self.profile
            equity = self.broker.equity()
            quantity = equity * profile['margin_pct']/100 * profile['leverage']/ref
            # Allow no silent reduction: 10% margin means exactly that target, or no entry.
            cost_risk = quantity * (abs(ref-signal.stop) + ref*(2*self.broker.fee+2*self.broker.slip))
            budget = equity*self.risk.limits.max_risk_per_trade_pct/100
            reasons=[]
            if cost_risk > budget:
                reasons.append(f'10× / залог 10%: риск со стопом и оценкой издержек {cost_risk/equity*100:.3f}% > лимита {self.risk.limits.max_risk_per_trade_pct:.3f}%; вход пропущен.')
            if requested_qty is not None and quantity > requested_qty*(1+1e-9):
                reasons.append('Размер 10× / 10% превышает отдельный лимит риска источника копии.')
            if reasons:
                self.risk._event('reject',symbol,reasons=reasons,profile=profile)
                return SubmitResult(False,reasons)
            requested_qty, requested_leverage = quantity, profile['leverage']
            context = {**context, 'execution_profile':{**profile,'equity_at_entry':equity,
                'target_notional':quantity*ref,'target_margin':equity*profile['margin_pct']/100}}
        self.risk.update_equity(self.broker.equity())
        req = OrderRequest(symbol=symbol, side=signal.side, entry=ref, stop=signal.stop,
                           take=signal.take, strategy=strategy, requested_qty=requested_qty,
                           requested_leverage=requested_leverage)
        d = self.risk.evaluate(req, self.broker.snapshot())
        if not d.approved:
            return SubmitResult(False, d.reasons)
        try:
            pos = self.broker.open(d.approval, coid, strategy)
        except RiskViolation as e:
            return SubmitResult(False, [str(e)])
        self.journal.record_open(pos, thesis, signal.to_dict(), regime, context)
        return SubmitResult(True, [], pos.id, pos.qty)

    def on_price(self, symbol: str, high: float, low: float, last: float,
                 bar_start_ts: int | None = None) -> list:
        closed = self.broker.mark(symbol, high, low, last, bar_start_ts)
        for p in closed:
            self.journal.record_close(p)
        return closed + self.guard()

    def guard(self) -> list:
        """Breakers: max drawdown / kill switch -> flatten everything."""
        state = self.risk.update_equity(self.broker.equity())
        reason = None
        if self.risk.kill_switch.exists():
            reason = "kill_switch"
        elif state["halted"] or not self.risk.config_intact():
            reason = "halt"
        if reason is None or not self.broker.open_positions():
            return []
        closed = self.broker.close_all(reason)
        for p in closed:
            self.journal.record_close(p)
        self.alert(f"Flattened {len(closed)} position(s): {reason}")
        return closed
