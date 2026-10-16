"""Hosted boundary tests never call a real provider or use real credentials."""
import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import app.auth as auth
import app.gateway.routes as routes
import app.session.store as store
from app.main import app

KEY = "test-owner-gateway-key-32-characters-minimum"


@pytest.fixture
def secured(tmp_path, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("CHUNKIT_GATEWAY_API_KEY", KEY)
    monkeypatch.delenv("CHUNKIT_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("NOSANA_ENABLED", "false")
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "auth.db"))
    with TestClient(app, base_url="https://chunkit.test") as client:
        yield client


@pytest.mark.parametrize("method,path", [
    ("GET", "/activity"), ("GET", "/config"), ("GET", "/openapi.json"),
    ("GET", "/docs"), ("GET", "/redoc"), ("GET", "/v1/models"),
    ("POST", "/v1/chat/completions"), ("POST", "/v1/messages"),
    ("POST", "/v1/messages/count_tokens"), ("OPTIONS", "/activity"),
])
def test_unauthenticated_requests_rejected_before_handler(secured, method, path):
    response = secured.request(method, path, json={"invalid": "never parsed"})
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


def test_public_shell_and_minimal_health(secured):
    assert secured.get("/dashboard").status_code == 200
    assert secured.get("/health").json() == {"status": "ok"}
    assert secured.get("/auth/status").json() == {
        "authenticated": False, "required": True, "configured": True,
    }


def test_missing_key_fails_closed_even_with_local_optout(secured, monkeypatch):
    monkeypatch.delenv("CHUNKIT_GATEWAY_API_KEY")
    monkeypatch.setenv("CHUNKIT_AUTH_DISABLED", "true")
    assert secured.get("/activity").status_code == 503
    assert secured.post("/auth/login", json={"api_key": KEY}).status_code == 503
    assert secured.get("/auth/status").json() == {
        "authenticated": False, "required": True, "configured": False,
    }
    monkeypatch.setenv("CHUNKIT_GATEWAY_API_KEY", "short")
    assert secured.get("/activity", headers={"Authorization": "Bearer short"}).status_code == 503


def test_login_cookie_logout_and_no_key_in_response(secured):
    denied = secured.post("/auth/login", json={"api_key": "incorrect"})
    assert denied.status_code == 401
    assert "set-cookie" not in denied.headers
    response = secured.post("/auth/login", json={"api_key": KEY})
    assert response.json() == {"authenticated": True}
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie
    assert "max-age=28800" in cookie
    assert KEY not in response.text and KEY not in response.headers["set-cookie"]
    assert secured.get("/activity").status_code == 200
    assert secured.get("/auth/status").json()["authenticated"] is True
    assert secured.post("/auth/logout").json() == {"authenticated": False}
    assert secured.get("/activity").status_code == 401


def test_validation_does_not_echo_submitted_key(secured):
    submitted = "private-input-" * 100
    response = secured.post("/auth/login", json={"api_key": submitted})
    assert response.status_code == 422
    assert submitted not in response.text


def test_cookie_expiry_tampering_and_key_rotation(secured, monkeypatch):
    secured.post("/auth/login", json={"api_key": KEY})
    original = secured.cookies.get(auth.COOKIE_NAME)
    secured.cookies.clear()
    secured.cookies.set(auth.COOKIE_NAME, original[:-1] + ("a" if original[-1] != "a" else "b"))
    assert secured.get("/activity").status_code == 401
    secured.cookies.set(auth.COOKIE_NAME, original)
    monkeypatch.setattr(auth.time, "time", lambda: int(original.split(".")[0]) + 1)
    assert secured.get("/activity").status_code == 401


def test_key_rotation_invalidates_sessions(secured, monkeypatch):
    secured.post("/auth/login", json={"api_key": KEY})
    monkeypatch.setenv("CHUNKIT_GATEWAY_API_KEY", "replacement-owner-key-32-characters-minimum")
    assert secured.get("/activity").status_code == 401


def test_cross_origin_cookie_writes_and_login_are_rejected(secured):
    evil = {"Origin": "https://other.test"}
    assert secured.post("/auth/login", json={"api_key": KEY}, headers=evil).status_code == 403
    secured.post("/auth/login", json={"api_key": KEY})
    assert secured.post("/auth/logout", headers=evil).status_code == 403
    assert secured.post("/v1/chat/completions", json={}, headers=evil).status_code == 403
    response = secured.post("/auth/logout", headers={"Origin": "https://chunkit.test"})
    assert response.status_code == 200


def test_bearer_and_explicit_header_authentication(secured):
    assert secured.get("/activity", headers={"Authorization": f"Bearer {KEY}"}).status_code == 200
    assert secured.get("/activity", headers={"X-ChunkIT-API-Key": KEY}).status_code == 200
    assert secured.get("/activity", headers={"Authorization": "Bearer incorrect"}).status_code == 401
    secured.post("/auth/login", json={"api_key": KEY})
    assert secured.get("/activity", headers={"X-ChunkIT-API-Key": "incorrect"}).status_code == 401


@respx.mock
def test_gateway_secret_and_cookie_not_forwarded_to_anthropic(secured):
    seen = []

    def upstream(request):
        seen.append(dict(request.headers))
        return httpx.Response(200, json={"data": []})

    respx.get("https://api.anthropic.com/v1/models").mock(side_effect=upstream)
    secured.post("/auth/login", json={"api_key": KEY})
    response = secured.get("/v1/models", headers={
        "X-ChunkIT-API-Key": KEY, "Authorization": "Bearer upstream-client-token",
    })
    assert response.status_code == 200
    assert seen[-1]["authorization"] == "Bearer upstream-client-token"
    assert "x-chunkit-api-key" not in seen[-1] and "cookie" not in seen[-1]
    secured.cookies.clear()
    secured.get("/v1/models", headers={"Authorization": f"Bearer {KEY}", "x-api-key": "provider-test"})
    assert "authorization" not in seen[-1]
    assert seen[-1]["x-api-key"] == "provider-test"
    secured.get("/v1/models", headers={
        "X-ChunkIT-API-Key": KEY, "Authorization": f"Bearer {KEY}", "x-api-key": KEY,
    })
    assert "authorization" not in seen[-1]
    assert "x-api-key" not in seen[-1]
    assert "x-chunkit-api-key" not in seen[-1]


def test_config_is_truthful_without_exposing_credentials(secured, monkeypatch):
    monkeypatch.setenv("DAYTONA_BACKEND", "remote")
    monkeypatch.setenv("DAYTONA_API_KEY", "never-return-this")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    response = secured.get("/config", headers={"Authorization": f"Bearer {KEY}"})
    data = response.json()
    assert data["provider"]["configured"] is False
    assert data["storage"]["mode"] == "ephemeral"
    assert data["storage"]["persistent"] is False
    assert data["activity"] == {"scope": "instance", "persistent": False}
    assert data["daytona"]["backend"] == "local"
    assert "never-return-this" not in response.text and KEY not in response.text
    from app.daytona.remote import daytona_remote_enabled
    assert daytona_remote_enabled() is False


def test_explicit_local_optout(secured, monkeypatch):
    monkeypatch.delenv("VERCEL")
    monkeypatch.delenv("CHUNKIT_GATEWAY_API_KEY")
    monkeypatch.setenv("CHUNKIT_AUTH_DISABLED", "true")
    assert secured.get("/activity").status_code == 200
    assert secured.get("/auth/status").json()["required"] is False


def test_authenticated_chat_calls_real_pipeline(secured, monkeypatch):
    calls = []

    async def upstream(messages, **kwargs):
        calls.append(messages)
        return {"response": {"choices": [{"message": {"content": "test answer"}}]}, "retrieval_rounds": 0}

    monkeypatch.setattr(routes, "get_completion", upstream)
    response = secured.post("/v1/chat/completions", headers={"Authorization": f"Bearer {KEY}"},
                            json={"messages": [{"role": "user", "content": "Hello"}]})
    assert response.status_code == 200
    assert calls and response.json()["choices"][0]["message"]["content"] == "test answer"
    assert "debug" in response.json()
