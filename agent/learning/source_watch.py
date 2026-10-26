"""Bounded daily source review. Changes are observations, never downloaded executable code."""
import hashlib
import httpx
from analyst.news_risk import read_state
from analyst.copy_trading import store
from strategies.public_methods import METHODS


def review(session,now,client=None):
    prior=read_state(session,'strategy_sources').get('sources',{})
    results={};own=client is None
    client=client or httpx.Client(timeout=12,follow_redirects=True,headers={'User-Agent':'FuturesAgent-research/1.0'})
    try:
        for key,method in METHODS.items():
            try:
                with client.stream('GET',method['url']) as response:
                    response.raise_for_status();digest=hashlib.sha256();size=0
                    for block in response.iter_bytes():
                        size+=len(block)
                        if size>2_000_000: raise ValueError('source exceeds 2 MB review budget')
                        digest.update(block)
                previous=prior.get(key,{}).get('sha256');value=digest.hexdigest()
                results[key]=dict(ts=now,url=method['url'],sha256=value,
                    status='changed' if previous and previous!=value else 'observed',
                    reason='Страница изменилась; правила не меняются до отдельной проверки.' if previous and previous!=value else
                           'Источник проверен. Это не подтверждение доходности стратегии.')
            except Exception as exc:
                results[key]=dict(ts=now,url=method['url'],status='unavailable',
                    sha256=prior.get(key,{}).get('sha256'),reason=type(exc).__name__)
        store(session,'strategy_sources',dict(ts=now,sources=results))
        return results
    finally:
        if own: client.close()
