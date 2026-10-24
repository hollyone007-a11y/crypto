import json
from types import SimpleNamespace

import numpy as np
import pandas as pd

H = 3_600_000


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        if isinstance(r, tuple):  # (text, stop_reason)
            text, stop = r
        else:
            text, stop = (r if isinstance(r, str) else json.dumps(r)), "end_turn"
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop,
                               usage=SimpleNamespace(input_tokens=100, output_tokens=50))


class FakeClient:
    def __init__(self, *responses):
        self.beta = SimpleNamespace(messages=FakeMessages(responses))

    @property
    def calls(self):
        return self.beta.messages.calls


def synthetic_market(n=3000, seed=7, start_ts=0):
    rng = np.random.default_rng(seed)
    drift = np.where((np.arange(n) // 500) % 2 == 0, 0.0015, 0.0)
    close = 100 * np.exp(np.cumsum(drift + rng.normal(0, 0.01, n)))
    open_ = np.r_[close[0], close[:-1]]
    spread = np.abs(rng.normal(0, 0.006, n)) * close
    df = pd.DataFrame({"ts": start_ts + np.arange(n) * H, "open": open_,
                       "high": np.maximum(open_, close) + spread,
                       "low": np.minimum(open_, close) - spread, "close": close, "volume": 1.0})
    f_ts = start_ts + np.arange(0, n * H, 8 * H)
    rate = rng.normal(0.0001, 0.0001, len(f_ts))
    if len(rate) > 200:
        rate[[150, 200]] = [0.003, -0.003]
    return df, pd.DataFrame({"ts": f_ts, "rate": rate})
