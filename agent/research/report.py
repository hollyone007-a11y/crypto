"""CLI: python -m research.report  -> reports/backtest_<date>.md

Runs every baseline strategy on every symbol over the stored history, with fixed
(untuned) parameters. Also splits the period in half to show stability."""
import json
from datetime import UTC, datetime
from pathlib import Path

from config.settings import ROOT, get_settings
from db.session import init_db, make_engine, make_session_factory
from research.backtest import BtConfig, metrics, run_backtest
from research.data_loader import load_funding, load_ohlcv
from strategies.baseline import ALL, TF_MS

COLS = ["trades", "total_return_pct", "cagr_pct", "sharpe", "max_dd_pct", "win_rate_pct",
        "profit_factor", "expectancy_r", "t_stat", "gross", "fees", "funding"]


def evaluate(strategy, bars, funding):
    sig = strategy.signals(bars, funding)
    cfg = BtConfig(bar_ms=TF_MS[strategy.timeframe])
    return run_backtest(bars, sig, funding, cfg, strategy.max_hold_bars)


def run() -> Path:
    s = get_settings()
    engine = make_engine(s.database_url)
    init_db(engine)
    rows = []
    with make_session_factory(engine)() as session:
        for cls in ALL:
            strat = cls()
            for sym in s.symbol_list:
                bars = load_ohlcv(session, sym, strat.timeframe)
                funding = load_funding(session, sym)
                if bars.empty:
                    continue
                res = evaluate(strat, bars, funding)
                m = metrics(res)
                # stability: same run, metrics per half of the period
                mid = bars["ts"].iloc[len(bars) // 2]
                halves = []
                for part in (res.trades[res.trades["entry_ts"] < mid],
                             res.trades[res.trades["entry_ts"] >= mid]):
                    halves.append(round(float(part["r"].mean()), 3) if len(part) else None)
                rows.append({"strategy": strat.name, "symbol": sym, **m,
                             "exp_r_h1": halves[0], "exp_r_h2": halves[1],
                             "from": _d(bars["ts"].iloc[0]), "to": _d(bars["ts"].iloc[-1])})

    out_dir = ROOT / "reports"
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    (out_dir / f"backtest_{stamp}.json").write_text(json.dumps(rows, indent=2))
    path = out_dir / f"backtest_{stamp}.md"
    path.write_text(render(rows), encoding="utf-8")
    return path


def render(rows: list[dict]) -> str:
    head = ["strategy", "symbol"] + COLS + ["exp_r_h1", "exp_r_h2"]
    lines = ["# Baseline backtest", "",
             f"Period: {rows[0]['from']} → {rows[0]['to']}. Equity 10 000 USDT, risk 0.5%/trade, "
             "max 3x, taker 0.055%/side, slippage 2 bps/side, real funding. Entry on next bar open; "
             "stop wins if stop & take hit in the same bar. Params NOT tuned.", "",
             "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in rows:
        lines.append("| " + " | ".join(str(r.get(k)) for k in head) + " |")
    pos = [r for r in rows if r["trades"] >= 30 and r["expectancy_r"] > 0]
    lines += ["", "## Verdict", ""]
    if not pos:
        lines.append("**No strategy/symbol has positive expectancy after costs with ≥30 trades.** "
                     "Parameters were not tuned to rescue them (that would be overfitting).")
    else:
        for r in pos:
            stable = (r["exp_r_h1"] or 0) > 0 and (r["exp_r_h2"] or 0) > 0
            signif = r["t_stat"] >= 2
            lines.append(f"- {r['strategy']} {r['symbol']}: +{r['expectancy_r']}R/trade, "
                         f"t={r['t_stat']}, Sharpe {r['sharpe']}, maxDD {r['max_dd_pct']}% — "
                         f"{'positive in both halves' if stable else 'NOT stable across halves'}, "
                         f"{'statistically significant' if signif else 'NOT significant (t<2)'}")
    return "\n".join(lines) + "\n"


def _d(ms) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, UTC).strftime("%Y-%m-%d")


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print(run().read_text(encoding="utf-8"))
