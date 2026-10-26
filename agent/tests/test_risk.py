"""Risk gate tests: every way to get around a limit must be rejected."""
import dataclasses
import math
import shutil

import pytest
from sqlalchemy import select

from db.models import RiskEvent
from risk.limits import RISK_FILE, RiskConfigError, load_limits
from risk.manager import AccountSnapshot, OpenPos, OrderRequest, RiskManager, RiskViolation

DAY = 86_400_000
T0 = 1_792_454_400_000  # 2026-10-20 00:00 UTC


class Clock:
    def __init__(self, t=T0 + 3_600_000):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def env(session, tmp_path):
    cfg = tmp_path / "risk.yaml"
    shutil.copy(RISK_FILE, cfg)
    alerts, clock = [], Clock()
    rm = RiskManager(session, load_limits(cfg), kill_switch=tmp_path / "STOP",
                     on_alert=alerts.append, clock=clock)
    return rm, alerts, clock, cfg


ACC = AccountSnapshot(10_000.0)
LONG = OrderRequest("BTCUSDT", "long", entry=100.0, stop=98.0, take=106.0, strategy="t")


def reject(rm, req=LONG, acc=ACC, needle=""):
    d = rm.evaluate(req, acc)
    assert not d.approved and d.approval is None
    assert any(needle in r for r in d.reasons), d.reasons
    return d


# ---------- limits file ----------

def test_real_risk_yaml_matches_spec():
    L = load_limits()
    assert (L.max_risk_per_trade_pct, L.max_leverage, L.max_open_positions,
            L.daily_loss_limit_pct, L.max_drawdown_pct, L.require_stop_loss) == (0.5, 10.0, 3, 2.0, 10.0, True)


def test_limits_object_is_immutable():
    L = load_limits()
    with pytest.raises(dataclasses.FrozenInstanceError):
        L.max_leverage = 100


@pytest.mark.parametrize("bad", [
    "max_leverage: 3\n",                                                    # missing keys
    "max_risk_per_trade_pct: 0.5\nmax_leverage: 3\nmax_open_positions: 3\ndaily_loss_limit_pct: 2\n"
    "max_drawdown_pct: 10\nrequire_stop_loss: false\n",                    # stop not required
    "max_risk_per_trade_pct: -1\nmax_leverage: 3\nmax_open_positions: 3\ndaily_loss_limit_pct: 2\n"
    "max_drawdown_pct: 10\nrequire_stop_loss: true\n",                     # negative
    "max_risk_per_trade_pct: .nan\nmax_leverage: 3\nmax_open_positions: 3\ndaily_loss_limit_pct: 2\n"
    "max_drawdown_pct: 10\nrequire_stop_loss: true\n",                     # NaN
    "max_risk_per_trade_pct: 150\nmax_leverage: 3\nmax_open_positions: 3\ndaily_loss_limit_pct: 2\n"
    "max_drawdown_pct: 10\nrequire_stop_loss: true\n",                     # >= 100%
    "max_risk_per_trade_pct: 0.5\nmax_leverage: 3\nmax_open_positions: 2.5\ndaily_loss_limit_pct: 2\n"
    "max_drawdown_pct: 10\nrequire_stop_loss: true\n",                     # fractional count
    "max_risk_per_trade_pct: 0.5\nmax_leverage: true\nmax_open_positions: 3\ndaily_loss_limit_pct: 2\n"
    "max_drawdown_pct: 10\nrequire_stop_loss: true\n",                     # bool as number
    "max_risk_per_trade_pct: 0.5\nmax_leverage: 3\nmax_open_positions: 3\ndaily_loss_limit_pct: 2\n"
    "max_drawdown_pct: 10\nrequire_stop_loss: true\nllm_override: true\n",  # unknown key
])
def test_bad_risk_yaml_refused(tmp_path, bad):
    p = tmp_path / "r.yaml"
    p.write_text(bad)
    with pytest.raises(RiskConfigError):
        load_limits(p)


# ---------- happy path & sizing ----------

def test_approves_and_sizes_by_risk(env):
    rm, *_ = env
    d = rm.evaluate(LONG, ACC)
    assert d.approved and d.qty == pytest.approx(10_000 * 0.005 / 2.0)   # 25
    assert d.qty * 2.0 == pytest.approx(50)                              # exactly 0.5% at risk
    assert rm.s.scalar(select(RiskEvent).where(RiskEvent.kind == "approve")) is not None


def test_size_capped_by_leverage_including_open_positions(env):
    rm, *_ = env
    acc = AccountSnapshot(10_000, (OpenPos("ETHUSDT", "long", 100, 200.0),))  # 20k notional used
    d = rm.evaluate(OrderRequest("BTCUSDT", "long", 100.0, 99.99), acc)
    assert d.approved and d.qty == pytest.approx(800)  # (100k - 20k) / 100
    full = AccountSnapshot(10_000, (OpenPos("ETHUSDT", "long", 500, 200.0),))
    reject(rm, OrderRequest("BTCUSDT", "long", 100.0, 99.0), full, "leverage headroom")


def test_smaller_requested_qty_is_honoured(env):
    rm, *_ = env
    d = rm.evaluate(dataclasses.replace(LONG, requested_qty=5.0), ACC)
    assert d.approved and d.qty == 5.0


# ---------- bypass attempts (e.g. from an LLM proposal) ----------

@pytest.mark.parametrize("req,needle", [
    (dataclasses.replace(LONG, stop=None), "stop-loss is mandatory"),
    (dataclasses.replace(LONG, stop=101.0), "wrong side"),
    (dataclasses.replace(LONG, side="short", take=90.0), "wrong side"),
    (dataclasses.replace(LONG, take=99.0), "take-profit on wrong side"),
    (dataclasses.replace(LONG, stop=0.0), "invalid stop"),
    (dataclasses.replace(LONG, stop=math.nan), "invalid stop"),
    (dataclasses.replace(LONG, entry=math.inf), "invalid entry"),
    (dataclasses.replace(LONG, side="buy_everything"), "invalid side"),
    (dataclasses.replace(LONG, requested_qty=26.0), "exceeds allowed"),        # > 0.5% risk
    (dataclasses.replace(LONG, requested_qty=1e12), "exceeds allowed"),
    (dataclasses.replace(LONG, requested_qty=-5.0), "invalid requested qty"),
    (dataclasses.replace(LONG, requested_leverage=11.0), "requested leverage"),
    (dataclasses.replace(LONG, requested_leverage=math.nan), "requested leverage"),
])
def test_bypass_attempts_rejected(env, req, needle):
    rm, *_ = env
    reject(rm, req, ACC, needle)
    ev = rm.s.scalars(select(RiskEvent).where(RiskEvent.kind == "reject")).all()
    assert ev and ev[-1].detail["reasons"]  # every rejection is logged


def test_max_open_positions_and_duplicate_symbol(env):
    rm, *_ = env
    three = AccountSnapshot(10_000, tuple(OpenPos(s, "long", 1, 1.0) for s in ("A", "B", "C")))
    reject(rm, LONG, three, "max open positions")
    one = AccountSnapshot(10_000, (OpenPos("BTCUSDT", "short", 1, 100.0),))
    reject(rm, LONG, one, "already open")


def test_non_positive_equity_rejected(env):
    rm, *_ = env
    reject(rm, LONG, AccountSnapshot(0.0), "non-positive equity")


# ---------- approvals ----------

def test_approval_single_use_untamperable_and_expiring(env):
    rm, _, clock, _ = env
    a = rm.evaluate(LONG, ACC).approval
    with pytest.raises(RiskViolation, match="tampered"):
        rm.consume(dataclasses.replace(a, qty=a.qty * 10))
    a = rm.evaluate(LONG, ACC).approval
    assert rm.consume(a) == a
    with pytest.raises(RiskViolation, match="already-used"):
        rm.consume(a)
    with pytest.raises(RiskViolation, match="unknown"):
        rm.consume(object())
    a = rm.evaluate(LONG, ACC).approval
    clock.t += 61_000
    with pytest.raises(RiskViolation, match="expired"):
        rm.consume(a)


def test_approval_void_if_kill_switch_appears_before_fill(env, tmp_path):
    rm, *_ = env
    a = rm.evaluate(LONG, ACC).approval
    (tmp_path / "STOP").write_text("")
    with pytest.raises(RiskViolation, match="kill switch"):
        rm.consume(a)


# ---------- breakers ----------

def test_kill_switch_blocks(env, tmp_path):
    rm, *_ = env
    (tmp_path / "STOP").write_text("")
    reject(rm, needle="kill switch")


def test_daily_loss_locks_until_next_utc_day(env):
    rm, alerts, clock, _ = env
    assert rm.update_equity(10_000) == {"daily_lock": False, "halted": False}
    assert rm.update_equity(9_801)["daily_lock"] is False      # -1.99%
    assert rm.update_equity(9_800)["daily_lock"] is True       # -2.00%
    assert any("Daily loss" in a for a in alerts)
    reject(rm, needle="daily loss")
    rm.update_equity(10_500)                                   # recovering same day doesn't unlock
    reject(rm, needle="daily loss")
    clock.t += DAY
    rm.update_equity(9_800)                                    # new day: new baseline
    assert rm.evaluate(LONG, ACC).approved


def test_max_drawdown_halts_permanently(env):
    rm, alerts, clock, _ = env
    rm.update_equity(10_000)
    clock.t += DAY
    rm.update_equity(11_000)                                   # new peak
    for _ in range(6):                                         # bleed 1.9%/day, under daily limit
        clock.t += DAY
        rm.update_equity(rm_eq := float(rm._get("day_start_equity")) * 0.981)
    assert rm_eq <= 11_000 * 0.9 and rm.halted
    assert any("FULL HALT" in a for a in alerts)
    clock.t += 30 * DAY
    rm.update_equity(20_000)
    reject(rm, needle="halted")                                # only a human can clear it


def test_risk_yaml_modified_at_runtime_fails_closed(env):
    rm, alerts, _, cfg = env
    assert rm.evaluate(LONG, ACC).approved
    cfg.write_text(cfg.read_text().replace("max_leverage: 10.0", "max_leverage: 50"))
    reject(rm, needle="integrity")
    assert rm.halted and any("risk.yaml" in a for a in alerts)


def test_risk_yaml_deleted_at_runtime_fails_closed(env):
    rm, _, _, cfg = env
    cfg.unlink()
    reject(rm, needle="integrity")


def test_admin_reset_halt(env, monkeypatch, capsys):
    from risk import admin
    rm, *_ = env
    rm._halt("test")
    engine = rm.s.get_bind()
    monkeypatch.setattr(admin, "make_engine", lambda url: engine)
    admin.main(["reset-halt"])
    rm.s.expire_all()
    assert not rm.halted and "halt cleared" in capsys.readouterr().out
