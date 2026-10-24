"""CLI: python -m data.collect [--backfill-days N] [--timeframes 1m,1h,...]"""
import argparse
import logging

from config.settings import get_settings
from data.market import MarketCollector, now_ms, public_exchange
from data.news import NewsCollector
from db.session import init_db, make_engine, make_session_factory

log = logging.getLogger("collect")
DAY_MS = 86_400_000


def run(backfill_days: int, timeframes: list[str], with_news: bool = True) -> None:
    s = get_settings()
    engine = make_engine(s.database_url)
    init_db(engine)
    Session = make_session_factory(engine)
    since = now_ms() - backfill_days * DAY_MS
    with Session() as session:
        mc = MarketCollector(public_exchange(s.exchange), session, s.exchange)
        for sym in s.symbol_list:
            for tf in timeframes:
                # 1m over long windows is huge; cap it to 30 days
                tf_since = max(since, now_ms() - 30 * DAY_MS) if tf == "1m" else since
                mc.collect_ohlcv(sym, tf, tf_since)
            mc.collect_funding(sym, since)
            mc.collect_open_interest(sym, max(since, now_ms() - 180 * DAY_MS))
            mc.snapshot_orderbook(sym)
        if with_news:
            nc = NewsCollector(session)
            nc.collect_rss()
            nc.collect_fear_greed(limit=max(30, backfill_days))
            nc.collect_calendar()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--backfill-days", type=int, default=3)
    p.add_argument("--timeframes", default="1m,15m,1h,4h,1d")
    p.add_argument("--no-news", action="store_true")
    a = p.parse_args()
    run(a.backfill_days, a.timeframes.split(","), not a.no_news)


if __name__ == "__main__":
    main()
