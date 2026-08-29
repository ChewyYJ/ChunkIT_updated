from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.daytona.worker import PreprocessedBlock, prepare_block_result
from compressors.common import estimate_tokens
from shared.schemas import ContextBlock


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        self._write_json({"status": "ok"})

    def do_POST(self):
        if self.path != "/prepare":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        block = ContextBlock(
            block_id=payload["block_id"],
            role=payload["role"],
            content=payload["content"],
            message_index=payload["message_index"],
            type_hint=payload.get("schema_content_type"),
            pinned=False,
        )
        preprocessed = PreprocessedBlock(
            block=block,
            sanitized_content=payload["content"],
            detected_route=payload.get("detected_route", "text"),
            schema_content_type=payload.get("schema_content_type", "text"),
            redaction_applied=False,
            source_hash=payload.get("source_hash", ""),
            original_tokens=estimate_tokens(payload["content"]),
        )
        result = prepare_block_result(block, backend="daytona", preprocessed=preprocessed)
        self._write_json({"units": [unit.__dict__ for unit in result.units], "metrics": result.metrics})

    def log_message(self, format: str, *args):
        return

    def _write_json(self, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    port = int(os.getenv("CHUNKIT_WORKER_PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
