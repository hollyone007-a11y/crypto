"""Live public quotes for the paper broker (no API keys)."""
import time
import logging
import math

log = logging.getLogger(__name__)


class StaleQuote(RuntimeError):
    pass


class LiveFeed:
    def __init__(self, exchange, symbols: list[str], max_age_s: float = 180,
                 clock=time.time):
        from config.settings import to_ccxt_symbol
        self.ex = exchange
        self.symbols = symbols
        self.markets = {s: to_ccxt_symbol(s) for s in symbols}
        self.max_age_s = max_age_s
        self.clock = clock
        self.book: dict[str, tuple[float, float, float, float]] = {}  # bid, ask, last, at
        self.bars: dict[str, list] = {}  # last few CLOSED 1m bars
        self.bar_minute: dict[str, int] = {}

    def refresh(self) -> None:
        now = self.clock()
        markets=dict(self.markets)
        batch_method=getattr(self.ex,'fetch_quote_batch',None)
        batch=batch_method(list(markets.values())) if callable(batch_method) else None
        refreshed=0
        for sym, mkt in markets.items():
            try:
                t = batch.get(mkt,{}) if batch is not None else self.ex.fetch_ticker(mkt)
                timestamp=t.get('timestamp')
                if timestamp is not None and (now*1000-float(timestamp)>30000 or float(timestamp)>now*1000+30000):
                    raise StaleQuote(f'stale exchange timestamp for {sym}')
                bid,ask=float(t.get('bid') or 0),float(t.get('ask') or 0)
                last=float(t.get('last') or (bid+ask)/2)
                if not all(math.isfinite(v) and v>0 for v in (bid,ask,last)) or ask<bid:
                    raise StaleQuote(f'invalid quote for {sym}')
                self.book[sym]=(bid,ask,last,now)
                refreshed+=1
                minute=int(now//60)
                if self.bar_minute.get(sym)!=minute:
                    bars=self.ex.fetch_ohlcv(mkt,'1m',limit=4)
                    self.bars[sym]=[b for b in bars if b[0]+60_000<=now*1000]
                    self.bar_minute[sym]=minute
            except Exception:
                self.bars[sym]=[]
                log.warning('Public feed failed for %s; no quote fabricated',sym)
        if not refreshed:
            raise StaleQuote('No symbols refreshed')

    def set_symbols(self, symbols):
        from config.settings import to_ccxt_symbol
        self.symbols=list(symbols)
        self.markets={symbol:to_ccxt_symbol(symbol) for symbol in symbols}

    def quotes(self, symbol: str) -> tuple[float, float]:
        q = self.book.get(symbol)
        if q is None or self.clock() - q[3] > self.max_age_s:
            raise StaleQuote(f"no fresh quote for {symbol}")
        return q[0], q[1]

    def last(self, symbol: str) -> float:
        self.quotes(symbol)
        return self.book[symbol][2]
