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


def _upgrade_schema_sync(connection) -> None:
    """Add columns introduced after first deploy (SQLite + PostgreSQL)."""
    from sqlalchemy import inspect, text

    insp = inspect(connection)
    dialect = connection.dialect.name
    if "games" not in insp.get_table_names():
        return

    cols = {c["name"] for c in insp.get_columns("games")}

    def add_column(name: str, sql_pg: str, sql_sqlite: str) -> None:
        if name in cols:
            return
        stmt = sql_pg if dialect == "postgresql" else sql_sqlite
        connection.execute(text(stmt))

    add_column("is_vs_cpu", "ALTER TABLE games ADD COLUMN IF NOT EXISTS is_vs_cpu BOOLEAN NOT NULL DEFAULT false",
               "ALTER TABLE games ADD COLUMN is_vs_cpu BOOLEAN DEFAULT 0")
    add_column("video_prize_terms_ack", "ALTER TABLE games ADD COLUMN IF NOT EXISTS video_prize_terms_ack BOOLEAN NOT NULL DEFAULT false",
               "ALTER TABLE games ADD COLUMN video_prize_terms_ack BOOLEAN DEFAULT 0")
    add_column("clock_initial_sec", "ALTER TABLE games ADD COLUMN IF NOT EXISTS clock_initial_sec INTEGER NOT NULL DEFAULT 600",
               "ALTER TABLE games ADD COLUMN clock_initial_sec INTEGER DEFAULT 600")
    add_column("clock_increment_sec", "ALTER TABLE games ADD COLUMN IF NOT EXISTS clock_increment_sec INTEGER NOT NULL DEFAULT 5",
               "ALTER TABLE games ADD COLUMN clock_increment_sec INTEGER DEFAULT 5")
    add_column("white_time_ms", "ALTER TABLE games ADD COLUMN IF NOT EXISTS white_time_ms INTEGER NOT NULL DEFAULT 600000",
               "ALTER TABLE games ADD COLUMN white_time_ms INTEGER DEFAULT 600000")
    add_column("black_time_ms", "ALTER TABLE games ADD COLUMN IF NOT EXISTS black_time_ms INTEGER NOT NULL DEFAULT 600000",
               "ALTER TABLE games ADD COLUMN black_time_ms INTEGER DEFAULT 600000")
    add_column("clock_last_tick_at", "ALTER TABLE games ADD COLUMN IF NOT EXISTS clock_last_tick_at TIMESTAMP",
               "ALTER TABLE games ADD COLUMN clock_last_tick_at TIMESTAMP")

    if "users" in insp.get_table_names():
        ucols = {c["name"] for c in insp.get_columns("users")}
        if "is_bot" not in ucols:
            if dialect == "postgresql":
                connection.execute(
                    text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_bot BOOLEAN NOT NULL DEFAULT false")
                )
            else:
                connection.execute(text("ALTER TABLE users ADD COLUMN is_bot BOOLEAN DEFAULT 0"))


async def _ensure_cpu_user_async() -> None:
    import secrets

    from sqlalchemy import select

    from auth import hash_password
    from config import CPU_BOT_EMAIL, CPU_BOT_USERNAME
    from models import User, Wallet

    async with AsyncSessionLocal() as session:
        r = await session.execute(select(User).where(User.username == CPU_BOT_USERNAME))
        existing = r.scalar_one_or_none()
        if existing:
            if not existing.is_bot:
                existing.is_bot = True
                await session.commit()
            return
        u = User(
            username=CPU_BOT_USERNAME,
            email=CPU_BOT_EMAIL,
            hashed_password=hash_password(secrets.token_hex(32)),
            is_bot=True,
        )
        session.add(u)
        await session.flush()
        session.add(Wallet(user_id=u.id, balance=0.0, total_invested=0.0))
        await session.commit()


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
            WithdrawalRequest,
        )
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_upgrade_schema_sync)
    await _ensure_cpu_user_async()
