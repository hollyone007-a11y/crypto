"""Read-only HTML dashboard on 127.0.0.1. Every value is HTML-escaped: theses,
lessons and news-derived text come from an LLM and must never be trusted as markup."""
import json
import threading
from datetime import UTC, datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sqlalchemy import desc, select

from db.models import (AnalystRun, EquitySnapshot, JournalEntry, Lesson, PromotionEvent,
                       Position, RiskEvent, StrategyVersion)

CSS = """
:root{--bg:#f7f7f5;--fg:#1d1d1b;--muted:#6b6b66;--card:#fff;--line:#e4e4df;--pos:#1a7f4b;--neg:#b42318;--accent:#2457c5}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--fg:#ecece8;--muted:#9a9a93;--card:#1e1e1c;--line:#2e2e2b;--pos:#4cc38a;--neg:#ff6b5e;--accent:#7aa2ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:0 0 8px}
.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:12px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;margin-bottom:12px;overflow-x:auto}
.kpi b{display:block;font-size:22px;font-variant-numeric:tabular-nums}.pos{color:var(--pos)}.neg{color:var(--neg)}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}th,td{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
td.wrap{white-space:normal;min-width:240px}.badge{display:inline-block;padding:2px 8px;border-radius:99px;border:1px solid var(--line);font-size:12px}
svg{width:100%;height:120px}
"""


def _t(ms) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M") if ms else ""


def _n(x, d=2) -> str:
    return "" if x is None else f"{x:,.{d}f}"


def _cls(x) -> str:
    return "" if not x else ("pos" if x > 0 else "neg")


def _table(headers, rows) -> str:
    h = "".join(f"<th>{escape(x)}</th>" for x in headers)
    body = "".join("<tr>" + "".join(c if c.startswith("<td") else f"<td>{escape(c)}</td>" for c in r)
                   + "</tr>" for r in rows)
    return f"<table><thead><tr>{h}</tr></thead><tbody>{body or '<tr><td>none</td></tr>'}</tbody></table>"


def _spark(points: list[float]) -> str:
    if len(points) < 2:
        return '<p class="muted">Not enough equity history yet.</p>'
    lo, hi = min(points), max(points)
    span = (hi - lo) or 1
    xs = [i * 1000 / (len(points) - 1) for i in range(len(points))]
    ys = [110 - (p - lo) / span * 100 for p in points]
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    return (f'<svg viewBox="0 0 1000 120" preserveAspectRatio="none" role="img" aria-label="equity curve">'
            f'<polyline fill="none" stroke="var(--accent)" stroke-width="2" points="{path}"/></svg>'
            f'<p class="muted">min {_n(lo)} · max {_n(hi)}</p>')


def render_legacy(s, mode: str, equity: float | None, blockers: list[str], market_source: str = "") -> str:
    snaps = s.scalars(select(EquitySnapshot).order_by(desc(EquitySnapshot.ts)).limit(2000)).all()[::-1]
    pos = s.scalars(select(Position).where(Position.status == "open")).all()
    closed = s.scalars(select(JournalEntry).where(JournalEntry.exit_ts.is_not(None))
                       .order_by(desc(JournalEntry.exit_ts)).limit(25)).all()
    lessons = s.scalars(select(Lesson).order_by(desc(Lesson.ts)).limit(10)).all()
    run = s.scalar(select(AnalystRun).where(AnalystRun.purpose == "decide", AnalystRun.ok == True)  # noqa: E712
                   .order_by(desc(AnalystRun.ts)).limit(1))
    versions = s.scalars(select(StrategyVersion).order_by(StrategyVersion.family,
                                                          desc(StrategyVersion.version))).all()
    promo = s.scalars(select(PromotionEvent).order_by(desc(PromotionEvent.ts)).limit(15)).all()
    risk = s.scalars(select(RiskEvent).where(RiskEvent.kind != "approve")
                     .order_by(desc(RiskEvent.ts)).limit(15)).all()
    sum_r = sum(e.r or 0 for e in closed)
    pnl = sum(e.pnl or 0 for e in closed)
    thesis = ""
    if run:
        try:
            thesis = json.loads(run.response).get("thesis", "")
        except ValueError:
            thesis = ""

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="60">
<title>Futures Agent</title><style>{CSS}</style></head><body><main>
<h1>Futures Agent <span class="badge">{escape(mode.upper())}</span></h1>
<p class="muted">{escape(market_source)} · Local paper simulation · No exchange account connected · Profitability unproven</p>
<p class="muted">Trading: {escape('BLOCKED — ' + '; '.join(blockers) if blockers else 'allowed')} · refreshes every 60s</p>
<div class="grid">
 <div class="card kpi"><span class="muted">Equity</span><b>{escape(_n(equity) if equity is not None else 'n/a')}</b></div>
 <div class="card kpi"><span class="muted">Open positions</span><b>{len(pos)}</b></div>
 <div class="card kpi"><span class="muted">Last 25 trades PnL</span><b class="{_cls(pnl)}">{escape(_n(pnl))}</b></div>
 <div class="card kpi"><span class="muted">Last 25 trades sum R</span><b class="{_cls(sum_r)}">{escape(_n(sum_r))}</b></div>
</div>
<div class="card"><h2>Equity</h2>{_spark([x.equity for x in snaps])}</div>
<div class="card"><h2>Latest thesis</h2><p class="muted">{escape(_t(run.ts) if run else '')}</p><p>{escape(thesis or 'none yet')}</p></div>
<div class="card"><h2>Open positions</h2>{_table(["Symbol","Side","Qty","Entry","Stop","Take","Strategy","Opened"],
    [[p.symbol, p.side, _n(p.qty, 4), _n(p.entry), _n(p.stop), _n(p.take), p.strategy, _t(p.opened_ts)] for p in pos])}</div>
<div class="card"><h2>Closed trades</h2>{_table(["Exit","Symbol","Side","Strategy","Reason","PnL","R","MAE R","MFE R"],
    [[_t(e.exit_ts), e.symbol, e.side, e.strategy, e.exit_reason,
      f'<td class="{_cls(e.pnl)}">{escape(_n(e.pnl))}</td>', _n(e.r), _n(e.mae_r), _n(e.mfe_r)] for e in closed])}</div>
<div class="card"><h2>Lessons</h2>{_table(["When","Strategy","Outcome","Lesson"],
    [[_t(l.ts), l.strategy, l.outcome, f'<td class="wrap">{escape(l.lesson)}</td>'] for l in lessons])}</div>
<div class="card"><h2>Strategy versions</h2>{_table(["Family","Version","Kind","Status","Params / OOS"],
    [[v.family, f"v{v.version}", v.kind, v.status,
      f'<td class="wrap">{escape(json.dumps(v.params))}<br><span class="muted">{escape(json.dumps(v.metrics.get("wf", {}).get("sharpe")))}</span></td>']
     for v in versions])}</div>
<div class="card"><h2>Learning decisions</h2>{_table(["When","Version id","Step","Passed","Detail"],
    [[_t(e.ts), str(e.version_id), e.step, "yes" if e.passed else "no",
      f'<td class="wrap">{escape(json.dumps(e.detail)[:300])}</td>'] for e in promo])}</div>
<div class="card"><h2>Risk events</h2>{_table(["When","Kind","Symbol","Detail"],
    [[_t(e.ts), e.kind, e.symbol, f'<td class="wrap">{escape(json.dumps(e.detail.get("reasons", e.detail))[:300])}</td>']
     for e in risk])}</div>
</main></body></html>"""


def render(s, mode: str, equity: float | None, blockers: list[str], market_source: str = "", quotes=None, limits=None) -> str:
    from ops.presentation import render_dashboard
    return render_dashboard(s, mode, equity, blockers, market_source, quotes, limits)


def serve(page_fn, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    """page_fn() -> html. Runs in a daemon thread. Bound to localhost only."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/", "/index.html"):
                self.send_error(404)
                return
            try:
                body = page_fn().encode("utf-8")
                self.send_response(200)
            except Exception:  # never take the agent down because of a page
                body = b"dashboard error"
                self.send_response(500)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True, name="dashboard").start()
    return httpd
