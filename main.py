"""
ChessWager API  +  Frontend
============================
Local dev:   python -m uvicorn main:app --reload --port 8000
Production:  ENV=production gunicorn -k uvicorn.workers.UvicornWorker main:app
Docs:        http://localhost:8000/docs
App:         http://localhost:8000/
"""
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config import ALLOWED_ORIGINS, DATABASE_URL, ENV, IS_PROD
from database import init_db
from routers.auth_router import router as auth_router
from routers.users_router import router as users_router
from routers.wallet_router import router as wallet_router
from routers.games_router import router as games_router
from routers.video_router import router as video_router
from routers.admin_router import router as admin_router
from routers.deposit_router import router as deposit_router
from video_retention import (
    VIDEO_RETENTION_DAYS,
    VIDEO_RETENTION_SWEEP_INTERVAL_HOURS,
    retention_loop,
)

logging.basicConfig(
    level=logging.INFO if IS_PROD else logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    masked = DATABASE_URL
    try:
        parts = urlparse(DATABASE_URL)
        if parts.password:
            masked = DATABASE_URL.replace(parts.password, "***")
    except Exception:
        pass
    logger.info("Database ready  env=%s  url=%s", ENV, masked)

    import asyncio
    retention_task = asyncio.create_task(retention_loop())
    logger.info(
        "Video retention sweeper armed: %d-day retention, sweep every %dh.",
        VIDEO_RETENTION_DAYS, VIDEO_RETENTION_SWEEP_INTERVAL_HOURS,
    )
    try:
        yield
    finally:
        retention_task.cancel()
        try:
            await retention_task
        except (asyncio.CancelledError, Exception):
            pass


app = FastAPI(
    title="ChessWager API",
    description="Multiplayer chess with micro-investments (≤₹100) and live video anti-cheat.",
    version="1.0.0",
    lifespan=lifespan,
)

# In production, set ALLOWED_ORIGINS env var to a comma-separated allow-list.
# CORS with credentials cannot be used with allow_origins=["*"], so flip the
# credentials flag when running open in dev mode.
_cors_allow_credentials = ALLOWED_ORIGINS != ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=_cors_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)
if IS_PROD and ALLOWED_ORIGINS == ["*"]:
    logger.warning(
        "ALLOWED_ORIGINS is '*' in production. Set ALLOWED_ORIGINS to your "
        "real frontend origin(s) for a stricter CORS policy."
    )

# API routers
app.include_router(auth_router)
app.include_router(users_router)
app.include_router(wallet_router)
app.include_router(games_router)
app.include_router(video_router)
app.include_router(admin_router)
app.include_router(deposit_router)

# Serve frontend static files
STATIC_DIR = Path("static")
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def serve_frontend():
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(
            str(index),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )
    return {"message": "ChessWager API", "docs": "/docs", "status": "ok"}


@app.get("/admin", include_in_schema=False)
async def serve_admin():
    admin_page = STATIC_DIR / "admin.html"
    if admin_page.exists():
        return FileResponse(
            str(admin_page),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )
    return {"message": "Admin page not found."}


@app.get("/health", tags=["Health"])
async def health():
    return {"status": "ok", "env": ENV}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    fav = STATIC_DIR / "favicon.ico"
    if fav.exists():
        return FileResponse(str(fav))
    # 204 keeps logs cleaner than a 404 for every page load.
    from fastapi.responses import Response
    return Response(status_code=204)
