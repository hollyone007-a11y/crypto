"""DB schema. All timestamps are UTC epoch milliseconds (BigInteger) — unambiguous
across SQLite and PostgreSQL. Natural unique keys make every insert idempotent."""
from sqlalchemy import JSON, BigInteger, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Ohlcv(Base):
    __tablename__ = "ohlcv"
    __table_args__ = (UniqueConstraint("exchange", "symbol", "timeframe", "ts"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exchange: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timeframe: Mapped[str] = mapped_column(String(8))
    ts: Mapped[int] = mapped_column(BigInteger, index=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)


class FundingRate(Base):
    __tablename__ = "funding_rates"
    __table_args__ = (UniqueConstraint("exchange", "symbol", "ts"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exchange: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    ts: Mapped[int] = mapped_column(BigInteger, index=True)
    rate: Mapped[float] = mapped_column(Float)


class OpenInterest(Base):
    __tablename__ = "open_interest"
    __table_args__ = (UniqueConstraint("exchange", "symbol", "timeframe", "ts"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exchange: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timeframe: Mapped[str] = mapped_column(String(8))
    ts: Mapped[int] = mapped_column(BigInteger, index=True)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)


class OrderBookSnapshot(Base):
    __tablename__ = "orderbook_snapshots"
    __table_args__ = (UniqueConstraint("exchange", "symbol", "ts"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exchange: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    ts: Mapped[int] = mapped_column(BigInteger, index=True)
    bids: Mapped[list] = mapped_column(JSON)  # [[price, amount], ...] best first
    asks: Mapped[list] = mapped_column(JSON)
    spread: Mapped[float] = mapped_column(Float)
    imbalance: Mapped[float] = mapped_column(Float)  # (bidVol-askVol)/(bidVol+askVol), top N


class News(Base):
    __tablename__ = "news"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    url: Mapped[str] = mapped_column(String(1024), unique=True)
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    published_ts: Mapped[int] = mapped_column(BigInteger, index=True)
    fetched_ts: Mapped[int] = mapped_column(BigInteger)


class MacroEvent(Base):
    __tablename__ = "macro_events"
    __table_args__ = (UniqueConstraint("ts", "country", "title"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[int] = mapped_column(BigInteger, index=True)
    country: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(256))
    impact: Mapped[str] = mapped_column(String(16))
    forecast: Mapped[str] = mapped_column(String(64), default="")
    previous: Mapped[str] = mapped_column(String(64), default="")


class FearGreed(Base):
    __tablename__ = "fear_greed"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[int] = mapped_column(BigInteger, unique=True)
    value: Mapped[int] = mapped_column(Integer)
    classification: Mapped[str] = mapped_column(String(32))


# ---- Ф3: paper account, orders, positions, risk state, journal ----

class PaperAccount(Base):
    __tablename__ = "paper_account"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cash: Mapped[float] = mapped_column(Float)          # realized balance (USDT)
    initial: Mapped[float] = mapped_column(Float)


class Position(Base):
    __tablename__ = "positions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(8))         # long / short
    qty: Mapped[float] = mapped_column(Float)
    entry: Mapped[float] = mapped_column(Float)
    stop: Mapped[float] = mapped_column(Float)
    take: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(8), index=True, default="open")
    opened_ts: Mapped[int] = mapped_column(BigInteger)
    closed_ts: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    exit: Mapped[float | None] = mapped_column(Float, nullable=True)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    funding: Mapped[float] = mapped_column(Float, default=0.0)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)  # net, set on close
    mae: Mapped[float] = mapped_column(Float, default=0.0)  # worst adverse price move
    mfe: Mapped[float] = mapped_column(Float, default=0.0)  # best favourable price move
    exit_reason: Mapped[str] = mapped_column(String(16), default="")
    strategy: Mapped[str] = mapped_column(String(64), default="")


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))         # buy / sell
    qty: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    fee: Mapped[float] = mapped_column(Float)
    ts: Mapped[int] = mapped_column(BigInteger)
    intent: Mapped[str] = mapped_column(String(16))      # open / close
    position_id: Mapped[int] = mapped_column(Integer, index=True)


class RiskState(Base):
    __tablename__ = "risk_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class RiskEvent(Base):
    """Audit log of every risk decision."""
    __tablename__ = "risk_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(16))        # approve / reject / halt / info
    symbol: Mapped[str] = mapped_column(String(32), default="")
    detail: Mapped[dict] = mapped_column(JSON)


class JournalEntry(Base):
    __tablename__ = "journal"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_id: Mapped[int] = mapped_column(Integer, unique=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    strategy: Mapped[str] = mapped_column(String(64))
    side: Mapped[str] = mapped_column(String(8))
    thesis: Mapped[str] = mapped_column(Text, default="")
    signal: Mapped[dict] = mapped_column(JSON)
    regime: Mapped[str] = mapped_column(String(16), default="")
    context: Mapped[dict] = mapped_column(JSON)          # market snapshot at entry
    entry_ts: Mapped[int] = mapped_column(BigInteger)
    entry: Mapped[float] = mapped_column(Float)
    qty: Mapped[float] = mapped_column(Float)
    exit_ts: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    exit: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    r: Mapped[float | None] = mapped_column(Float, nullable=True)
    mae_r: Mapped[float | None] = mapped_column(Float, nullable=True)
    mfe_r: Mapped[float | None] = mapped_column(Float, nullable=True)
    fees: Mapped[float | None] = mapped_column(Float, nullable=True)
    funding: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str] = mapped_column(String(16), default="")


# ---- Ф4: analyst runs, lessons, strategy versions, promotion log ----

class AnalystRun(Base):
    __tablename__ = "analyst_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[int] = mapped_column(BigInteger, index=True)
    purpose: Mapped[str] = mapped_column(String(32))      # decide / lesson / weekly
    model: Mapped[str] = mapped_column(String(64))
    ok: Mapped[bool] = mapped_column(Integer)
    error: Mapped[str] = mapped_column(Text, default="")
    request: Mapped[dict] = mapped_column(JSON)
    response: Mapped[str] = mapped_column(Text, default="")  # raw text as returned
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)


class Lesson(Base):
    __tablename__ = "lessons"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[int] = mapped_column(BigInteger, index=True)
    journal_id: Mapped[int] = mapped_column(Integer, unique=True)
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    outcome: Mapped[str] = mapped_column(String(16))      # win / loss / scratch
    what_worked: Mapped[str] = mapped_column(Text)
    mistake: Mapped[str] = mapped_column(Text)
    lesson: Mapped[str] = mapped_column(Text)
    tags: Mapped[list] = mapped_column(JSON)


class StrategyVersion(Base):
    __tablename__ = "strategy_versions"
    __table_args__ = (UniqueConstraint("family", "version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    family: Mapped[str] = mapped_column(String(64), index=True)   # e.g. trend_donchian
    version: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(8))                   # params / code
    params: Mapped[dict] = mapped_column(JSON)
    code_path: Mapped[str] = mapped_column(String(256), default="")
    # candidate -> paper_trial -> production -> retired | rejected | rolled_back
    status: Mapped[str] = mapped_column(String(16), index=True)
    parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rationale: Mapped[str] = mapped_column(Text, default="")
    created_ts: Mapped[int] = mapped_column(BigInteger)
    trial_start_ts: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    promoted_ts: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)


class PromotionEvent(Base):
    """Decision log of the learning pipeline."""
    __tablename__ = "promotion_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[int] = mapped_column(BigInteger, index=True)
    version_id: Mapped[int] = mapped_column(Integer, index=True)
    step: Mapped[str] = mapped_column(String(32))          # proposed / walk_forward / paper / promote / rollback
    passed: Mapped[bool] = mapped_column(Integer)
    detail: Mapped[dict] = mapped_column(JSON)


# ---- Ф5: ops ----

class AgentState(Base):
    """Generic key-value state for ops jobs (cursors, heartbeats)."""
    __tablename__ = "agent_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[int] = mapped_column(BigInteger, index=True)
    equity: Mapped[float] = mapped_column(Float)
    cash: Mapped[float] = mapped_column(Float)
    open_positions: Mapped[int] = mapped_column(Integer)


class SignalObservation(Base):
    """Research-only unit-notional outcomes; never account orders or positions."""
    __tablename__ = 'signal_observations'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[str] = mapped_column(String(24), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    method: Mapped[str] = mapped_column(String(32))
    timeframe: Mapped[str] = mapped_column(String(8))
    policy: Mapped[str] = mapped_column(String(12))
    mode: Mapped[str] = mapped_column(String(12), index=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    created_ts: Mapped[int] = mapped_column(BigInteger, index=True)
    signal_ts: Mapped[int] = mapped_column(BigInteger, index=True)
    entry_due_ts: Mapped[int] = mapped_column(BigInteger)
    cursor_ts: Mapped[int] = mapped_column(BigInteger, default=0)
    side: Mapped[int] = mapped_column(Integer)
    reference: Mapped[float] = mapped_column(Float)
    stop: Mapped[float] = mapped_column(Float)
    take: Mapped[float] = mapped_column(Float)
    entry: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_ts: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    exit_ts: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    net_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_r: Mapped[float | None] = mapped_column(Float, nullable=True)
    fees_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    funding_pct: Mapped[float] = mapped_column(Float, default=0.)
    prediction_r: Mapped[float | None] = mapped_column(Float, nullable=True)
    prediction_n: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(String(160), default='')
