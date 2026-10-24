"""Market data collector: OHLCV, funding, open interest, order book.

Uses a PUBLIC ccxt client (no API keys) — the data layer can never trade.
The exchange object is injected so tests can use a fake.
"""
import logging
import time

import ccxt
from sqlalchemy.orm import Session

from config.settings import to_ccxt_symbol
from db.models import FundingRate, Ohlcv, OpenInterest, OrderBookSnapshot
from db.session import insert_ignore

log = logging.getLogger(__name__)

TIMEFRAME_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000,
    "4h": 14_400_000, "1d": 86_400_000,
}


class BinancePublic(ccxt.binanceusdm):
    """Public Binance futures quotes; 24h tickers omit bid/ask."""

    def fetch_ticker(self, symbol, params=None):
        ticker = super().fetch_ticker(symbol, params or {})
        book = self.fetch_order_book(symbol, limit=5)
        if not book.get("bids") or not book.get("asks"):
            raise ValueError("Binance returned an empty order book")
        ticker["bid"] = float(book["bids"][0][0])
        ticker["ask"] = float(book["asks"][0][0])
        return ticker

    def fetch_quote_batch(self, symbols):
        return self.fetch_bids_asks(symbols)


def public_exchange(name: str = "bybit") -> ccxt.Exchange:
    if name == "binanceusdm":
        return BinancePublic({"enableRateLimit": True, "timeout": 15000})
    cls = getattr(ccxt, name)
    return cls({"enableRateLimit": True, "options": {"defaultType": "swap"}})


def now_ms() -> int:
    return int(time.time() * 1000)


class MarketCollector:
    def __init__(self, exchange, session: Session, exchange_name: str = "bybit"):
        self.ex = exchange
        self.session = session
        self.name = exchange_name

    def collect_ohlcv(self, symbol: str, timeframe: str, since_ms: int,
                      until_ms: int | None = None, limit: int = 1000) -> int:
        until_ms = until_ms or now_ms()
        step = TIMEFRAME_MS[timeframe]
        market = to_ccxt_symbol(symbol)
        cursor, inserted = since_ms, 0
        while cursor < until_ms:
            batch = self.ex.fetch_ohlcv(market, timeframe, since=cursor, limit=limit)
            # drop the still-forming candle: only closed bars go into the DB
            closed = [b for b in batch if b[0] + step <= now_ms() and b[0] < until_ms]
            rows = [
                dict(exchange=self.name, symbol=symbol, timeframe=timeframe, ts=int(b[0]),
                     open=float(b[1]), high=float(b[2]), low=float(b[3]),
                     close=float(b[4]), volume=float(b[5] or 0))
                for b in closed
            ]
            inserted += insert_ignore(self.session, Ohlcv, rows)
            if not batch:
                break
            nxt = int(batch[-1][0]) + step
            if nxt <= cursor:
                break
            cursor = nxt
        log.info("ohlcv %s %s: +%d", symbol, timeframe, inserted)
        return inserted

    def collect_funding(self, symbol: str, since_ms: int, limit: int = 200) -> int:
        market = to_ccxt_symbol(symbol)
        cursor, inserted = since_ms, 0
        while True:
            batch = self.ex.fetch_funding_rate_history(market, since=cursor, limit=limit)
            rows = [dict(exchange=self.name, symbol=symbol, ts=int(r["timestamp"]),
                         rate=float(r["fundingRate"])) for r in batch if r.get("timestamp")]
            inserted += insert_ignore(self.session, FundingRate, rows)
            if len(batch) < limit:
                break
            nxt = max(r["timestamp"] for r in batch) + 1
            if nxt <= cursor:
                break
            cursor = nxt
        log.info("funding %s: +%d", symbol, inserted)
        return inserted

    def collect_open_interest(self, symbol: str, since_ms: int, timeframe: str = "1h",
                              limit: int = 200) -> int:
        market = to_ccxt_symbol(symbol)
        cursor, inserted = since_ms, 0
        while True:
            batch = self.ex.fetch_open_interest_history(market, timeframe, since=cursor, limit=limit)
            rows = [dict(exchange=self.name, symbol=symbol, timeframe=timeframe,
                         ts=int(r["timestamp"]),
                         amount=_f(r.get("openInterestAmount")),
                         value=_f(r.get("openInterestValue")))
                    for r in batch if r.get("timestamp")]
            inserted += insert_ignore(self.session, OpenInterest, rows)
            if len(batch) < limit:
                break
            nxt = max(r["timestamp"] for r in batch) + 1
            if nxt <= cursor:
                break
            cursor = nxt
        log.info("oi %s: +%d", symbol, inserted)
        return inserted

    def snapshot_orderbook(self, symbol: str, depth: int = 50, top_n: int = 10) -> int:
        ob = self.ex.fetch_order_book(to_ccxt_symbol(symbol), limit=depth)
        bids = [[float(p), float(a)] for p, a, *_ in ob["bids"]]
        asks = [[float(p), float(a)] for p, a, *_ in ob["asks"]]
        if not bids or not asks:
            return 0
        bv = sum(a for _, a in bids[:top_n])
        av = sum(a for _, a in asks[:top_n])
        row = dict(exchange=self.name, symbol=symbol, ts=int(ob.get("timestamp") or now_ms()),
                   bids=bids, asks=asks, spread=asks[0][0] - bids[0][0],
                   imbalance=(bv - av) / (bv + av) if bv + av else 0.0)
        return insert_ignore(self.session, OrderBookSnapshot, [row])


def _f(v):
    return float(v) if v is not None else None
