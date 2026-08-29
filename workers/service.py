from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel

from app.daytona.worker import PreprocessedBlock, prepare_block_result
from compressors.common import estimate_tokens
from shared.schemas import ContextBlock

app = FastAPI(title="ChunkIT Daytona Worker")


class PrepareRequest(BaseModel):
    block_id: str
    role: str
    content: str
    message_index: int
    detected_route: str | None = None
    schema_content_type: str | None = None
    source_hash: str | None = None


@app.post("/prepare")
def prepare(request: PrepareRequest):
    block = ContextBlock(
        block_id=request.block_id,
        role=request.role,
        content=request.content,
        message_index=request.message_index,
        type_hint=request.schema_content_type,
        pinned=False,
    )
    preprocessed = PreprocessedBlock(
        block=block,
        sanitized_content=request.content,
        detected_route=request.detected_route or "text",
        schema_content_type=request.schema_content_type or "text",
        redaction_applied=False,
        source_hash=request.source_hash or "",
        original_tokens=estimate_tokens(request.content),
    )
    result = prepare_block_result(block, backend="daytona", preprocessed=preprocessed)
    return {"units": [unit.__dict__ for unit in result.units], "metrics": result.metrics}
