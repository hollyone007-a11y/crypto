"""Out-of-sample evaluation and look-ahead detection for strategy versions.

walk_forward(): fixed params evaluated on consecutive OOS folds covering the last
`oos_frac` of history (earlier bars only serve as indicator warm-up).
forward(): same evaluation on bars that arrived AFTER a start time = paper trial."""
import numpy as np
import pandas as pd

from research.backtest import BtConfig, run_backtest
from strategies.base import Strategy
from strategies.baseline import TF_MS

DAY_MS = 86_400_000


def has_lookahead(strategy: Strategy, bars: pd.DataFrame, funding: pd.DataFrame,
                  cuts: int = 4) -> bool:
    """True if any signal changes when future bars are removed."""
    full = strategy.signals(bars, funding)
    n = len(bars)
    for k in range(1, cuts + 1):
        cut = int(n * k / (cuts + 1))
        if cut < 50:
            continue
        end_ts = bars["ts"].iloc[cut]
        part = strategy.signals(bars.iloc[:cut + 1].copy(),
                                funding[funding["ts"] <= end_ts + TF_MS[strategy.timeframe]])
        if not np.array_equal(part['side'].to_numpy(), full['side'].iloc[:cut + 1].to_numpy()):
            return True
        active = part['side'].to_numpy() != 0
        for column in ('stop','take'):
            if not np.allclose(part[column].to_numpy(float)[active], full[column].iloc[:cut+1].to_numpy(float)[active], equal_nan=True):
                return True
    return False


def _window_stats(trades: pd.DataFrame, equity: pd.Series) -> dict:
    if equity.empty:
        return {"trades": 0, "sharpe": 0.0, "max_dd_pct": 0.0, "expectancy_r": 0.0,
                "sum_r": 0.0, "worst_day_pct": 0.0}
    daily = pd.Series(equity.to_numpy(), index=pd.to_datetime(equity.index, unit="ms", utc=True)) \
        .resample("1D").last().dropna()
    daily_with_initial = pd.concat([pd.Series([10_000.0], index=[daily.index[0]-pd.Timedelta(days=1)]), daily])
    rets = daily_with_initial.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * np.sqrt(365)) if len(rets) > 1 and rets.std() > 0 else 0.0
    peaks = equity.cummax().clip(lower=10_000.0)
    dd = float((equity / peaks - 1).min())
    return {
        "trades": int(len(trades)),
        "sharpe": round(sharpe, 3),
        "max_dd_pct": round(100 * dd, 2),
        "expectancy_r": round(float(trades["r"].mean()), 3) if len(trades) else 0.0,
        "sum_r": round(float(trades["r"].sum()), 3) if len(trades) else 0.0,
        "worst_day_pct": round(100 * float(rets.min()), 2) if len(rets) else 0.0,
    }


def evaluate_window(strategy: Strategy, bars: pd.DataFrame, funding: pd.DataFrame,
                    start_ts: int, end_ts: int | None = None) -> dict:
    """Run on all bars (warm-up included) but score only [start_ts, end_ts)."""
    end_ts = end_ts if end_ts is not None else int(bars["ts"].iloc[-1]) + 1
    bars = bars[bars['ts'] < end_ts].copy()
    funding = funding[funding['ts'] < end_ts].copy() if funding is not None and not funding.empty else funding
    sig = strategy.signals(bars, funding)
    sig.loc[(bars["ts"] < start_ts) | (bars["ts"] >= end_ts), "side"] = 0  # no entries outside
    res = run_backtest(bars, sig, funding, BtConfig(bar_ms=TF_MS[strategy.timeframe]),
                       strategy.max_hold_bars)
    eq = res.equity[(res.equity.index >= start_ts) & (res.equity.index < end_ts)]
    tr = res.trades[res.trades["entry_ts"] >= start_ts]
    return _window_stats(tr, eq)


def walk_forward(strategy: Strategy, data: dict[str, tuple[pd.DataFrame, pd.DataFrame]],
                 folds: int = 4, oos_frac: float = 0.5) -> dict:
    """data: symbol -> (bars, funding). Aggregates per-fold stats across symbols."""
    per_fold = []
    per_symbol_folds={symbol:[] for symbol in data}
    for f in range(folds):
        fold = {"trades": 0, "sum_r": 0.0, "sharpe": [], "max_dd_pct": []}
        for symbol,(bars, funding) in data.items():
            n = len(bars)
            oos_start = int(n * (1 - oos_frac))
            a = oos_start + (n - oos_start) * f // folds
            b = oos_start + (n - oos_start) * (f + 1) // folds
            if b <= a:
                continue
            end_ts = int(bars["ts"].iloc[b - 1]) + 1
            s = evaluate_window(strategy, bars.iloc[:b], funding, int(bars["ts"].iloc[a]), end_ts)
            per_symbol_folds[symbol].append(s)
            fold["trades"] += s["trades"]
            fold["sum_r"] += s["sum_r"]
            fold["sharpe"].append(s["sharpe"])
            fold["max_dd_pct"].append(s["max_dd_pct"])
        per_fold.append({"trades": fold["trades"], "sum_r": round(fold["sum_r"], 3),
                         "sharpe": round(float(np.mean(fold["sharpe"])) if fold["sharpe"] else 0.0, 3),
                         "max_dd_pct": min(fold["max_dd_pct"]) if fold["max_dd_pct"] else 0.0})
    trades = sum(p["trades"] for p in per_fold)
    result = {
        "folds": per_fold,
        "trades": trades,
        "sharpe": round(float(np.mean([p["sharpe"] for p in per_fold])), 3) if per_fold else 0.0,
        "max_dd_pct": min((p["max_dd_pct"] for p in per_fold), default=0.0),
        "expectancy_r": round(sum(p["sum_r"] for p in per_fold) / trades, 3) if trades else 0.0,
    }
    result['per_symbol']={}
    for symbol,parts in per_symbol_folds.items():
        count=sum(p['trades'] for p in parts)
        result['per_symbol'][symbol]={'trades':count,
            'sharpe':round(float(np.mean([p['sharpe'] for p in parts])),3) if parts else 0.,
            'max_dd_pct':min((p['max_dd_pct'] for p in parts),default=0.),
            'expectancy_r':round(sum(p['sum_r'] for p in parts)/count,3) if count else 0.}
    return result


def beats(cand: dict, prod: dict, min_trades: int = 20) -> tuple[bool, list[str]]:
    """Promotion rule: better Sharpe AND not-worse max DD AND enough trades AND
    better Sharpe in at least half of the folds."""
    why = []
    if any(not np.isfinite(prod.get(key,float('nan'))) for key in ('sharpe','max_dd_pct','expectancy_r')):
        why.append('non-finite benchmark statistics')
    if cand.get('dataset_id') or prod.get('dataset_id'):
        for key in ('dataset_id','cutoff_ts','symbols','cost_model'):
            if cand.get(key)!=prod.get(key):
                why.append('candidate and benchmark use different '+key)
    if cand["trades"] < min_trades:
        why.append(f"too few OOS trades ({cand['trades']} < {min_trades})")
    if not cand.get('folds') or len(cand.get('folds',[])) != len(prod.get('folds',[])):
        why.append('missing or mismatched validation folds')
    if any(not np.isfinite(cand.get(key,float('nan'))) for key in ('sharpe','max_dd_pct','expectancy_r')):
        why.append('non-finite candidate statistics')
    if cand.get('expectancy_r',0) <= 0 or cand.get('sharpe',0) <= 0:
        why.append('candidate must have positive net expectancy and Sharpe')
    if cand["sharpe"] <= prod["sharpe"]:
        why.append(f"Sharpe {cand['sharpe']} <= prod {prod['sharpe']}")
    if cand["max_dd_pct"] < prod["max_dd_pct"]:
        why.append(f"maxDD {cand['max_dd_pct']}% worse than prod {prod['max_dd_pct']}%")
    wins = sum(c["sharpe"] > p["sharpe"] for c, p in zip(cand["folds"], prod["folds"]))
    if wins * 2 < len(cand["folds"]):
        why.append(f"better in only {wins}/{len(cand['folds'])} folds")
    return not why, why
