"""
main.py — FastAPI application entry point for Railway deployment.

Endpoints:
  POST   /upload              Upload file to OpenAI Vector Store
  GET    /assets              List files in Vector Store
  DELETE /assets/{file_id}   Delete file from Vector Store
  POST   /chat                SSE streaming chat (Responses API + file_search)
  POST   /schedules/import    Extract and store a student schedule PDF
  GET    /health              Health check

All endpoints (except /health) require:
  Authorization: Bearer <Firebase ID Token>

Environment variables (set on Railway → Variables):
  OPENAI_API_KEY
  OPENAI_VECTOR_STORE_ID
  FIREBASE_SERVICE_ACCOUNT_JSON
  OPENAI_MODEL               (optional, default: gpt-4o)
  OPENAI_SCHEDULE_MODEL      (optional, defaults to OPENAI_MODEL)
  HISTORY_TOKEN_BUDGET       (optional, default: 3000)
  PORT                       (set automatically by Railway)
"""

import logging
import os

from dotenv import load_dotenv

# Load .env for local development (no-op in production where env vars are set directly)
load_dotenv()

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from contextlib import asynccontextmanager
import uuid

from app.firebase import init_firebase
from app.limiter import limiter
from app.routers import assets_router, chat_router, schedules_router, upload_router
from app.logger import setup_logging, request_id_var
from app.task_tracker import background_tasks


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
setup_logging()
logger = logging.getLogger(__name__)

# ── Lifespan ──────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting KuwaitChatAI backend…")
    
    # Fail-fast validation
    from app.config import get_config
    try:
        cfg = get_config()
        logger.info(
            "Config OK — model=%s  schedule_model=%s  vector_store=%s  history_budget=%d",
            cfg.OPENAI_MODEL,
            cfg.OPENAI_SCHEDULE_MODEL,
            cfg.OPENAI_VECTOR_STORE_ID,
            cfg.HISTORY_TOKEN_BUDGET,
        )
        init_firebase()
    except Exception as e:
        logger.error(f"Startup validation failed: {e}")
        raise
        
    yield  # App runs here
    
    logger.info("Shutting down KuwaitChatAI backend…")
    await background_tasks.wait_all(timeout=10.0)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="KuwaitChatAI Backend",
    description="RAG backend using OpenAI Responses API + File Search",
    version="2.0.0",
    docs_url="/docs",      # Swagger UI (disable in prod if desired)
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ── Rate Limiting ───────────────────────────────────────────────────────────
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── CORS ────────────────────────────────────────────────────────────────────
# Allow requests from Flutter Web, mobile, and the admin dashboard.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Restrict to your domain(s) in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Middleware ──────────────────────────────────────────────────────────────
class CorrelationIdMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        req_id_bytes = headers.get(b"x-request-id")
        req_id = req_id_bytes.decode("latin1") if req_id_bytes else str(uuid.uuid4())
            
        token = request_id_var.set(req_id)
        
        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                res_headers = message.setdefault("headers", [])
                if not any(k.lower() == b"x-request-id" for k, v in res_headers):
                    res_headers.append((b"x-request-id", req_id.encode("latin1")))
            await send(message)
            
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            request_id_var.reset(token)

app.add_middleware(CorrelationIdMiddleware)

# ── Routes ──────────────────────────────────────────────────────────────────
app.include_router(upload_router)
app.include_router(assets_router)
app.include_router(chat_router)
app.include_router(schedules_router)

@app.get("/health", tags=["health"])
async def health():
    """Liveness check for container orchestrators."""
    return {"status": "alive"}

@app.get("/ready", tags=["health"])
async def ready():
    """Readiness check validating config and dependencies."""
    try:
        from app.config import get_config
        get_config()
        # Verify Firebase is initialized
        from firebase_admin import get_app
        get_app()
        return {"status": "ready"}
    except Exception as e:
        logger.error(f"Readiness check failed: {e}")
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="Service not ready")
