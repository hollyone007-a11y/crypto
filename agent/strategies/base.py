"""Strategy contract. A strategy turns a bar frame into per-bar signals.

Signal semantics: computed on the CLOSE of bar i; the executor may only act on
bar i+1 (no look-ahead). stop/take are absolute prices relative to `entry`.
"""
from dataclasses import asdict, dataclass, field
from typing import Literal

import pandas as pd


@dataclass(frozen=True)
class Signal:
    side: Literal["long", "short"]
    entry: float
    stop: float
    take: float
    confidence: float
    rationale: str

    def __post_init__(self):
        if self.side == "long" and not self.stop < self.entry < self.take:
            raise ValueError(f"long needs stop < entry < take: {self}")
        if self.side == "short" and not self.take < self.entry < self.stop:
            raise ValueError(f"short needs take < entry < stop: {self}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Strategy:
    name: str = "base"
    timeframe: str = "1h"
    params: dict = field(default_factory=dict)
    max_hold_bars: int = 0  # 0 = no time stop

    def signals(self, df: pd.DataFrame, funding: pd.DataFrame | None = None) -> pd.DataFrame:
        """Return frame aligned to df: side (+1/-1/0), stop, take, confidence, rationale."""
        raise NotImplementedError

    def latest_signal(self, df: pd.DataFrame, funding: pd.DataFrame | None = None) -> Signal | None:
        sig = self.signals(df, funding).iloc[-1]
        if sig["side"] == 0:
            return None
        return Signal("long" if sig["side"] > 0 else "short", float(df["close"].iloc[-1]),
                      float(sig["stop"]), float(sig["take"]), float(sig["confidence"]),
                      str(sig["rationale"]))


def empty_signals(df: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({"side": 0, "stop": float("nan"), "take": float("nan"),
                         "confidence": 0.0, "rationale": ""}, index=df.index)
