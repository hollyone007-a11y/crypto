import httpx
from sqlalchemy import select

from data.news import NewsCollector, load_sources, parse_rss
from db.models import FearGreed, MacroEvent, News

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>BTC breaks 100k</title><link>https://ex.com/a</link>
<pubDate>Tue, 20 Oct 2026 10:00:00 GMT</pubDate><description>desc</description></item>
<item><title>No link</title></item>
<item><title>ETH ETF</title><link>https://ex.com/b</link></item>
</channel></rss>"""

SOURCES = {"rss": {"good": "https://feeds/good", "bad": "https://feeds/bad"},
           "fear_greed": "https://fng/", "economic_calendar": "https://cal/"}


def handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url.startswith("https://feeds/good"):
        return httpx.Response(200, text=RSS)
    if url.startswith("https://feeds/bad"):
        return httpx.Response(503)
    if url.startswith("https://fng/"):
        assert request.url.params["limit"] == "2"
        return httpx.Response(200, json={"data": [
            {"value": "25", "value_classification": "Extreme Fear", "timestamp": "1760918400"},
            {"value": "40", "value_classification": "Fear", "timestamp": "1760832000"}]})
    if url.startswith("https://cal/"):
        return httpx.Response(200, json=[
            {"title": "CPI m/m", "country": "USD", "date": "2026-10-20T08:30:00-04:00",
             "impact": "High", "forecast": "0.3%", "previous": None},
            {"title": "broken", "country": "EUR", "date": "not-a-date"}])
    return httpx.Response(404)


def collector(session):
    return NewsCollector(session, httpx.Client(transport=httpx.MockTransport(handler)), SOURCES)


def test_parse_rss():
    rows = parse_rss("x", RSS)
    assert [r["url"] for r in rows] == ["https://ex.com/a", "https://ex.com/b"]
    assert rows[0]["published_ts"] == 1_792_490_400_000


def test_rss_bad_feed_does_not_stop_others_and_dedups(session):
    c = collector(session)
    assert c.collect_rss() == 2
    assert c.collect_rss() == 0
    assert {n.source for n in session.scalars(select(News))} == {"good"}


def test_fear_greed_and_calendar(session):
    c = collector(session)
    assert c.collect_fear_greed(limit=2) == 2
    assert session.scalars(select(FearGreed).order_by(FearGreed.ts)).first().value == 40
    assert c.collect_calendar() == 1
    ev = session.scalars(select(MacroEvent)).one()
    assert ev.ts == 1_792_499_400_000 and ev.impact == "High" and ev.previous == ""


def test_sources_file_is_valid():
    s = load_sources()
    assert s["rss"] and s["fear_greed"] and s["economic_calendar"]
