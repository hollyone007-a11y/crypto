"""Thin Claude wrapper: one call -> validated pydantic object or None.

Fail closed: any API error, refusal, truncation, invalid JSON or schema mismatch
returns None and is logged to analyst_runs. Callers treat None as "no trade"."""
import json
import logging
import time
from typing import TypeVar

import anthropic
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from config.settings import get_settings
from db.models import AnalystRun

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

ROUTINE_MODEL = "claude-sonnet-5-5"
WEEKLY_MODEL = "claude-opus-5-5"


def make_client() -> anthropic.Anthropic:
    key = get_settings().anthropic_api_key.get_secret_value()
    return anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()


class LLM:
    def __init__(self, session: Session, client=None):
        self.s = session
        self._client = client

    @property
    def client(self):
        if self._client is None:
            self._client = make_client()
        return self._client

    def call(self, *, purpose: str, model: str, system: str, user: str, schema: dict,
             out: type[T], effort: str = "medium", max_tokens: int = 8000) -> T | None:
        run = AnalystRun(ts=int(time.time() * 1000), purpose=purpose, model=model, ok=False,
                         request={"system": system, "user": user, "effort": effort})
        try:
            resp = self.client.beta.messages.create(
                model=model, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": effort,
                               "format": {"type": "json_schema", "schema": schema}},
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            )
            usage = getattr(resp, "usage", None)
            run.input_tokens = getattr(usage, "input_tokens", 0) or 0
            run.output_tokens = getattr(usage, "output_tokens", 0) or 0
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            run.response = text
            if resp.stop_reason != "end_turn":
                raise ValueError(f"stop_reason={resp.stop_reason}")
            result = out.model_validate(json.loads(text))
            run.ok = True
            return result
        except (anthropic.AnthropicError, ValueError, ValidationError, TypeError) as e:
            # json.JSONDecodeError is a ValueError
            run.error = f"{type(e).__name__}: {e}"[:2000]
            log.warning("llm %s failed: %s", purpose, run.error)
            return None
        finally:
            self.s.add(run)
            self.s.commit()
