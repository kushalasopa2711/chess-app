"""
Video monitoring router.

During any bet game every player must enable their webcam.
The browser uploads 30-second WebM chunks here, the server stores them,
and an admin (or automated system) can later review and penalise cheaters.

Endpoints:
  POST /video/{game_id}/chunk           – upload a recording chunk
  GET  /video/{game_id}/chunks          – list chunks for a game
  POST /video/{game_id}/flag/{user_id}  – flag a chunk as suspicious (admin)
  POST /video/{game_id}/penalise/{user_id} – deduct funds & optionally ban (admin)
  GET  /video/pending-review            – all flagged chunks (admin)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiofiles
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_active_unbanned_user, get_current_user
from database import get_db
from models import AntiCheatFlag, FlagType, Game, Penalty, Transaction, TransactionType, User, VideoChunk, Wallet
from websocket_manager import manager

router = APIRouter(prefix="/video", tags=["Video Anti-Cheat"])
logger = logging.getLogger(__name__)

VIDEOS_DIR = Path("videos")
VIDEOS_DIR.mkdir(exist_ok=True)

MAX_CHUNK_SIZE_MB = 50  # 50 MB per chunk ceiling
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "admin-secret-change-me")


def _require_admin(admin_key: str) -> None:
    if admin_key != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Admin access required.")


# ─── Upload chunk ─────────────────────────────────────────────────────────────

@router.post("/{game_id}/chunk", status_code=201)
async def upload_chunk(
    game_id: int,
    chunk: UploadFile = File(..., description="WebM video chunk (max 50 MB)"),
    chunk_number: int = Form(0),
    user: User = Depends(get_active_unbanned_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Upload a 30-second webcam chunk for a game.
    The browser should call this every 30 seconds while a bet game is active.
    """
    result = await db.execute(select(Game).where(Game.id == game_id))
    game = result.scalar_one_or_none()
    if not game:
        raise HTTPException(status_code=404, detail="Game not found.")
    if user.id not in (game.white_player_id, game.black_player_id):
        raise HTTPException(status_code=403, detail="Not a player in this game.")

    # Read and size-check
    data = await chunk.read()
    if len(data) > MAX_CHUNK_SIZE_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"Chunk exceeds {MAX_CHUNK_SIZE_MB} MB limit.")

    # Save file
    game_dir = VIDEOS_DIR / str(game_id) / str(user.id)
    game_dir.mkdir(parents=True, exist_ok=True)
    file_path = game_dir / f"chunk_{chunk_number:04d}.webm"

    async with aiofiles.open(file_path, "wb") as f:
        await f.write(data)

    # Record in DB
    record = VideoChunk(
        game_id=game_id,
        user_id=user.id,
        chunk_number=chunk_number,
        file_path=str(file_path),
        file_size_bytes=len(data),
    )
    db.add(record)
    await db.commit()

    logger.info("Saved video chunk %d for user %d game %d (%d bytes)",
                chunk_number, user.id, game_id, len(data))
    return {"status": "saved", "chunk_number": chunk_number, "bytes": len(data)}


@router.post("/{game_id}/no-camera", status_code=201)
async def report_no_camera(
    game_id: int,
    reason: str = Form("User denied camera permission"),
    user: User = Depends(get_active_unbanned_user),
    db: AsyncSession = Depends(get_db),
):
    """Report that the player blocked or lost camera access during a bet game."""
    flag = AntiCheatFlag(
        game_id=game_id,
        user_id=user.id,
        flag_type=FlagType.SUSPICIOUS_PATTERN,
        description=f"Camera not available during bet game: {reason}",
        severity=2,
    )
    db.add(flag)
    await db.commit()
    return {"status": "flagged"}


@router.post("/{game_id}/tab-hidden")
async def report_tab_hidden(
    game_id: int,
    duration_ms: int = Form(0),
    user: User = Depends(get_active_unbanned_user),
    db: AsyncSession = Depends(get_db),
):
    """Report that the player switched tabs/windows during a bet game (possible phone use)."""
    if duration_ms > 5000:  # only flag if away for > 5 seconds
        flag = AntiCheatFlag(
            game_id=game_id,
            user_id=user.id,
            flag_type=FlagType.SUSPICIOUS_PATTERN,
            description=f"Player switched away from game tab for {duration_ms} ms during bet game.",
            severity=1 if duration_ms < 30000 else 2,
        )
        db.add(flag)
        await db.commit()
    return {"status": "noted"}


# ─── Admin endpoints ───────────────────────────────────────────────────────────

@router.get("/{game_id}/chunks")
async def list_chunks(
    game_id: int,
    admin_key: str = "",
    db: AsyncSession = Depends(get_db),
):
    """List all video chunks for a game (admin only)."""
    _require_admin(admin_key)
    result = await db.execute(
        select(VideoChunk)
        .where(VideoChunk.game_id == game_id)
        .order_by(VideoChunk.user_id, VideoChunk.chunk_number)
    )
    chunks = result.scalars().all()
    return [
        {
            "id": c.id,
            "user_id": c.user_id,
            "chunk_number": c.chunk_number,
            "file_path": c.file_path,
            "file_size_mb": round(c.file_size_bytes / (1024 * 1024), 2),
            "flagged": c.flagged,
            "flag_reason": c.flag_reason,
            "created_at": c.created_at.isoformat(),
        }
        for c in chunks
    ]


@router.get("/pending-review")
async def pending_review(
    admin_key: str = "",
    db: AsyncSession = Depends(get_db),
):
    """All flagged video chunks awaiting admin review."""
    _require_admin(admin_key)
    result = await db.execute(
        select(VideoChunk, User, Game)
        .join(User, User.id == VideoChunk.user_id)
        .join(Game, Game.id == VideoChunk.game_id)
        .where(VideoChunk.flagged == True)
        .order_by(VideoChunk.created_at.desc())
    )
    rows = result.all()
    return [
        {
            "chunk_id": vc.id,
            "game_id": vc.game_id,
            "user_id": vc.user_id,
            "username": u.username,
            "chunk_number": vc.chunk_number,
            "file_path": vc.file_path,
            "flag_reason": vc.flag_reason,
            "bet_amount": g.bet_amount,
            "created_at": vc.created_at.isoformat(),
        }
        for vc, u, g in rows
    ]


@router.post("/{game_id}/flag/{user_id}")
async def flag_video(
    game_id: int,
    user_id: int,
    chunk_ids: list[int],
    reason: str,
    admin_key: str = "",
    db: AsyncSession = Depends(get_db),
):
    """Flag specific video chunks as suspicious (admin only)."""
    _require_admin(admin_key)
    result = await db.execute(
        select(VideoChunk).where(
            VideoChunk.id.in_(chunk_ids),
            VideoChunk.game_id == game_id,
            VideoChunk.user_id == user_id,
        )
    )
    chunks = result.scalars().all()
    for c in chunks:
        c.flagged = True
        c.flag_reason = reason
        db.add(c)
    await db.commit()
    return {"flagged_chunks": len(chunks)}


@router.post("/{game_id}/penalise/{user_id}")
async def penalise_player(
    game_id: int,
    user_id: int,
    penalty_amount: float = Form(0.0),
    ban_account: bool = Form(False),
    reason: str = Form("Video evidence of cheating"),
    admin_key: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """
    Apply a penalty after reviewing video evidence (admin only).

    - Deducts `penalty_amount` from the player's wallet.
    - Optionally bans the account permanently.
    - Notifies the player via WebSocket if they are connected.
    """
    _require_admin(admin_key)

    user_result = await db.execute(select(User).where(User.id == user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    wallet_result = await db.execute(select(Wallet).where(Wallet.user_id == user_id))
    wallet = wallet_result.scalar_one_or_none()

    actual_deducted = 0.0
    if penalty_amount > 0 and wallet:
        actual_deducted = min(penalty_amount, wallet.balance)
        wallet.balance = max(0.0, round(wallet.balance - actual_deducted, 2))
        db.add(Transaction(
            user_id=user_id,
            amount=-actual_deducted,
            type=TransactionType.WITHDRAWAL,
            game_id=game_id,
            description=f"Penalty: {reason[:100]}",
        ))
        db.add(wallet)

    if ban_account:
        user.is_banned = True
        user.ban_reason = f"Video evidence review: {reason[:200]}"

    penalty = Penalty(
        user_id=user_id,
        game_id=game_id,
        amount_deducted=actual_deducted,
        account_banned=ban_account,
        reason=reason,
        reviewed_by="admin",
    )
    db.add(user)
    db.add(penalty)
    await db.commit()

    # Notify player if online
    await manager.send_to_player(game_id, user_id, {
        "type": "penalty",
        "data": {
            "amount_deducted": actual_deducted,
            "banned": ban_account,
            "reason": reason,
        },
    })

    logger.warning("Penalty applied to user %d: ₹%.2f deducted, banned=%s, reason=%s",
                   user_id, actual_deducted, ban_account, reason)
    return {
        "user_id": user_id,
        "username": user.username,
        "amount_deducted": actual_deducted,
        "banned": ban_account,
        "reason": reason,
    }
