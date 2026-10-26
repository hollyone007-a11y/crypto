"""Read-only public Hyperliquid data. No wallets, keys, signatures or order API."""
import math
import re
import time
import httpx

INFO = 'https://api.hyperliquid.xyz/info'
LEADERBOARD = 'https://stats-data.hyperliquid.xyz/Mainnet/leaderboard'
ADDRESS = re.compile(r'^0x[0-9a-fA-F]{40}$')


def number(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('nonfinite source number')
    return result


def rank_traders(rows, limit=3):
    """Predeclared research screen; historical profit is not evidence of future skill."""
    ranked = []
    for row in rows:
        try:
            address = row['ethAddress'].lower()
            if not ADDRESS.fullmatch(address):
                continue
            windows = dict(row['windowPerformances'])
            month, week, lifetime = (windows[x] for x in ('month', 'week', 'allTime'))
            equity, roi = number(row['accountValue']), number(month['roi'])
            if not (equity >= 100_000 and .01 <= roi <= 2 and number(month['pnl']) > 0
                    and number(week['pnl']) > 0 and number(lifetime['pnl']) > 0
                    and number(month['vlm']) >= 1_000_000):
                continue
            ranked.append(dict(address=address, name=str(row.get('displayName') or address)[:100],
                               equity=equity, month_roi=roi, month_pnl=number(month['pnl']),
                               week_pnl=number(week['pnl']), alltime_pnl=number(lifetime['pnl'])))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(ranked, key=lambda x: (-x['month_roi'], -x['month_pnl'], x['address']))[:limit]


def collect(previous, now, tracked=(), client=None):
    """Network operations happen outside the account writer lock. Cache ranking 6h."""
    own_client = client is None
    client = client or httpx.Client(timeout=12, follow_redirects=False)
    result = dict(previous)
    result.update(ts=now, errors=[], wallets={})
    try:
        if not previous.get('traders') or now - previous.get('rank_ts', 0) >= 6 * 3_600_000:
            try:
                response = client.get(LEADERBOARD)
                response.raise_for_status()
                result['traders'] = rank_traders(response.json()['leaderboardRows'])
                result['rank_ts'] = now
            except Exception as exc:
                result['errors'].append('leaderboard: ' + type(exc).__name__)
        addresses = [row['address'] for row in result.get('traders', [])]
        addresses = list(dict.fromkeys(addresses + [x for x in tracked if ADDRESS.fullmatch(x)]))[:6]
        for address in addresses:
            try:
                fills = client.post(INFO, json=dict(type='userFillsByTime', user=address,
                    startTime=now - 120_000, endTime=now, aggregateByTime=False))
                fills.raise_for_status()
                state = client.post(INFO, json=dict(type='clearinghouseState', user=address))
                state.raise_for_status()
                payload = state.json()
                source_ts = int(payload['time'])
                received = int(time.time()*1000) if own_client else now
                if not -10_000 <= received - source_ts <= 60_000:
                    raise ValueError('stale position snapshot')
                positions = {x['position']['coin']: number(x['position']['szi'])
                             for x in payload['assetPositions']}
                records = fills.json()
                if not isinstance(records, list) or any(not isinstance(x,dict) or not isinstance(x.get('time'),int) for x in records):
                    raise ValueError('invalid fills')
                result['wallets'][address] = dict(ts=now, source_ts=source_ts,
                    positions=positions, fills=records[:2000])
            except Exception as exc:
                result['errors'].append(address + ': ' + type(exc).__name__)
        return result
    finally:
        if own_client:
            client.close()
