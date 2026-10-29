"""Entry point:  python -m ops.run   (PAPER only)

Starts the scheduler, Telegram polling and the dashboard on http://127.0.0.1:8765."""
import logging
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from datetime import UTC, datetime

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler

from config.settings import ROOT, get_settings
from ops import dashboard
from ops.app import App, LiveModeRefused

log = logging.getLogger("agent")


def setup_logging() -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    fh = RotatingFileHandler(ROOT / "logs" / "agent.log", maxBytes=5_000_000, backupCount=5,
                             encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers[:] = [fh, sh]
    for noisy in ("httpx", "httpcore", "apscheduler.executors.default", "ccxt"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _safe(fn, *a, **k):
    try:
        return fn(*a, **k)
    except Exception:
        log.exception("job %s failed", getattr(fn, "__name__", fn))


def build_scheduler(app: App) -> BlockingScheduler:
    # ONE worker: trading jobs never run concurrently against the paper account
    sched = BlockingScheduler(executors={"default": ThreadPoolExecutor(1),
                                        "research": ThreadPoolExecutor(1),
                                        "market": ThreadPoolExecutor(1),
                                        "context": ThreadPoolExecutor(1),
                                        "scanner": ThreadPoolExecutor(1),
                                        "priority": ThreadPoolExecutor(1),
                                        "observations": ThreadPoolExecutor(1),
                                        "sources": ThreadPoolExecutor(1),
                                        "publish": ThreadPoolExecutor(1)}, timezone="UTC",
                              job_defaults={"coalesce": True, "max_instances": 1,
                                            "misfire_grace_time": 120})

    def add(fn, trigger, kwargs=None, **kw):  # every job is wrapped: one failure never kills the loop
        sched.add_job(_safe, trigger, args=[fn], kwargs=kwargs or {}, **kw)
    add(app.tick, "interval", seconds=5, id="tick")
    add(app.collect_market, "cron", minute=1, id="market", executor='market')
    add(app.analyst_cycle, "cron", minute=3, id="analyst")
    add(app.snapshot_books, "interval", minutes=5, id="books", executor='market')
    add(app.collect_news, "interval", minutes=5, id="news", executor='context',
        next_run_time=datetime.now(UTC))
    add(app.collect_copy_sources, "interval", seconds=60, id="copy_sources", executor='context',
        next_run_time=datetime.now(UTC))
    add(app.copy_cycle, "interval", seconds=15, id="copy_demo")
    add(app.multiframe_collect, 'interval', seconds=60, id='multiframe_collect',executor='scanner',
        next_run_time=datetime.now(UTC))
    add(app.multiframe_execute, 'interval', seconds=15, id='multiframe_execute')
    add(app.priority_research,'interval',seconds=60,id='priority_research',executor='priority',next_run_time=datetime.now(UTC))
    add(app.signal_learning_cycle,'interval',seconds=15,id='signal_learning',executor='observations',next_run_time=datetime.now(UTC))
    add(app.review_strategy_sources,'interval',hours=24,id='strategy_sources',executor='sources',
        next_run_time=datetime.now(UTC))
    add(app.collect_news, "cron", hour="*/6", minute=20, id="news_full",
        kwargs={"fear_greed": True, "calendar": True}, executor='context',
        next_run_time=datetime.now(UTC))
    add(app.review_lessons, "interval", minutes=15, id="lessons")
    add(app.advance_candidates, "cron", hour=0, minute=15, id="advance", executor='research')
    add(app.daily_report, "cron", hour=20, minute=0, id="daily_report")
    add(app.weekly, "cron", day_of_week="sun", hour=18, minute=0, id="weekly")
    add(app.research_cycle, 'interval', minutes=10, id='research', executor='research',
        next_run_time=datetime.now(UTC))
    add(app.publish_dashboard, 'interval', seconds=15, id='cloud', executor='publish',
        next_run_time=datetime.now(UTC))
    add(app.bootstrap_market,'date',run_date=datetime.now(UTC),id='bootstrap_market',executor='market')
    add(app.bootstrap_market,'interval',hours=6,id='universe',executor='market')
    return sched


def main() -> int:
    setup_logging()
    try:
        app = App(get_settings())
    except LiveModeRefused as e:
        log.error(str(e))
        return 2
    _safe(app.startup)
    _safe(app.tick)
    _safe(app.review_lessons)
    _safe(app.analyst_cycle)
    dashboard.serve(lambda: app.page(),snapshot_fn=app.live_snapshot)
    log.info("dashboard on http://127.0.0.1:8765")

    def poll():
        while True:
            if not app.tg.enabled:
                time.sleep(60)
                continue
            _safe(app.tg.poll_once, app.handle_command)
            time.sleep(1)  # long-poll already waits; this only guards against fast error loops
    threading.Thread(target=poll, daemon=True, name="telegram").start()

    sched = build_scheduler(app)
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
