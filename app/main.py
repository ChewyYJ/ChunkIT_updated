from dotenv import load_dotenv

load_dotenv()

import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from app.auth import GatewayAuthMiddleware, on_vercel, router as auth_router
from app.gateway.activity import recent as recent_activity, totals as activity_totals
from app.daytona.remote import initialize_daytona_pool, shutdown_daytona_pool
from app.gateway.anthropic_routes import router as anthropic_router
from app.gateway.routes import router as gateway_router
from app.gateway.termcolor import banner
from app.nosana.client import NosanaScorerClient
from app.nosana.config import NosanaSettings
from app.session.store import init_db, storage_status

DASHBOARD_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "dashboard.html")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    settings = NosanaSettings.from_env()
    http_client = httpx.AsyncClient()
    app.state.http_client = http_client
    app.state.nosana_settings = settings
    app.state.nosana_client = NosanaScorerClient(settings, http_client)
    try:
        # No-op unless DAYTONA_BACKEND=remote/daytona and DAYTONA_API_KEY set.
        await initialize_daytona_pool()
    except Exception as exc:
        print(f"[daytona] startup init skipped: {exc}")
    print(banner())
    try:
        yield
    finally:
        await shutdown_daytona_pool()
        await http_client.aclose()


app = FastAPI(title="ChunkIT", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    # Pydantic's default errors echo submitted input, including login keys.
    errors = [{"loc": error["loc"], "msg": error["msg"], "type": error["type"]}
              for error in exc.errors()]
    return JSONResponse({"detail": errors}, status_code=422)


# Cross-origin API clients must supply their own gateway bearer/header key.
# Browser login cookies are restricted to the same origin.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.add_middleware(GatewayAuthMiddleware)
app.include_router(auth_router)
app.include_router(gateway_router)
# Anthropic front (Claude Code): /v1/messages + a /v1/* passthrough catch-all.
# Registered second so /v1/chat/completions keeps precedence.
app.include_router(anthropic_router)


@app.get("/", include_in_schema=False)
def index():
    return RedirectResponse("/dashboard")


@app.get("/activity")
def activity():
    """Recent request summaries (sanitised) for the dashboard live view."""
    return {"requests": recent_activity(), "totals": activity_totals()}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/config")
def config():
    provider = os.getenv("LLM_PROVIDER", "openai").lower()
    provider_key = "OPENAI_API_KEY" if provider == "openai" else "MOONSHOT_API_KEY"
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini") if provider == "openai" else os.getenv("KIMI_MODEL", "kimi-k3")
    return {
        "provider": {"name": provider, "model": model,
                     "configured": provider in {"openai", "kimi"} and bool(os.getenv(provider_key))},
        "storage": storage_status(),
        "activity": {"scope": "instance", "persistent": False},
        "daytona": {"backend": "local" if on_vercel() else os.getenv("DAYTONA_BACKEND", "local")},
    }


@app.get("/dashboard")
def dashboard():
    return FileResponse(DASHBOARD_PATH, media_type="text/html")
