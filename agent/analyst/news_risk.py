"""Auditable headline risk, never a claim that text predicts a price move."""
import json
import re
from sqlalchemy import select
from db.models import AgentState, News, MacroEvent

HOUR = 3_600_000
ALIASES = {'BTC': ['bitcoin'], 'ETH': ['ethereum', 'ether'], 'SOL': ['solana'],
           'BNB': ['binance coin'], 'XRP': ['ripple'], 'DOGE': ['dogecoin'],
           'ADA': ['cardano'], 'AVAX': ['avalanche'], 'LINK': ['chainlink']}
CRISIS = re.compile(r'\b(hack(?:ed)?|exploit(?:ed)?|bankrupt(?:cy)?|insolven\w*|'
                    r'delist(?:ing|ed)?|withdrawals? (?:halted|suspended)|depeg\w*)\b', re.I)
MARKET = re.compile(r'\b(bitcoin|crypto(?:currency)?|federal reserve|fomc|inflation|interest rate|sec)\b', re.I)


def read_state(session, key):
    row = session.get(AgentState, key)
    try:
        return json.loads(row.value) if row else {}
    except (ValueError, TypeError):
        return {}


def assess(session, symbol, now):
    base = symbol.removesuffix('USDT')
    words = [re.escape(base)] + [re.escape(x) for x in ALIASES.get(base, [])]
    specific = re.compile(r'(?<!\w)(?:' + '|'.join(words) + r')(?!\w)', re.I)
    headlines = []
    blockers = []
    news = session.scalars(select(News).where(
        News.published_ts >= now - 6 * HOUR, News.published_ts <= now,
        News.fetched_ts <= now).order_by(News.published_ts.desc()).limit(500))
    for item in news:
        # Titles only: summary HTML, instructions and sensational sentiment are not executable signals.
        related = bool(specific.search(item.title))
        broad = bool(MARKET.search(item.title))
        if not (related or broad):
            continue
        severe = bool(CRISIS.search(item.title)) and now - item.published_ts <= 2 * HOUR
        headline = dict(title=item.title[:400], url=item.url, source=item.source,
                        published_ts=item.published_ts, fetched_ts=item.fetched_ts,
                        kind='crisis_keyword' if severe else 'context', specific=related)
        headlines.append(headline)
        if severe and (related or re.search(r'\b(crypto|bitcoin|exchange)\b', item.title, re.I)):
            blockers.append('Пауза входа: сообщение с ключевым словом риска — ' + item.title[:160])
    for event in session.scalars(select(MacroEvent).where(
            MacroEvent.ts >= now - 15 * 60_000, MacroEvent.ts <= now + 30 * 60_000)):
        if event.country in ('USD', 'US') and event.impact.lower() == 'high':
            blockers.append('Макроокно −30/+15 минут: ' + event.title)
    status = read_state(session, 'news_status')
    coverage = bool(status.get('successful_sources')) and 0 <= now - status.get('ts', 0) <= 15 * 60_000
    return dict(symbol=symbol, assessed_ts=now, headlines=headlines[:10], blockers=blockers,
                coverage_fresh=coverage,
                method='Заголовки: релевантность и словарь рисков. Направление цены и причинность не предсказываются.')
