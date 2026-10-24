"""Liquidity-ranked public Binance universe; not a prediction or endorsement."""
import math

STABLES = {'USDC','USDT','FDUSD','TUSD','DAI','USDP','BUSD','USD1','USDE','USDD','EUR','EURI'}
DAY = 86_400_000


def select_universe(markets, tickers, now_ms, size=20, min_volume=50_000_000):
    rows=[]
    for market in markets.values():
        if not (market.get('active') and market.get('swap') and market.get('linear') and
                market.get('quote')=='USDT' and market.get('settle')=='USDT'):
            continue
        if market.get('base') in STABLES:
            continue
        info=market.get('info',{})
        if info.get('underlyingType')!='COIN' or info.get('contractType')!='PERPETUAL':
            continue
        listed=market.get('info',{}).get('onboardDate')
        try:
            if not listed or now_ms-int(listed)<30*DAY:
                continue
            ticker=tickers.get(market['symbol'],{})
            volume=float(ticker.get('quoteVolume') or 0)
            price=float(ticker.get('last') or 0)
            if not math.isfinite(volume) or not math.isfinite(price) or volume<min_volume or price<=0:
                continue
        except (ValueError,TypeError):
            continue
        rows.append({'symbol':market['id'],'market':market['symbol'],'quote_volume':volume,
                     'last':price,'change_pct':ticker.get('percentage')})
    return sorted(rows,key=lambda row:(-row['quote_volume'],row['symbol']))[:size]
