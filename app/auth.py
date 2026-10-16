"""Owner-only gateway authentication, independent of upstream provider keys."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.datastructures import MutableHeaders

COOKIE_NAME = "chunkit_session"
SESSION_SECONDS = 8 * 60 * 60
PUBLIC_ROUTES = {
    ("GET", "/"), ("GET", "/dashboard"), ("GET", "/health"),
    ("HEAD", "/"), ("HEAD", "/dashboard"), ("HEAD", "/health"),
    ("GET", "/auth/status"), ("POST", "/auth/login"), ("POST", "/auth/logout"),
}
router = APIRouter(prefix="/auth", tags=["Authentication"])


def on_vercel() -> bool:
    return os.getenv("VERCEL", "").lower() in {"1", "true"}


def auth_required() -> bool:
    # A local convenience switch must never remove the deployed boundary.
    return on_vercel() or os.getenv("CHUNKIT_AUTH_DISABLED", "").lower() != "true"


def gateway_key() -> str:
    return os.getenv("CHUNKIT_GATEWAY_API_KEY", "")


def auth_configured() -> bool:
    key = gateway_key()
    return bool(key) and (not on_vercel() or len(key) >= 32)


def key_matches(candidate: str) -> bool:
    return bool(candidate) and auth_configured() and hmac.compare_digest(
        candidate.encode("utf-8"), gateway_key().encode("utf-8")
    )


def _signature(payload: str) -> str:
    return hmac.new(gateway_key().encode("utf-8"),
                    ("chunkit-session-v1:" + payload).encode("ascii"), hashlib.sha256).hexdigest()


def create_session() -> str:
    payload = f"{int(time.time()) + SESSION_SECONDS}.{secrets.token_hex(24)}"
    return f"{payload}.{_signature(payload)}"


def valid_session(value: str) -> bool:
    if not value or len(value) > 256 or not auth_configured():
        return False
    try:
        expires, nonce, signature = value.split(".")
        expiry = int(expires)
        if not int(time.time()) < expiry <= int(time.time()) + SESSION_SECONDS:
            return False
        if len(nonce) != 48 or len(signature) != 64:
            return False
        return hmac.compare_digest(signature, _signature(f"{expires}.{nonce}"))
    except (ValueError, TypeError, UnicodeError):
        return False


def authentication(request: Request) -> str | None:
    if not auth_required():
        return "local"
    # Custom header lets Anthropic clients retain their provider auth header.
    explicit = request.headers.get("x-chunkit-api-key")
    if explicit is not None:
        return "header" if key_matches(explicit) else None
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and key_matches(token):
        return "bearer"
    if valid_session(request.cookies.get(COOKIE_NAME, "")):
        return "cookie"
    return None


def same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return request.headers.get("sec-fetch-site") not in {"cross-site", "same-site"}
    expected_scheme = "https" if on_vercel() else request.url.scheme
    try:
        parsed = urlsplit(origin)
        return parsed.scheme == expected_scheme and parsed.netloc == request.headers.get("host")
    except ValueError:
        return False


class GatewayAuthMiddleware:
    """ASGI middleware preserves streaming and ContextVars in the gateway."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        identity = authentication(request)
        scope["chunkit.auth"] = identity

        async def private_send(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["cache-control"] = "no-store"
                headers["x-content-type-options"] = "nosniff"
                headers["x-frame-options"] = "DENY"
                headers["referrer-policy"] = "no-referrer"
            await send(message)

        public = (request.method, request.url.path) in PUBLIC_ROUTES
        preflight = (request.method == "OPTIONS" and "origin" in request.headers
                     and "access-control-request-method" in request.headers)
        if not public and not preflight:
            if auth_required() and not auth_configured():
                await JSONResponse({"detail": "Gateway access is not configured."}, status_code=503)(scope, receive, private_send)
                return
            if not identity:
                await JSONResponse({"detail": "Sign in to access this gateway."}, status_code=401,
                                   headers={"WWW-Authenticate": "Bearer"})(scope, receive, private_send)
                return
            if identity == "cookie" and request.method not in {"GET", "HEAD"} and not same_origin(request):
                await JSONResponse({"detail": "Cross-origin requests are not allowed."}, status_code=403)(scope, receive, private_send)
                return
        await self.app(scope, receive, private_send)


class LoginRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=512, repr=False)


@router.get("/status")
def status(request: Request):
    return {"authenticated": bool(authentication(request)), "required": auth_required(),
            "configured": auth_configured() or not auth_required()}


@router.post("/login")
def login(body: LoginRequest, request: Request):
    if not same_origin(request):
        raise HTTPException(403, "Cross-origin requests are not allowed.")
    if not auth_configured():
        raise HTTPException(503, "Gateway access is not configured.")
    if not key_matches(body.api_key):
        raise HTTPException(401, "The access key is incorrect.")
    response = JSONResponse({"authenticated": True})
    response.set_cookie(COOKIE_NAME, create_session(), max_age=SESSION_SECONDS,
                        httponly=True, secure=on_vercel() or request.url.scheme == "https",
                        samesite="strict", path="/")
    return response


@router.post("/logout")
def logout(request: Request):
    if not same_origin(request):
        raise HTTPException(403, "Cross-origin requests are not allowed.")
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True,
                           secure=on_vercel() or request.url.scheme == "https", samesite="strict")
    return response
