import os
import ssl
from urllib.parse import urlparse

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from config import DATABASE_URL


def _engine_kwargs():
    """Render Postgres (and many hosts) require TLS for external connections."""
    kwargs: dict = {"echo": False}
    parsed = urlparse(DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://", 1))
    if parsed.scheme.startswith("postgresql") and (
        "render.com" in (parsed.hostname or "")
        or os.getenv("DATABASE_SSL", "").lower() in ("1", "true", "require")
    ):
        kwargs["connect_args"] = {"ssl": ssl.create_default_context()}
    return kwargs


engine = create_async_engine(DATABASE_URL, **_engine_kwargs())
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db() -> None:
    """Create all tables on startup."""
    async with engine.begin() as conn:
        from models import (  # noqa: F401
            AntiCheatFlag,
            DepositRequest,
            Game,
            Move,
            PendingPayout,
            Penalty,
            Transaction,
            User,
            VideoChunk,
            Wallet,
        )
        await conn.run_sync(Base.metadata.create_all)
