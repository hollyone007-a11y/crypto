"""Paper broker: simulated fills against real quotes. State lives in the DB so a
restart resumes exactly where it stopped. Opening a position REQUIRES a valid
single-use Approval from RiskManager."""
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Order, PaperAccount, Position
from risk.manager import Approval, AccountSnapshot, OpenPos, RiskManager, now_ms

Quote = Callable[[str], tuple[float, float]]  # symbol -> (bid, ask)


class PaperBroker:
    def __init__(self, session: Session, risk: RiskManager, quotes: Quote,
                 taker_fee: float = 0.00055, slippage_bps: float = 2.0,
                 initial_equity: float = 10_000.0, clock: Callable[[], int] = now_ms):
        self.s = session
        self.risk = risk
        self.quotes = quotes
        self.fee = taker_fee
        self.slip = slippage_bps / 10_000
        self.clock = clock
        self.account = self.s.get(PaperAccount, 1)
        if self.account is None:
            self.account = PaperAccount(id=1, cash=initial_equity, initial=initial_equity)
            self.s.add(self.account)
            self.s.commit()

    # ---------- queries ----------
    def open_positions(self, symbol: str | None = None) -> list[Position]:
        q = select(Position).where(Position.status == "open")
        if symbol:
            q = q.where(Position.symbol == symbol)
        return list(self.s.scalars(q.order_by(Position.id)))

    def _mid(self, symbol: str) -> float:
        bid, ask = self.quotes(symbol)
        return (bid + ask) / 2

    def equity(self) -> float:
        unreal = 0.0
        for p in self.open_positions():
            sign = 1 if p.side == "long" else -1
            unreal += sign * (self._mid(p.symbol) - p.entry) * p.qty
        return self.account.cash + unreal

    def snapshot(self) -> AccountSnapshot:
        return AccountSnapshot(self.equity(), tuple(
            OpenPos(p.symbol, p.side, p.qty, self._mid(p.symbol)) for p in self.open_positions()))

    def order_by_coid(self, coid: str) -> Order | None:
        return self.s.scalar(select(Order).where(Order.client_order_id == coid))

    # ---------- actions ----------
    def open(self, approval: Approval, client_order_id: str, strategy: str = "") -> Position:
        existing = self.order_by_coid(client_order_id)
        if existing is not None:  # idempotent retry: same coid -> same position
            return self.s.get(Position, existing.position_id)
        a = self.risk.consume(approval)  # raises RiskViolation if not a valid approval
        bid, ask = self.quotes(a.symbol)
        px = ask * (1 + self.slip) if a.side == "long" else bid * (1 - self.slip)
        fee = self.fee * px * a.qty
        ts = self.clock()
        pos = Position(symbol=a.symbol, side=a.side, qty=a.qty, entry=px, stop=a.stop,
                       take=a.take, status="open", opened_ts=ts, fees=fee, funding=0.0,
                       mae=0.0, mfe=0.0, strategy=strategy, exit_reason="")
        self.s.add(pos)
        self.s.flush()
        self.s.add(Order(client_order_id=client_order_id, symbol=a.symbol,
                         side="buy" if a.side == "long" else "sell", qty=a.qty, price=px,
                         fee=fee, ts=ts, intent="open", position_id=pos.id))
        self.account.cash -= fee
        self.s.commit()
        return pos

    def close(self, pos: Position, reason: str, price: float | None = None) -> Position:
        if pos.status != "open":
            return pos
        coid = f"close-{pos.id}"
        if self.order_by_coid(coid) is not None:
            return pos
        bid, ask = self.quotes(pos.symbol)
        if price is None:
            price = bid if pos.side == "long" else ask
        px = price * (1 - self.slip) if pos.side == "long" else price * (1 + self.slip)
        fee = self.fee * px * pos.qty
        sign = 1 if pos.side == "long" else -1
        gross = sign * (px - pos.entry) * pos.qty
        pos.exit, pos.closed_ts, pos.status, pos.exit_reason = px, self.clock(), "closed", reason
        pos.fees += fee
        pos.pnl = gross - pos.fees + pos.funding
        self.account.cash += gross - fee
        self.s.add(Order(client_order_id=coid, symbol=pos.symbol,
                         side="sell" if pos.side == "long" else "buy", qty=pos.qty, price=px,
                         fee=fee, ts=pos.closed_ts, intent="close", position_id=pos.id))
        self.s.commit()
        return pos

    def mark(self, symbol: str, high: float, low: float, last: float,
             bar_start_ts: int | None = None) -> list[Position]:
        """Process a price update (bar high/low/last). Returns positions closed by stop/take.
        Stop is checked before take (conservative); a gap through the stop fills at last.
        If bar_start_ts is given, positions opened after the bar started are skipped."""
        closed = []
        for p in self.open_positions(symbol):
            if bar_start_ts is not None and p.opened_ts > bar_start_ts:
                continue
            if p.side == "long":
                p.mae = max(p.mae, p.entry - low)
                p.mfe = max(p.mfe, high - p.entry)
                if low <= p.stop:
                    closed.append(self.close(p, "stop", min(p.stop, last)))
                elif p.take is not None and high >= p.take:
                    closed.append(self.close(p, "take", p.take))
            else:
                p.mae = max(p.mae, high - p.entry)
                p.mfe = max(p.mfe, p.entry - low)
                if high >= p.stop:
                    closed.append(self.close(p, "stop", max(p.stop, last)))
                elif p.take is not None and low <= p.take:
                    closed.append(self.close(p, "take", p.take))
        self.s.commit()
        return closed

    def apply_funding(self, symbol: str, rate: float, mark_price: float,
                      at_ts: int | None = None) -> float:
        """Charge/credit funding to positions that were open at settlement time at_ts."""
        total = 0.0
        for p in self.open_positions(symbol):
            if at_ts is not None and p.opened_ts >= at_ts:
                continue
            amt = -(1 if p.side == "long" else -1) * rate * p.qty * mark_price
            p.funding += amt
            self.account.cash += amt
            total += amt
        self.s.commit()
        return total

    def close_all(self, reason: str) -> list[Position]:
        return [self.close(p, reason) for p in self.open_positions()]

    def reconcile(self) -> list[str]:
        """Startup check of persisted state. Paper state is its own source of truth;
        any open position without a valid stop is flattened immediately."""
        issues = []
        for p in self.open_positions():
            bad = p.stop is None or p.stop <= 0 or \
                (p.side == "long" and p.stop >= p.entry) or (p.side == "short" and p.stop <= p.entry)
            if bad:
                issues.append(f"position {p.id} {p.symbol} had invalid stop -> closed")
                self.close(p, "reconcile")
        return issues
