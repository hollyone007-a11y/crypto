"""Human-only CLI. The agent never calls this.
    python -m risk.admin status
    python -m risk.admin reset-halt   # after a max-drawdown halt, once you have reviewed it
"""
import sys

from sqlalchemy import select

from config.settings import get_settings
from db.models import RiskState
from db.session import init_db, make_engine, make_session_factory


def main(argv: list[str]) -> int:
    engine = make_engine(get_settings().database_url)
    init_db(engine)
    with make_session_factory(engine)() as s:
        if argv[:1] == ["reset-halt"]:
            for key in ("halted", "halt_reason", "peak_equity"):
                row = s.get(RiskState, key)
                if row:
                    s.delete(row)
            s.commit()
            print("halt cleared; peak equity will restart from current equity")
        for row in s.scalars(select(RiskState)):
            print(f"{row.key} = {row.value}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
