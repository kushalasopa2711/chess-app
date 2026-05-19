"""
Background sweeper that deletes webcam evidence (file + DB row) once it
exceeds the configured retention period.

Default retention: 7 days. Override with the ``VIDEO_RETENTION_DAYS`` env var.

The sweeper runs once on startup (with a small delay so it doesn't block the
boot path) and then every ``VIDEO_RETENTION_SWEEP_INTERVAL_HOURS`` hours
(default 12). It is best-effort — errors deleting a single file are logged
and the row is still removed so we don't keep retrying forever.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Tuple

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import AsyncSessionLocal
from models import VideoChunk

logger = logging.getLogger(__name__)

VIDEO_RETENTION_DAYS = int(os.getenv("VIDEO_RETENTION_DAYS", "7"))
VIDEO_RETENTION_SWEEP_INTERVAL_HOURS = int(
    os.getenv("VIDEO_RETENTION_SWEEP_INTERVAL_HOURS", "12")
)
VIDEOS_DIR = Path(os.getenv("VIDEOS_DIR", "videos")).resolve()


async def _purge_once(session: AsyncSession) -> Tuple[int, int]:
    """Delete chunks older than VIDEO_RETENTION_DAYS. Returns (files_deleted, rows_deleted)."""
    cutoff = datetime.utcnow() - timedelta(days=VIDEO_RETENTION_DAYS)
    expired = (
        await session.execute(
            select(VideoChunk).where(VideoChunk.created_at < cutoff)
        )
    ).scalars().all()

    files_deleted = 0
    ids: list[int] = []
    for vc in expired:
        ids.append(vc.id)
        candidates: list[Path] = []
        raw = Path(vc.file_path)
        candidates.append(raw if raw.is_absolute() else (Path.cwd() / raw))
        candidates.append(
            VIDEOS_DIR
            / str(vc.game_id)
            / str(vc.user_id)
            / f"chunk_{vc.chunk_number:04d}.webm"
        )
        removed = False
        for cand in candidates:
            try:
                rp = cand.resolve()
                rp.relative_to(VIDEOS_DIR)
                if rp.is_file():
                    rp.unlink(missing_ok=True)
                    files_deleted += 1
                    removed = True
                    break
            except (ValueError, OSError) as e:
                logger.warning(
                    "Could not delete video file for chunk %d at %s: %s",
                    vc.id, cand, e,
                )
        if not removed:
            logger.info(
                "Chunk %d (game=%d user=%d) — no file on disk; only DB row will be removed.",
                vc.id, vc.game_id, vc.user_id,
            )

    if ids:
        await session.execute(delete(VideoChunk).where(VideoChunk.id.in_(ids)))
        await session.commit()

    # Sweep any now-empty <game_id>/<user_id> directories so the videos tree
    # doesn't accumulate dead folders.
    try:
        if VIDEOS_DIR.is_dir():
            for game_dir in VIDEOS_DIR.iterdir():
                if not game_dir.is_dir():
                    continue
                for user_dir in game_dir.iterdir():
                    if user_dir.is_dir() and not any(user_dir.iterdir()):
                        try:
                            user_dir.rmdir()
                        except OSError:
                            pass
                if game_dir.is_dir() and not any(game_dir.iterdir()):
                    try:
                        game_dir.rmdir()
                    except OSError:
                        pass
    except OSError as e:
        logger.warning("Video tree cleanup failed: %s", e)

    return files_deleted, len(ids)


async def purge_expired_videos() -> Tuple[int, int]:
    """One-shot purge — useful for tests and for the admin trigger endpoint."""
    async with AsyncSessionLocal() as session:
        return await _purge_once(session)


async def retention_loop() -> None:
    """Long-running task — runs once shortly after startup, then on an interval."""
    # Short initial delay so the app finishes boot before we start touching files.
    await asyncio.sleep(30)
    interval_sec = max(60, VIDEO_RETENTION_SWEEP_INTERVAL_HOURS * 3600)
    while True:
        try:
            files, rows = await purge_expired_videos()
            if rows:
                logger.info(
                    "Video retention sweep: removed %d file(s), %d DB row(s) older than %d day(s).",
                    files, rows, VIDEO_RETENTION_DAYS,
                )
            else:
                logger.debug("Video retention sweep: nothing to purge.")
        except Exception:  # noqa: BLE001 — we never want this loop to die
            logger.exception("Video retention sweep failed; will retry on next interval.")
        await asyncio.sleep(interval_sec)
