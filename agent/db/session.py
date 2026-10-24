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
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()
    return engine


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


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
