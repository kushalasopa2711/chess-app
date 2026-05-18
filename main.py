"""
ChessWager API  +  Frontend
============================
Run:  python -m uvicorn main:app --reload --port 8000
Docs: http://localhost:8000/docs
App:  http://localhost:8000/
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from database import init_db
from routers.auth_router import router as auth_router
from routers.users_router import router as users_router
from routers.wallet_router import router as wallet_router
from routers.games_router import router as games_router
from routers.video_router import router as video_router
from routers.admin_router import router as admin_router
from routers.deposit_router import router as deposit_router

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logging.getLogger(__name__).info("Database ready.")
    yield


app = FastAPI(
    title="ChessWager API",
    description="Multiplayer chess with micro-investments (≤₹100) and live video anti-cheat.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
        return FileResponse(str(index))
    return {"message": "ChessWager API", "docs": "/docs", "status": "ok"}


@app.get("/admin", include_in_schema=False)
async def serve_admin():
    admin_page = STATIC_DIR / "admin.html"
    if admin_page.exists():
        return FileResponse(str(admin_page))
    return {"message": "Admin page not found."}


@app.get("/health", tags=["Health"])
async def health():
    return {"status": "ok"}
