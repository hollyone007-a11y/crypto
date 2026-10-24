"""Output contracts for every LLM call. The JSON schema is sent to the API
(structured outputs); pydantic re-validates with tighter bounds locally."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------- routine analyst decision ----------

class Pick(_Strict):
    candidate_id: str = Field(max_length=64)
    take: bool
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=500)


class Decision(_Strict):
    thesis: str = Field(min_length=1, max_length=1500)
    market_view: Literal["bullish", "bearish", "neutral", "uncertain"]
    risk_flags: list[str] = Field(max_length=10)
    strategy_weights: dict[str, float]
    picks: list[Pick] = Field(max_length=10)


DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "thesis": {"type": "string"},
        "market_view": {"type": "string", "enum": ["bullish", "bearish", "neutral", "uncertain"]},
        "risk_flags": {"type": "array", "items": {"type": "string"}},
        "strategy_weights": {
            "type": "object",
            "properties": {"trend_donchian": {"type": "number"}, "meanrev_z": {"type": "number"},
                           "funding_fade": {"type": "number"}},
            "required": ["trend_donchian", "meanrev_z", "funding_fade"],
            "additionalProperties": False},
        "picks": {"type": "array", "items": {
            "type": "object",
            "properties": {"candidate_id": {"type": "string"}, "take": {"type": "boolean"},
                           "confidence": {"type": "number"}, "reason": {"type": "string"}},
            "required": ["candidate_id", "take", "confidence", "reason"],
            "additionalProperties": False}},
    },
    "required": ["thesis", "market_view", "risk_flags", "strategy_weights", "picks"],
    "additionalProperties": False,
}


# ---------- post-trade lesson ----------

class LessonOut(_Strict):
    outcome: Literal["win", "loss", "scratch"]
    what_worked: str = Field(max_length=800)
    mistake: str = Field(max_length=800)
    lesson: str = Field(min_length=1, max_length=800)
    tags: list[str] = Field(max_length=8)


LESSON_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": ["win", "loss", "scratch"]},
        "what_worked": {"type": "string"}, "mistake": {"type": "string"},
        "lesson": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["outcome", "what_worked", "mistake", "lesson", "tags"],
    "additionalProperties": False,
}


# ---------- weekly review ----------

class ParamChange(_Strict):
    name: str = Field(max_length=32)
    value: float


class ParamProposal(_Strict):
    family: Literal["trend_donchian", "meanrev_z", "funding_fade", "ema_adx"]
    changes: list[ParamChange] = Field(min_length=1, max_length=5)
    rationale: str = Field(max_length=1500)


class CodeProposal(_Strict):
    class_name: str = Field(pattern=r"^[A-Z][A-Za-z0-9]{2,40}$")
    family: str = Field(pattern=r"^[a-z][a-z0-9_]{2,40}$")
    timeframe: Literal["1h", "4h"]
    code: str = Field(max_length=12000)
    rationale: str = Field(max_length=1500)


class WeeklyReview(_Strict):
    summary: str = Field(max_length=4000)
    param_proposals: list[ParamProposal] = Field(max_length=3)
    code_proposals: list[CodeProposal] = Field(max_length=1)


WEEKLY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "param_proposals": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "family": {"type": "string", "enum": ["trend_donchian", "meanrev_z", "funding_fade", "ema_adx"]},
                "changes": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "value": {"type": "number"}},
                    "required": ["name", "value"], "additionalProperties": False}},
                "rationale": {"type": "string"}},
            "required": ["family", "changes", "rationale"], "additionalProperties": False}},
        "code_proposals": {"type": "array", "items": {
            "type": "object",
            "properties": {"class_name": {"type": "string"}, "family": {"type": "string"},
                           "timeframe": {"type": "string", "enum": ["1h", "4h"]},
                           "code": {"type": "string"}, "rationale": {"type": "string"}},
            "required": ["class_name", "family", "timeframe", "code", "rationale"],
            "additionalProperties": False}},
    },
    "required": ["summary", "param_proposals", "code_proposals"],
    "additionalProperties": False,
}
