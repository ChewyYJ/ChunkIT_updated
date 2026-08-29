from __future__ import annotations

import os
from dataclasses import dataclass


def _as_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class NosanaSettings:
    enabled: bool
    scorer_url: str
    scorer_token: str
    timeout_seconds: float
    max_retries: int
    request_batch_size: int
    fallback_enabled: bool

    @classmethod
    def from_env(cls) -> "NosanaSettings":
        return cls(
            enabled=_as_bool("NOSANA_ENABLED", True),
            scorer_url=os.getenv("NOSANA_SCORER_URL", "").rstrip("/"),
            scorer_token=os.getenv("NOSANA_SCORER_TOKEN", ""),
            timeout_seconds=float(os.getenv("NOSANA_TIMEOUT_SECONDS", "5")),
            max_retries=int(os.getenv("NOSANA_MAX_RETRIES", "2")),
            request_batch_size=int(os.getenv("NOSANA_REQUEST_BATCH_SIZE", "128")),
            fallback_enabled=_as_bool("NOSANA_FALLBACK_ENABLED", True),
        )

    def validate_for_remote(self) -> None:
        if self.enabled and not self.scorer_url:
            raise ValueError("NOSANA_SCORER_URL is required when NOSANA_ENABLED=true")
        if self.enabled and not self.scorer_token:
            raise ValueError("NOSANA_SCORER_TOKEN is required when NOSANA_ENABLED=true")
        if self.timeout_seconds <= 0:
            raise ValueError("NOSANA_TIMEOUT_SECONDS must be positive")
        if not 1 <= self.request_batch_size <= 128:
            raise ValueError("NOSANA_REQUEST_BATCH_SIZE must be between 1 and 128")
