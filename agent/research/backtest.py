"""Bar-by-bar backtester for one strategy on one symbol.

Rules (conservative by design):
- signal on bar i close -> market entry at bar i+1 OPEN, plus slippage
- stop/take checked intrabar on high/low; if both touched in one bar -> STOP wins
- gaps through stop fill at the open (worse price)
- taker fee on both sides, funding charged on every settlement while in position
- size = equity * risk_pct / stop distance, capped by max_leverage
"""
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class Costs:
    taker_fee: float = 0.00055   # Bybit non-VIP perp taker
    slippage_bps: float = 2.0    # per side


@dataclass
class BtConfig:
    initial_equity: float = 10_000.0
    risk_pct: float = 0.5        # matches config/risk.yaml
    max_leverage: float = 3.0
    costs: Costs = field(default_factory=Costs)
    bar_ms: int = 3_600_000


@dataclass
class BtResult:
    trades: pd.DataFrame
    equity: pd.Series  # indexed by bar ts (ms), marked at close
    config: BtConfig


def run_backtest(df: pd.DataFrame, signals: pd.DataFrame, funding: pd.DataFrame | None,
                 cfg: BtConfig, max_hold_bars: int = 0) -> BtResult:
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    ts = df["ts"].to_numpy(np.int64)
    side_sig = signals["side"].to_numpy(int)
    stop_sig = signals["stop"].to_numpy(float)
    take_sig = signals["take"].to_numpy(float)
    f_ts = funding["ts"].to_numpy(np.int64) if funding is not None and len(funding) else np.array([], np.int64)
    f_rate = funding["rate"].to_numpy(float) if len(f_ts) else np.array([])
    slip = cfg.costs.slippage_bps / 10_000
    fee = cfg.costs.taker_fee

    equity = cfg.initial_equity
    pos = None
    trades, curve = [], np.empty(len(df))

    def close_pos(i, price, reason):
        nonlocal equity, pos
        px = price * (1 - slip) if pos["side"] > 0 else price * (1 + slip)
        gross = pos["side"] * (px - pos["entry"]) * pos["qty"]
        exit_fee = fee * px * pos["qty"]
        net = gross - pos["fees"] - exit_fee + pos["funding"]
        equity += gross - exit_fee  # entry fee + funding already booked
        trades.append(dict(
            entry_ts=pos["ts"], exit_ts=int(ts[i]), side="long" if pos["side"] > 0 else "short",
            entry=pos["entry"], exit=px, qty=pos["qty"], stop=pos["stop"], take=pos["take"],
            gross=gross, fees=pos["fees"] + exit_fee, funding=pos["funding"], pnl=net,
            r=net / pos["risk"], mae_r=pos["mae"] / pos["risk"], mfe_r=pos["mfe"] / pos["risk"],
            bars=i - pos["i"] + 1, reason=reason))
        pos = None

    for i in range(len(df)):
        # 1) entry from previous bar's signal
        if pos is None and i > 0 and side_sig[i - 1] != 0:
            s = side_sig[i - 1]
            ref = c[i - 1]
            stop_d, take_d = abs(ref - stop_sig[i - 1]), abs(take_sig[i - 1] - ref)
            entry = o[i] * (1 + slip) if s > 0 else o[i] * (1 - slip)
            if stop_d > 0 and take_d > 0 and equity > 0:
                risk = equity * cfg.risk_pct / 100
                qty = min(risk / stop_d, equity * cfg.max_leverage / entry)
                entry_fee = fee * entry * qty
                equity -= entry_fee
                pos = dict(side=s, entry=entry, qty=qty, i=i, ts=int(ts[i]),
                           stop=entry - s * stop_d, take=entry + s * take_d,
                           risk=stop_d * qty, fees=entry_fee, funding=0.0, mae=0.0, mfe=0.0,
                           last_f=int(ts[i]))

        if pos is not None:
            # 2) funding settlements inside this bar
            bar_end = int(ts[i]) + cfg.bar_ms
            if len(f_ts):
                lo_i, hi_i = np.searchsorted(f_ts, [pos["last_f"], bar_end], side="right")
                for k in range(lo_i, hi_i):
                    if f_ts[k] > pos["ts"]:
                        amt = -pos["side"] * f_rate[k] * pos["qty"] * c[i]
                        pos["funding"] += amt
                        equity += amt
                pos["last_f"] = bar_end
            # 3) excursions and exits
            s = pos["side"]
            adverse = (pos["entry"] - l[i]) if s > 0 else (h[i] - pos["entry"])
            favor = (h[i] - pos["entry"]) if s > 0 else (pos["entry"] - l[i])
            pos["mae"] = max(pos["mae"], adverse * pos["qty"])
            pos["mfe"] = max(pos["mfe"], favor * pos["qty"])
            hit_stop = l[i] <= pos["stop"] if s > 0 else h[i] >= pos["stop"]
            hit_take = h[i] >= pos["take"] if s > 0 else l[i] <= pos["take"]
            if hit_stop:
                gap = o[i] < pos["stop"] if s > 0 else o[i] > pos["stop"]
                close_pos(i, o[i] if gap else pos["stop"], "stop")
            elif hit_take:
                gap = o[i] > pos["take"] if s > 0 else o[i] < pos["take"]
                close_pos(i, o[i] if gap else pos["take"], "take")
            elif max_hold_bars and i - pos["i"] + 1 >= max_hold_bars:
                close_pos(i, c[i], "time")
            elif i == len(df) - 1:
                close_pos(i, c[i], "end")

        mtm = 0.0
        if pos is not None:
            mtm = pos["side"] * (c[i] - pos["entry"]) * pos["qty"]
        curve[i] = equity + mtm

    cols = ["entry_ts", "exit_ts", "side", "entry", "exit", "qty", "stop", "take", "gross",
            "fees", "funding", "pnl", "r", "mae_r", "mfe_r", "bars", "reason"]
    return BtResult(pd.DataFrame(trades, columns=cols), pd.Series(curve, index=ts), cfg)


def metrics(res: BtResult) -> dict:
    t, eq = res.trades, res.equity
    daily = pd.Series(eq.to_numpy(), index=pd.to_datetime(eq.index, unit="ms", utc=True)) \
        .resample("1D").last().dropna()
    rets = daily.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * np.sqrt(365)) if len(rets) > 1 and rets.std() > 0 else 0.0
    dd = float((daily / daily.cummax() - 1).min()) if len(daily) else 0.0
    years = max((eq.index[-1] - eq.index[0]) / (365 * 86_400_000), 1e-9) if len(eq) else 1
    final = float(eq.iloc[-1]) if len(eq) else res.config.initial_equity
    total = final / res.config.initial_equity - 1
    wins = t[t["pnl"] > 0]
    losses = t[t["pnl"] <= 0]
    pf = float(wins["pnl"].sum() / -losses["pnl"].sum()) if len(losses) and losses["pnl"].sum() < 0 else float("inf")
    return {
        "trades": int(len(t)),
        "total_return_pct": round(100 * total, 2),
        "cagr_pct": round(100 * ((1 + total) ** (1 / years) - 1), 2) if total > -1 else -100.0,
        "sharpe": round(sharpe, 2),
        "max_dd_pct": round(100 * dd, 2),
        "win_rate_pct": round(100 * len(wins) / len(t), 1) if len(t) else 0.0,
        "profit_factor": round(pf, 2) if np.isfinite(pf) else None,
        "expectancy_r": round(float(t["r"].mean()), 3) if len(t) else 0.0,
        # t-stat of mean R: |t| < 2 means the edge is indistinguishable from noise
        "t_stat": round(float(t["r"].mean() / t["r"].std() * np.sqrt(len(t))), 2)
        if len(t) > 2 and t["r"].std() > 0 else 0.0,
        "avg_pnl": round(float(t["pnl"].mean()), 2) if len(t) else 0.0,
        "fees": round(float(t["fees"].sum()), 2),
        "funding": round(float(t["funding"].sum()), 2),
        "gross": round(float(t["gross"].sum()), 2),
    }
