from __future__ import annotations

import asyncio
import os
import secrets
import time
from contextlib import asynccontextmanager
from typing import Literal

import torch
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sentence_transformers import CrossEncoder

MODEL_NAME = os.getenv("MODEL_NAME", "cross-encoder/ms-marco-MiniLM-L6-v2")
SCORER_TOKEN = os.getenv("SCORER_TOKEN", "")
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "32"))
MAX_LENGTH = int(os.getenv("MAX_LENGTH", "512"))
MAX_ITEMS = int(os.getenv("MAX_ITEMS", "128"))
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

_model: CrossEncoder | None = None
_load_error: str | None = None


class ScoreItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    unit_id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=20_000)
    content_type: Literal["log", "json", "code", "text"] = "text"

    @field_validator("text")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")
        return value


class ScoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=16_000)
    items: list[ScoreItem] = Field(min_length=1)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _model, _load_error
    try:
        _model = CrossEncoder(
            MODEL_NAME,
            device=DEVICE,
            max_length=MAX_LENGTH,
            activation_fn=torch.nn.Sigmoid(),
        )
    except Exception as exc:  # health reports the startup failure
        _load_error = f"{type(exc).__name__}: {exc}"
    yield
    _model = None


app = FastAPI(title="ChunkIT Nosana Relevance Scorer", lifespan=lifespan)


def require_token(received: str | None) -> None:
    if not SCORER_TOKEN:
        raise HTTPException(503, "SCORER_TOKEN is not configured")
    if received is None or not secrets.compare_digest(received, SCORER_TOKEN):
        raise HTTPException(401, "invalid scorer token")


@app.get("/health")
async def health() -> dict:
    if _load_error:
        return {"status": "error", "model": MODEL_NAME, "device": DEVICE,
                "detail": _load_error}
    return {
        "status": "ready" if _model is not None else "loading",
        "model": MODEL_NAME,
        "device": DEVICE,
    }


@app.post("/score")
async def score(
    request: ScoreRequest,
    x_scorer_token: str | None = Header(default=None, alias="X-Scorer-Token"),
) -> dict:
    require_token(x_scorer_token)
    if _model is None:
        raise HTTPException(503, "model is not ready")
    if len(request.items) > MAX_ITEMS:
        raise HTTPException(413, f"maximum {MAX_ITEMS} items per request")

    pairs = [(request.query, item.text) for item in request.items]
    started = time.perf_counter()
    scores = await asyncio.to_thread(
        _model.predict,
        pairs,
        batch_size=BATCH_SIZE,
        show_progress_bar=False,
        convert_to_numpy=True,
    )
    latency_ms = (time.perf_counter() - started) * 1000

    return {
        "model": MODEL_NAME,
        "device": DEVICE,
        "latency_ms": latency_ms,
        "scores": [
            {
                "unit_id": item.unit_id,
                "relevance": max(0.0, min(1.0, float(value))),
                "truncated": False,
            }
            for item, value in zip(request.items, scores, strict=True)
        ],
    }
