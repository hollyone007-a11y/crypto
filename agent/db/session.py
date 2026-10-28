import threading
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from db.models import Base


def make_engine(url: str) -> Engine:
    engine = create_engine(url, future=True)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _pragmas(conn, _):
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()
    return engine


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)


_writers = {}
_writers_guard = threading.Lock()


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    if engine.dialect.name != 'sqlite':
        return sessionmaker(engine, expire_on_commit=False)
    # WAL permits concurrent readers, but SQLite still has only one writer.
    # Serialize complete write transactions, including ORM autoflush, rather
    # than retrying a partly executed account transaction after an error.
    key = str(engine.url) if engine.url.database not in (None, '', ':memory:') else id(engine)
    with _writers_guard:
        lock = _writers.setdefault(key, threading.RLock())

    class WriterSession(Session):
        pass

    def acquire(session):
        if not session.in_transaction():
            session.begin()
        if not session.info.get('_writer_held'):
            if not lock.acquire(timeout=30):
                raise TimeoutError('SQLite writer busy for 30 seconds; transaction not retried')
            session.info['_writer_held'] = True

    @event.listens_for(WriterSession, 'before_flush')
    def before_flush(session, *_):
        acquire(session)

    @event.listens_for(WriterSession, 'do_orm_execute')
    def before_execute(state):
        if state.is_insert or state.is_update or state.is_delete:
            acquire(state.session)

    @event.listens_for(WriterSession, 'after_transaction_end')
    def release(session, transaction):
        if transaction.parent is None and session.info.pop('_writer_held', False):
            lock.release()

    return sessionmaker(engine, class_=WriterSession, expire_on_commit=False)


def insert_ignore(session: Session, model, rows: list[dict]) -> int:
    """INSERT ... ON CONFLICT DO NOTHING. Returns number of rows actually inserted."""
    if not rows:
        return 0
    dialect = session.get_bind().dialect.name
    insert = {"sqlite": sqlite_insert, "postgresql": pg_insert}[dialect]
    total = 0
    for i in range(0, len(rows), 500):  # stay under SQLite variable limit
        stmt = insert(model).values(rows[i : i + 500]).on_conflict_do_nothing()
        total += session.execute(stmt).rowcount or 0
    session.commit()
    return total
