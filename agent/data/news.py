"""News (RSS), Fear & Greed index, economic calendar. HTTP client is injected."""
import calendar
import logging
from datetime import datetime
from pathlib import Path

import feedparser
import httpx
import yaml
from sqlalchemy.orm import Session

from data.market import now_ms
from db.models import FearGreed, MacroEvent, News
from db.session import insert_ignore

log = logging.getLogger(__name__)
SOURCES = Path(__file__).resolve().parent.parent / "config" / "sources.yaml"
UA = {"User-Agent": "futures-agent/0.1 (+research)"}


def load_sources() -> dict:
    return yaml.safe_load(SOURCES.read_text(encoding="utf-8"))


class NewsCollector:
    def __init__(self, session: Session, client: httpx.Client | None = None,
                 sources: dict | None = None):
        self.session = session
        self.client = client or httpx.Client(timeout=20, headers=UA, follow_redirects=True)
        self.sources = sources or load_sources()

    def collect_rss(self) -> int:
        inserted = 0
        self.successful_sources = []
        self.failed_sources = []
        for name, url in self.sources.get("rss", {}).items():
            try:
                resp = self.client.get(url)
                resp.raise_for_status()
                parsed = feedparser.parse(resp.text)
                if not parsed.entries:
                    raise ValueError('empty or invalid RSS feed')
            except (httpx.HTTPError, ValueError) as e:
                self.failed_sources.append(name)
                log.warning("rss %s failed: %s", name, e)  # one bad feed must not stop the rest
                continue
            self.successful_sources.append(name)
            inserted += insert_ignore(self.session, News, parse_rss(name, resp.text))
        log.info("news: +%d", inserted)
        return inserted

    def collect_fear_greed(self, limit: int = 30) -> int:
        resp = self.client.get(self.sources["fear_greed"], params={"limit": limit})
        resp.raise_for_status()
        rows = [dict(ts=int(d["timestamp"]) * 1000, value=int(d["value"]),
                     classification=d["value_classification"])
                for d in resp.json().get("data", [])]
        return insert_ignore(self.session, FearGreed, rows)

    def collect_calendar(self) -> int:
        resp = self.client.get(self.sources["economic_calendar"])
        resp.raise_for_status()
        rows = []
        for e in resp.json():
            try:
                ts = int(datetime.fromisoformat(e["date"]).timestamp() * 1000)
            except (KeyError, ValueError):
                continue
            rows.append(dict(ts=ts, country=e.get("country", ""), title=e.get("title", "")[:256],
                             impact=e.get("impact", ""), forecast=e.get("forecast") or "",
                             previous=e.get("previous") or ""))
        return insert_ignore(self.session, MacroEvent, rows)


def parse_rss(source: str, text: str) -> list[dict]:
    feed = feedparser.parse(text)
    fetched, rows = now_ms(), []
    for entry in feed.entries:
        link = entry.get("link")
        if not link:
            continue
        parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        published = calendar.timegm(parsed) * 1000 if parsed else 0  # unknown time is not fresh evidence
        rows.append(dict(source=source, url=link[:1024], title=entry.get("title", ""),
                         summary=entry.get("summary", "")[:4000],
                         published_ts=published, fetched_ts=fetched))
    return rows
