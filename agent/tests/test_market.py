from sqlalchemy import func, select

from data.market import MarketCollector, now_ms
from db.models import FundingRate, Ohlcv, OpenInterest, OrderBookSnapshot

H = 3_600_000


class FakeExchange:
    """Mimics the ccxt calls we use. Serves hourly candles from `start` to now."""

    def __init__(self, start):
        self.start = start
        self.calls = []

    def fetch_ohlcv(self, symbol, timeframe, since, limit):
        self.calls.append(("ohlcv", symbol, since))
        t = max(since, self.start)
        t -= t % H
        out = []
        while t <= now_ms() and len(out) < limit:  # includes the still-open bar
            out.append([t, 100.0, 101.0, 99.0, 100.5, 10.0])
            t += H
        return out

    def fetch_funding_rate_history(self, symbol, since, limit):
        ts = [self.start + i * 8 * H for i in range(5)]
        return [{"timestamp": t, "fundingRate": 0.0001} for t in ts if t >= since][:limit]

    def fetch_open_interest_history(self, symbol, timeframe, since, limit):
        ts = [self.start + i * H for i in range(7)]
        return [{"timestamp": t, "openInterestAmount": 1000.0 + t % 7, "openInterestValue": None}
                for t in ts if t >= since][:limit]

    def fetch_order_book(self, symbol, limit):
        return {"timestamp": 1_700_000_000_000,
                "bids": [[99.0, 3.0], [98.0, 1.0]], "asks": [[101.0, 1.0], [102.0, 1.0]]}


def count(session, model):
    return session.scalar(select(func.count()).select_from(model))


def test_ohlcv_paginates_skips_open_bar_and_is_idempotent(session):
    start = now_ms() - 50 * H
    ex = FakeExchange(start)
    mc = MarketCollector(ex, session)
    n = mc.collect_ohlcv("BTCUSDT", "1h", start, limit=20)
    assert n == count(session, Ohlcv)
    assert 49 <= n <= 50                  # forming bar excluded
    assert len(ex.calls) >= 3             # paginated with limit=20
    assert ex.calls[0][1] == "BTC/USDT:USDT"
    last = session.scalar(select(func.max(Ohlcv.ts)))
    assert last + H <= now_ms()
    assert mc.collect_ohlcv("BTCUSDT", "1h", start, limit=20) == 0  # re-run inserts nothing


def test_funding_and_oi(session):
    start = 1_700_000_000_000
    mc = MarketCollector(FakeExchange(start), session)
    assert mc.collect_funding("ETHUSDT", start, limit=2) == 5
    assert mc.collect_funding("ETHUSDT", start, limit=2) == 0
    assert mc.collect_open_interest("ETHUSDT", start, limit=3) == 7
    row = session.scalars(select(OpenInterest)).first()
    assert row.value is None and row.amount >= 1000
    assert count(session, FundingRate) == 5


def test_orderbook_snapshot(session):
    mc = MarketCollector(FakeExchange(0), session)
    assert mc.snapshot_orderbook("BTCUSDT") == 1
    snap = session.scalars(select(OrderBookSnapshot)).one()
    assert snap.spread == 2.0
    assert snap.imbalance == (4.0 - 2.0) / 6.0
    assert snap.bids[0] == [99.0, 3.0]
