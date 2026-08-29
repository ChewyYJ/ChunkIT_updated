from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from shared.schemas import ContentType, ScoredUnit, SelectionPlan

# Wire limits of the scorer contract — single source of truth, mirrored by
# the client's clamping and the scorer service's own validation.
MAX_UNIT_CHARS = 20_000
MAX_QUERY_CHARS = 16_000
MAX_ITEMS_PER_REQUEST = 128


class ScoreItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit_id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=MAX_UNIT_CHARS)
    content_type: ContentType = "text"

    @field_validator("text")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")
        return value


class ScoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    items: list[ScoreItem] = Field(min_length=1, max_length=MAX_ITEMS_PER_REQUEST)


class ScoreResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Constrained charset: a misbehaving remote scorer must not be able to
    # smuggle HTML/control characters into error messages or debug output.
    unit_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.:\-]+$")
    relevance: float = Field(ge=0.0, le=1.0)
    truncated: bool = False


class ScoreResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    device: str
    latency_ms: float = Field(ge=0.0)
    scores: list[ScoreResult]


class HealthResponse(BaseModel):
    status: Literal["loading", "ready", "error"]
    model: str
    device: str


@dataclass(frozen=True)
class ScoringOutcome:
    scored_units: list[ScoredUnit]
    source: Literal["nosana", "fallback"]
    latency_ms: float
    fallback_reason: Optional[str] = None


@dataclass(frozen=True)
class RankAndSelectOutcome:
    plan: SelectionPlan
    scored_units: list[ScoredUnit]
    score_source: Literal["nosana", "fallback"]
    nosana_ms: float
    fallback_reason: Optional[str] = None
