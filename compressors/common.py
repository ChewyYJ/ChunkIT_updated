from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit, urlunsplit

REDACTION_VERSION = "redaction_v2"
COMPRESSION_CONFIG_VERSION = "compression_config_v2"

SECRET_PATTERNS = (
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\b\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgho_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._-]{12,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9._-]{8,}\.[A-Za-z0-9._-]{8,}\b"),
    re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----[\s\S]+?-----END [A-Z ]+PRIVATE KEY-----"),
)
ENV_SECRET_RE = re.compile(
    r"(?im)^(?P<key>[A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|DATABASE_URL|PRIVATE_KEY)[A-Z0-9_]*)=(?P<value>.+)$"
)
DATABASE_URL_RE = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s]+")


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def redact_secrets(text: str) -> str:
    redacted = ENV_SECRET_RE.sub(_replace_env_secret, text)
    redacted = DATABASE_URL_RE.sub(_replace_database_url, redacted)
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(_replace_secret, redacted)
    return redacted


def _replace_secret(match: re.Match[str]) -> str:
    if match.lastindex and match.lastindex >= 2:
        label = match.group(1)
        return f"{label}=<REDACTED>"
    token = match.group(0)
    if token.lower().startswith("bearer "):
        return "Bearer <REDACTED>"
    if token.startswith("-----BEGIN "):
        return "<REDACTED_PRIVATE_KEY>"
    return "<REDACTED_SECRET>"


def _replace_env_secret(match: re.Match[str]) -> str:
    return f"{match.group('key')}=<REDACTED>"


def _replace_database_url(match: re.Match[str]) -> str:
    value = match.group(0)
    try:
        parts = urlsplit(value)
    except ValueError:
        return value

    if "@" not in parts.netloc or ":" not in parts.netloc.split("@", 1)[0]:
        return value

    credentials, host = parts.netloc.rsplit("@", 1)
    username = credentials.split(":", 1)[0]
    redacted_netloc = f"{username}:<REDACTED>@{host}"
    return urlunsplit((parts.scheme, redacted_netloc, parts.path, parts.query, parts.fragment))
