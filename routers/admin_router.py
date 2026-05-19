"""
Admin router – protected by ADMIN_SECRET header/query param.

Endpoints:
  GET  /admin/stats              – platform-wide metrics
  GET  /admin/users              – all users with wallet + flag count
  POST /admin/users/{id}/add-funds   – credit any amount to a wallet
  POST /admin/users/{id}/deduct-funds– deduct from wallet
  POST /admin/users/{id}/ban         – ban account
  POST /admin/users/{id}/unban       – unban account
  DELETE /admin/users/{id}           – delete user (optional force=true removes their games too)
  GET  /admin/games              – all games (paginated)
  GET  /admin/flags              – all anti-cheat flags
  GET  /admin/penalties          – all applied penalties
  GET  /admin/videos             – recorded video sessions (grouped by game + user)
  GET  /admin/videos/{id}/file   – download/stream one segment (WebM) for review
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import case, delete, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from config import ADMIN_SECRET
from database import get_db
from models import (
    AntiCheatFlag, DepositRequest, Game, GameStatus, Move, Penalty,
    PendingPayout, Transaction, TransactionType, User, VideoChunk, Wallet,
    WithdrawalRequest,
)
from video_evidence import payout_video_requirement_error, video_evidence_summary_for_admin

router = APIRouter(prefix="/admin", tags=["Admin"])
VIDEOS_STORAGE_ROOT = Path("videos").resolve()


def _auth(key: Optional[str]) -> None:
    # Constant-time comparison so an attacker cannot brute-force the admin key
    # via response-timing differences.
    if not key or not ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Invalid admin key.")
    import hmac
    if not hmac.compare_digest(key, ADMIN_SECRET):
        raise HTTPException(status_code=403, detail="Invalid admin key.")


# ── Stats ─────────────────────────────────────────────────────────────────────

@router.get("/stats")
async def stats(
    admin_key: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    _auth(admin_key)

    total_users     = (await db.execute(select(func.count(User.id)))).scalar_one()
    active_users    = (await db.execute(select(func.count(User.id)).where(User.is_active == True, User.is_banned == False))).scalar_one()
    banned_users    = (await db.execute(select(func.count(User.id)).where(User.is_banned == True))).scalar_one()
    total_games     = (await db.execute(select(func.count(Game.id)))).scalar_one()
    active_games    = (await db.execute(select(func.count(Game.id)).where(Game.status == GameStatus.ACTIVE))).scalar_one()
    waiting_games   = (await db.execute(select(func.count(Game.id)).where(Game.status == GameStatus.WAITING))).scalar_one()
    completed_games = (await db.execute(select(func.count(Game.id)).where(Game.status == GameStatus.COMPLETED))).scalar_one()
    total_flags     = (await db.execute(select(func.count(AntiCheatFlag.id)))).scalar_one()
    severe_flags    = (await db.execute(select(func.count(AntiCheatFlag.id)).where(AntiCheatFlag.severity == 3))).scalar_one()
    total_videos    = (await db.execute(select(func.count(VideoChunk.id)))).scalar_one()
    video_sessions  = (await db.execute(
        select(func.count()).select_from(
            select(VideoChunk.game_id, VideoChunk.user_id)
            .group_by(VideoChunk.game_id, VideoChunk.user_id)
            .subquery()
        )
    )).scalar_one()
    flagged_videos  = (await db.execute(select(func.count(VideoChunk.id)).where(VideoChunk.flagged == True))).scalar_one()

    # Total money in the system (sum of all wallet balances)
    total_balance   = (await db.execute(select(func.coalesce(func.sum(Wallet.balance), 0)))).scalar_one()
    total_invested  = (await db.execute(select(func.coalesce(func.sum(Wallet.total_invested), 0)))).scalar_one()

    # Total deposits ever
    dep_result = await db.execute(
        select(func.coalesce(func.sum(Transaction.amount), 0))
        .where(Transaction.type == TransactionType.DEPOSIT)
    )
    total_deposited = dep_result.scalar_one()

    # Total winnings paid
    win_result = await db.execute(
        select(func.coalesce(func.sum(Transaction.amount), 0))
        .where(Transaction.type == TransactionType.WIN)
    )
    total_winnings = win_result.scalar_one()

    return {
        "users": {
            "total": total_users,
            "active": active_users,
            "banned": banned_users,
        },
        "games": {
            "total": total_games,
            "active": active_games,
            "waiting": waiting_games,
            "completed": completed_games,
        },
        "money": {
            "total_in_wallets": round(float(total_balance), 2),
            "locked_in_games": round(float(total_invested), 2),
            "total_deposited": round(float(total_deposited), 2),
            "total_winnings_paid": round(float(total_winnings), 2),
        },
        "anticheat": {
            "total_flags": total_flags,
            "severe_flags": severe_flags,
            "video_chunks": total_videos,
            "video_sessions": video_sessions,
            "flagged_videos": flagged_videos,
        },
    }


# ── Users ─────────────────────────────────────────────────────────────────────

@router.get("/users")
async def list_users(
    admin_key: str = Query(""),
    search: str = Query(""),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    _auth(admin_key)

    q = select(User, Wallet).outerjoin(Wallet, Wallet.user_id == User.id)
    if search:
        q = q.where(User.username.ilike(f"%{search}%") | User.email.ilike(f"%{search}%"))
    q = q.order_by(User.created_at.desc()).offset((page - 1) * per_page).limit(per_page)

    rows = (await db.execute(q)).all()

    # Flag counts per user
    flag_result = await db.execute(
        select(AntiCheatFlag.user_id, func.count(AntiCheatFlag.id).label("cnt"))
        .group_by(AntiCheatFlag.user_id)
    )
    flag_map = {r.user_id: r.cnt for r in flag_result}

    total = (await db.execute(select(func.count(User.id)))).scalar_one()

    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "users": [
            {
                "id": u.id,
                "username": u.username,
                "email": u.email,
                "is_active": u.is_active,
                "is_banned": u.is_banned,
                "ban_reason": u.ban_reason,
                "games_played": u.games_played,
                "games_won": u.games_won,
                "total_earned": round(u.total_earned, 2),
                "balance": round(w.balance, 2) if w else 0.0,
                "locked": round(w.total_invested, 2) if w else 0.0,
                "flag_count": flag_map.get(u.id, 0),
                "created_at": u.created_at.isoformat(),
            }
            for u, w in rows
        ],
    }


@router.post("/users/{user_id}/add-funds")
async def add_funds(
    user_id: int,
    amount: float = Query(..., gt=0),
    note: str = Query("Admin credit"),
    admin_key: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    """Credit any amount to a user's wallet (admin only)."""
    _auth(admin_key)

    wallet_r = await db.execute(select(Wallet).where(Wallet.user_id == user_id))
    wallet = wallet_r.scalar_one_or_none()
    if not wallet:
        raise HTTPException(status_code=404, detail="User / wallet not found.")

    wallet.balance = round(wallet.balance + amount, 2)
    db.add(Transaction(
        user_id=user_id,
        amount=amount,
        type=TransactionType.DEPOSIT,
        description=f"Admin credit: {note}",
    ))
    await db.commit()
    return {"user_id": user_id, "new_balance": wallet.balance, "credited": amount}


@router.post("/users/{user_id}/deduct-funds")
async def deduct_funds(
    user_id: int,
    amount: float = Query(..., gt=0),
    note: str = Query("Admin deduction"),
    admin_key: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    """Deduct funds from a user's wallet (admin only)."""
    _auth(admin_key)

    wallet_r = await db.execute(select(Wallet).where(Wallet.user_id == user_id))
    wallet = wallet_r.scalar_one_or_none()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found.")

    deducted = min(amount, wallet.balance)
    wallet.balance = max(0.0, round(wallet.balance - deducted, 2))
    db.add(Transaction(
        user_id=user_id,
        amount=deducted,
        type=TransactionType.WITHDRAWAL,
        description=f"Admin deduction: {note}",
    ))
    await db.commit()
    return {"user_id": user_id, "new_balance": wallet.balance, "deducted": deducted}


@router.post("/users/{user_id}/ban")
async def ban_user(
    user_id: int,
    reason: str = Query("Admin action"),
    admin_key: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    _auth(admin_key)
    user_r = await db.execute(select(User).where(User.id == user_id))
    user = user_r.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    user.is_banned = True
    user.ban_reason = reason
    await db.commit()
    return {"user_id": user_id, "banned": True, "reason": reason}


@router.post("/users/{user_id}/unban")
async def unban_user(
    user_id: int,
    admin_key: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    _auth(admin_key)
    user_r = await db.execute(select(User).where(User.id == user_id))
    user = user_r.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    user.is_banned = False
    user.ban_reason = None
    await db.commit()
    return {"user_id": user_id, "banned": False}


async def _delete_games_cascade(db: AsyncSession, game_ids: list[int]) -> None:
    if not game_ids:
        return
    await db.execute(delete(Move).where(Move.game_id.in_(game_ids)))
    await db.execute(delete(AntiCheatFlag).where(AntiCheatFlag.game_id.in_(game_ids)))
    await db.execute(delete(VideoChunk).where(VideoChunk.game_id.in_(game_ids)))
    await db.execute(delete(PendingPayout).where(PendingPayout.game_id.in_(game_ids)))
    await db.execute(delete(Transaction).where(Transaction.game_id.in_(game_ids)))
    await db.execute(delete(Game).where(Game.id.in_(game_ids)))


async def _release_solo_waiting_bets(db: AsyncSession, games: list[Game]) -> None:
    """If we're deleting a solo waiting lobby, unlock the creator's total_invested."""
    for g in games:
        if g.status == GameStatus.WAITING and g.black_player_id is None:
            w_r = await db.execute(select(Wallet).where(Wallet.user_id == g.white_player_id))
            wallet = w_r.scalar_one_or_none()
            if wallet:
                wallet.total_invested = max(0.0, round(wallet.total_invested - g.bet_amount, 2))
                db.add(wallet)


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    admin_key: str = Query(""),
    force: bool = Query(
        False,
        description="If true, deletes all games this user joined (affects opponents' history too).",
    ),
    db: AsyncSession = Depends(get_db),
):
    """
    Remove a user so they can sign up again with the same username/email.

    Without force: only allowed if the user has no games with another player
    (you may still have solo 'waiting' lobbies — those games are removed).

    With force: deletes every game they were part of and all related rows.
    """
    _auth(admin_key)

    user_r = await db.execute(select(User).where(User.id == user_id))
    user = user_r.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    g_r = await db.execute(
        select(Game).where(
            (Game.white_player_id == user_id)
            | (Game.black_player_id == user_id)
            | (Game.winner_id == user_id)
        )
    )
    games = g_r.scalars().all()

    def _blocks_soft_delete(g: Game) -> bool:
        if g.black_player_id is not None:
            return True
        if g.status != GameStatus.WAITING:
            return True
        return False

    blocking = [g for g in games if _blocks_soft_delete(g)]
    solo_waiting = [g for g in games if not _blocks_soft_delete(g)]

    if blocking and not force:
        raise HTTPException(
            status_code=400,
            detail=(
                "This player has games with another person (or non-waiting games). "
                "Delete with query parameter force=true to remove those games entirely "
                "(this also deletes history for their opponents for those games)."
            ),
        )

    ids_to_cascade = [g.id for g in (games if force else solo_waiting)]
    games_to_remove = games if force else solo_waiting
    await _release_solo_waiting_bets(db, games_to_remove)
    await _delete_games_cascade(db, ids_to_cascade)

    await db.execute(delete(PendingPayout).where(PendingPayout.user_id == user_id))
    await db.execute(delete(DepositRequest).where(DepositRequest.user_id == user_id))
    await db.execute(delete(Penalty).where(Penalty.user_id == user_id))
    await db.execute(delete(AntiCheatFlag).where(AntiCheatFlag.user_id == user_id))
    await db.execute(delete(VideoChunk).where(VideoChunk.user_id == user_id))
    await db.execute(delete(Transaction).where(Transaction.user_id == user_id))
    await db.execute(delete(Wallet).where(Wallet.user_id == user_id))
    await db.execute(delete(User).where(User.id == user_id))
    await db.commit()

    return {
        "deleted": True,
        "user_id": user_id,
        "games_removed": len(ids_to_cascade),
        "forced": force,
    }


# ── Games ────────────────────────────────────────────────────────────────────

@router.get("/games")
async def list_games(
    admin_key: str = Query(""),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25),
    db: AsyncSession = Depends(get_db),
):
    _auth(admin_key)

    q = select(Game, User).join(User, User.id == Game.white_player_id)
    if status:
        try:
            q = q.where(Game.status == GameStatus(status))
        except ValueError:
            pass
    q = q.order_by(Game.created_at.desc()).offset((page-1)*per_page).limit(per_page)
    rows = (await db.execute(q)).all()
    total = (await db.execute(select(func.count(Game.id)))).scalar_one()

    return {
        "total": total,
        "games": [
            {
                "id": g.id,
                "white": u.username,
                "white_id": g.white_player_id,
                "black_id": g.black_player_id,
                "status": g.status.value,
                "bet_amount": g.bet_amount,
                "winner_id": g.winner_id,
                "result": g.result,
                "created_at": g.created_at.isoformat(),
                "ended_at": g.ended_at.isoformat() if g.ended_at else None,
            }
            for g, u in rows
        ],
    }


# ── Anti-cheat flags ──────────────────────────────────────────────────────────

@router.get("/flags")
async def list_flags(
    admin_key: str = Query(""),
    severity: Optional[int] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50),
    db: AsyncSession = Depends(get_db),
):
    _auth(admin_key)

    q = select(AntiCheatFlag, User).join(User, User.id == AntiCheatFlag.user_id)
    if severity:
        q = q.where(AntiCheatFlag.severity == severity)
    q = q.order_by(AntiCheatFlag.created_at.desc()).offset((page-1)*per_page).limit(per_page)
    rows = (await db.execute(q)).all()
    total = (await db.execute(select(func.count(AntiCheatFlag.id)))).scalar_one()

    return {
        "total": total,
        "flags": [
            {
                "id": f.id,
                "game_id": f.game_id,
                "user_id": f.user_id,
                "username": u.username,
                "flag_type": f.flag_type.value,
                "description": f.description,
                "severity": f.severity,
                "created_at": f.created_at.isoformat(),
            }
            for f, u in rows
        ],
    }


# ── Video chunks ──────────────────────────────────────────────────────────────

@router.get("/videos/retention")
async def video_retention_status(
    admin_key: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    """Report retention policy + how many chunks are currently older than it."""
    _auth(admin_key)
    from datetime import timedelta
    from video_retention import VIDEO_RETENTION_DAYS, VIDEO_RETENTION_SWEEP_INTERVAL_HOURS

    cutoff = datetime.utcnow() - timedelta(days=VIDEO_RETENTION_DAYS)
    expired = (
        await db.execute(
            select(func.count(VideoChunk.id)).where(VideoChunk.created_at < cutoff)
        )
    ).scalar_one()
    total = (await db.execute(select(func.count(VideoChunk.id)))).scalar_one()
    oldest = (
        await db.execute(select(func.min(VideoChunk.created_at)))
    ).scalar_one()
    return {
        "retention_days": VIDEO_RETENTION_DAYS,
        "sweep_interval_hours": VIDEO_RETENTION_SWEEP_INTERVAL_HOURS,
        "total_chunks": int(total),
        "expired_chunks": int(expired),
        "oldest_chunk_created_at": oldest.isoformat() if oldest else None,
    }


@router.post("/videos/retention/run")
async def run_video_retention(
    admin_key: str = Query(""),
):
    """Manually trigger a retention sweep right now."""
    _auth(admin_key)
    from video_retention import purge_expired_videos, VIDEO_RETENTION_DAYS
    files, rows = await purge_expired_videos()
    return {
        "files_deleted": files,
        "db_rows_deleted": rows,
        "retention_days": VIDEO_RETENTION_DAYS,
    }


@router.get("/videos")
async def list_videos(
    admin_key: str = Query(""),
    flagged_only: bool = Query(False),
    page: int = Query(1, ge=1),
    per_page: int = Query(50),
    db: AsyncSession = Depends(get_db),
):
    _auth(admin_key)

    flag_sum = func.sum(case((VideoChunk.flagged.is_(True), 1), else_=0))

    agg = (
        select(
            VideoChunk.game_id,
            VideoChunk.user_id,
            func.count(VideoChunk.id).label("segments"),
            func.coalesce(func.sum(VideoChunk.file_size_bytes), 0).label("total_bytes"),
            func.min(VideoChunk.created_at).label("session_start"),
            func.max(VideoChunk.created_at).label("session_end"),
            flag_sum.label("flag_count"),
        )
        .group_by(VideoChunk.game_id, VideoChunk.user_id)
    )
    if flagged_only:
        agg = agg.having(flag_sum > 0)

    agg_sub = agg.subquery()

    total = (await db.execute(select(func.count()).select_from(agg_sub))).scalar_one()
    total_chunks = (await db.execute(select(func.count(VideoChunk.id)))).scalar_one()

    page_stmt = (
        select(
            agg_sub.c.game_id,
            agg_sub.c.user_id,
            agg_sub.c.segments,
            agg_sub.c.total_bytes,
            agg_sub.c.session_start,
            agg_sub.c.session_end,
            agg_sub.c.flag_count,
            User.username,
            Game.bet_amount,
        )
        .join(User, User.id == agg_sub.c.user_id)
        .join(Game, Game.id == agg_sub.c.game_id)
        .order_by(agg_sub.c.session_end.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    page_rows = (await db.execute(page_stmt)).all()

    pairs = [(r.game_id, r.user_id) for r in page_rows]
    by_pair: dict[tuple[int, int], list[VideoChunk]] = {}
    if pairs:
        chunks_all = (
            (
                await db.execute(
                    select(VideoChunk).where(
                        tuple_(VideoChunk.game_id, VideoChunk.user_id).in_(pairs)
                    )
                )
            )
            .scalars()
            .all()
        )
        for vc in chunks_all:
            by_pair.setdefault((vc.game_id, vc.user_id), []).append(vc)
        for key in by_pair:
            by_pair[key].sort(key=lambda c: (c.chunk_number, c.id))

    sessions: list[dict] = []
    for r in page_rows:
        vcs = by_pair.get((r.game_id, r.user_id), [])
        chunk_ids = [vc.id for vc in vcs]
        flagged_any = any(vc.flagged for vc in vcs)
        fr = next(
            (vc.flag_reason for vc in vcs if vc.flagged and vc.flag_reason),
            None,
        )
        file_paths = [vc.file_path for vc in vcs]
        sessions.append(
            {
                "chunk_ids": chunk_ids,
                "primary_chunk_id": chunk_ids[-1] if chunk_ids else None,
                "game_id": r.game_id,
                "user_id": r.user_id,
                "username": r.username,
                "segments": int(r.segments),
                "total_size_kb": round(float(r.total_bytes or 0) / 1024, 1),
                "session_start": r.session_start.isoformat() if r.session_start else None,
                "session_end": r.session_end.isoformat() if r.session_end else None,
                "bet_amount": float(r.bet_amount),
                "flagged": flagged_any,
                "flag_reason": fr,
                "file_paths": file_paths,
            }
        )

    return {
        "total": total,
        "total_chunks": total_chunks,
        "sessions": sessions,
    }


@router.get("/videos/{chunk_id}/file")
async def stream_video_chunk(
    chunk_id: int,
    admin_key: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    """
    Stream or download a single recorded WebM chunk for manual review.
    Files live under ./videos/{game_id}/{user_id}/ on the server by default.
    """
    _auth(admin_key)
    vc_r = await db.execute(select(VideoChunk).where(VideoChunk.id == chunk_id))
    vc = vc_r.scalar_one_or_none()
    if not vc:
        raise HTTPException(status_code=404, detail="Chunk not found.")
    root = VIDEOS_STORAGE_ROOT
    candidates: list[Path] = []
    raw = Path(vc.file_path)
    if raw.is_absolute():
        candidates.append(raw.resolve())
    else:
        candidates.append((Path.cwd() / raw).resolve())
    candidates.append(
        root / str(vc.game_id) / str(vc.user_id) / f"chunk_{vc.chunk_number:04d}.webm"
    )
    path: Optional[Path] = None
    for cand in candidates:
        try:
            rp = cand.resolve()
            rp.relative_to(root)
            if rp.is_file():
                path = rp
                break
        except ValueError:
            continue
    if path is None:
        raise HTTPException(
            status_code=404,
            detail="Recording file missing on disk (path may differ after deploy or cleanup).",
        )
    return FileResponse(
        path,
        media_type="video/webm",
        filename=f"game{vc.game_id}_user{vc.user_id}_chunk{vc.chunk_number:04d}.webm",
        content_disposition_type="inline",
    )


# ── Deposit requests ──────────────────────────────────────────────────────────

@router.get("/deposits")
async def list_deposits(
    admin_key: str = Query(""),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
):
    """List all UPI deposit requests (pending ones need manual verification)."""
    _auth(admin_key)
    q = select(DepositRequest, User).join(User, User.id == DepositRequest.user_id)
    if status:
        q = q.where(DepositRequest.status == status)
    q = q.order_by(DepositRequest.created_at.desc()).offset((page-1)*25).limit(25)
    rows = (await db.execute(q)).all()
    total = (await db.execute(select(func.count(DepositRequest.id)))).scalar_one()
    return {
        "total": total,
        "deposits": [
            {
                "id": d.id, "user_id": d.user_id, "username": u.username,
                "amount": d.amount, "utr_number": d.utr_number,
                "upi_id_paid_to": d.upi_id_paid_to,
                "screenshot_path": d.screenshot_path,
                "status": d.status, "rejection_reason": d.rejection_reason,
                "created_at": d.created_at.isoformat(),
                "reviewed_at": d.reviewed_at.isoformat() if d.reviewed_at else None,
            }
            for d, u in rows
        ],
    }


@router.post("/deposits/{deposit_id}/approve")
async def approve_deposit(
    deposit_id: int,
    admin_key: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    """Approve a UPI deposit after verifying the UTR number. Credits wallet immediately."""
    _auth(admin_key)
    dep_r = await db.execute(select(DepositRequest).where(DepositRequest.id == deposit_id))
    dep = dep_r.scalar_one_or_none()
    if not dep:
        raise HTTPException(status_code=404, detail="Deposit request not found.")
    if dep.status != "pending":
        raise HTTPException(status_code=400, detail=f"Already {dep.status}.")

    wallet_r = await db.execute(select(Wallet).where(Wallet.user_id == dep.user_id))
    wallet = wallet_r.scalar_one_or_none()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found.")

    wallet.balance = round(wallet.balance + dep.amount, 2)
    dep.status = "approved"
    dep.reviewed_at = datetime.utcnow()
    dep.reviewed_by = "admin"
    db.add(Transaction(
        user_id=dep.user_id, amount=dep.amount,
        type=TransactionType.DEPOSIT,
        description=f"UPI Deposit approved – UTR {dep.utr_number}",
    ))
    await db.commit()
    return {"status": "approved", "credited": dep.amount, "user_id": dep.user_id}


@router.post("/deposits/{deposit_id}/reject")
async def reject_deposit(
    deposit_id: int,
    reason: str = Query("UTR not verified"),
    admin_key: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    """Reject a deposit request (UTR invalid / duplicate)."""
    _auth(admin_key)
    dep_r = await db.execute(select(DepositRequest).where(DepositRequest.id == deposit_id))
    dep = dep_r.scalar_one_or_none()
    if not dep:
        raise HTTPException(status_code=404, detail="Not found.")
    dep.status = "rejected"
    dep.rejection_reason = reason
    dep.reviewed_at = datetime.utcnow()
    dep.reviewed_by = "admin"
    await db.commit()
    return {"status": "rejected", "reason": reason}


# ── Pending payouts ───────────────────────────────────────────────────────────

@router.get("/payouts")
async def list_payouts(
    admin_key: str = Query(""),
    status: Optional[str] = Query("pending"),
    db: AsyncSession = Depends(get_db),
):
    """List pending payouts awaiting video review."""
    _auth(admin_key)
    q = select(PendingPayout, User, Game)\
        .join(User, User.id == PendingPayout.user_id)\
        .join(Game, Game.id == PendingPayout.game_id)
    if status:
        q = q.where(PendingPayout.status == status)
    q = q.order_by(PendingPayout.created_at.desc()).limit(100)
    rows = (await db.execute(q)).all()
    total = (await db.execute(select(func.count(PendingPayout.id)))).scalar_one()

    payouts_out = []
    for p, u, g in rows:
        vsum = await video_evidence_summary_for_admin(db, g)
        payouts_out.append({
            "id": p.id,
            "user_id": p.user_id,
            "username": u.username,
            "game_id": p.game_id,
            "bet_amount": g.bet_amount,
            "is_vs_cpu": g.is_vs_cpu,
            "gross_amount": p.gross_amount,
            "platform_fee": p.platform_fee,
            "net_amount": p.net_amount,
            "status": p.status,
            "rejection_reason": p.rejection_reason,
            "penalty_amount": p.penalty_amount,
            "auto_release_at": p.auto_release_at.isoformat() if p.auto_release_at else None,
            "created_at": p.created_at.isoformat(),
            **vsum,
        })

    return {
        "total": total,
        "payouts": payouts_out,
    }


@router.post("/payouts/{payout_id}/approve")
async def approve_payout(
    payout_id: int,
    admin_key: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    """Release winnings to the player after video review passes."""
    _auth(admin_key)
    pay_r = await db.execute(select(PendingPayout).where(PendingPayout.id == payout_id))
    pay = pay_r.scalar_one_or_none()
    if not pay:
        raise HTTPException(status_code=404, detail="Payout not found.")
    if pay.status != "pending":
        raise HTTPException(status_code=400, detail=f"Already {pay.status}.")

    game_r = await db.execute(select(Game).where(Game.id == pay.game_id))
    game = game_r.scalar_one_or_none()
    if game:
        block = await payout_video_requirement_error(db, game)
        if block:
            raise HTTPException(status_code=400, detail=block)

    wallet_r = await db.execute(select(Wallet).where(Wallet.user_id == pay.user_id))
    wallet = wallet_r.scalar_one_or_none()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found.")

    user_r = await db.execute(select(User).where(User.id == pay.user_id))
    user = user_r.scalar_one()

    wallet.balance = round(wallet.balance + pay.net_amount, 2)
    user.total_earned = round(user.total_earned + pay.net_amount, 2)
    pay.status = "approved"
    pay.reviewed_at = datetime.utcnow()
    pay.reviewed_by = "admin"

    db.add(Transaction(
        user_id=pay.user_id, amount=pay.net_amount,
        type=TransactionType.WIN, game_id=pay.game_id,
        description=f"Winnings released – Game #{pay.game_id} (fee ₹{pay.platform_fee})",
    ))
    await db.commit()
    return {"status": "approved", "released": pay.net_amount, "user_id": pay.user_id}


@router.post("/payouts/{payout_id}/reject")
async def reject_payout(
    payout_id: int,
    reason: str = Query("Cheating detected via video review"),
    penalty_amount: float = Query(0.0, ge=0),
    ban: bool = Query(False),
    admin_key: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    """
    Reject a payout (cheating found in video review).
    Optionally deduct a penalty from remaining wallet balance and ban account.
    """
    _auth(admin_key)
    pay_r = await db.execute(select(PendingPayout).where(PendingPayout.id == payout_id))
    pay = pay_r.scalar_one_or_none()
    if not pay:
        raise HTTPException(status_code=404, detail="Payout not found.")
    if pay.status != "pending":
        raise HTTPException(status_code=400, detail=f"Already {pay.status}.")

    pay.status = "rejected"
    pay.rejection_reason = reason
    pay.reviewed_at = datetime.utcnow()
    pay.reviewed_by = "admin"

    user_r  = await db.execute(select(User).where(User.id == pay.user_id))
    wallet_r = await db.execute(select(Wallet).where(Wallet.user_id == pay.user_id))
    user   = user_r.scalar_one()
    wallet = wallet_r.scalar_one_or_none()

    actual_penalty = 0.0
    if penalty_amount > 0 and wallet:
        actual_penalty = min(penalty_amount, wallet.balance)
        wallet.balance = max(0.0, round(wallet.balance - actual_penalty, 2))
        pay.penalty_amount = actual_penalty
        db.add(Transaction(
            user_id=pay.user_id, amount=-actual_penalty,
            type=TransactionType.WITHDRAWAL, game_id=pay.game_id,
            description=f"Penalty: {reason[:100]}",
        ))

    if ban:
        user.is_banned = True
        user.ban_reason = f"Video review: {reason[:200]}"

    db.add(pay); db.add(user)
    if wallet: db.add(wallet)

    from models import Penalty as PenaltyModel
    db.add(PenaltyModel(
        user_id=pay.user_id, game_id=pay.game_id,
        amount_deducted=actual_penalty,
        account_banned=ban, reason=reason, reviewed_by="admin",
    ))
    await db.commit()
    return {
        "status": "rejected",
        "payout_withheld": pay.net_amount,
        "penalty_deducted": actual_penalty,
        "banned": ban,
        "reason": reason,
    }


# ── Player withdrawals (UPI / GPay) ───────────────────────────────────────────


@router.get("/withdrawals")
async def list_withdrawals(
    admin_key: str = Query(""),
    status: Optional[str] = Query(None, description="Filter: pending, completed, rejected"),
    db: AsyncSession = Depends(get_db),
):
    """Queue of wallet withdrawals — player wallet already debited; pay destination_upi."""
    _auth(admin_key)
    q = (
        select(WithdrawalRequest, User)
        .join(User, User.id == WithdrawalRequest.user_id)
        .order_by(WithdrawalRequest.created_at.desc())
        .limit(200)
    )
    if status:
        q = q.where(WithdrawalRequest.status == status)
    rows = (await db.execute(q)).all()
    return {
        "withdrawals": [
            {
                "id": w.id,
                "user_id": w.user_id,
                "username": u.username,
                "amount": w.amount,
                "destination_upi": w.destination_upi,
                "status": w.status,
                "transaction_id": w.transaction_id,
                "created_at": w.created_at.isoformat(),
                "reviewed_at": w.reviewed_at.isoformat() if w.reviewed_at else None,
                "rejection_reason": w.rejection_reason,
            }
            for w, u in rows
        ]
    }


@router.post("/withdrawals/{withdrawal_id}/complete")
async def complete_withdrawal(
    withdrawal_id: int,
    admin_key: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    """Mark withdrawal as paid to the player's UPI (after you sent GPay/bank)."""
    _auth(admin_key)
    wr_r = await db.execute(
        select(WithdrawalRequest).where(WithdrawalRequest.id == withdrawal_id)
    )
    wr = wr_r.scalar_one_or_none()
    if not wr:
        raise HTTPException(status_code=404, detail="Withdrawal not found.")
    if wr.status != "pending":
        raise HTTPException(status_code=400, detail=f"Already {wr.status}.")
    wr.status = "completed"
    wr.reviewed_at = datetime.utcnow()
    wr.reviewed_by = "admin"
    await db.commit()
    return {"status": "completed", "id": wr.id}


@router.post("/withdrawals/{withdrawal_id}/reject")
async def reject_withdrawal(
    withdrawal_id: int,
    admin_key: str = Query(""),
    reason: str = Query("Could not process withdrawal — refunded to wallet"),
    db: AsyncSession = Depends(get_db),
):
    """Cancel a pending withdrawal and refund the amount to the player's wallet."""
    _auth(admin_key)
    wr_r = await db.execute(
        select(WithdrawalRequest).where(WithdrawalRequest.id == withdrawal_id)
    )
    wr = wr_r.scalar_one_or_none()
    if not wr:
        raise HTTPException(status_code=404, detail="Withdrawal not found.")
    if wr.status != "pending":
        raise HTTPException(status_code=400, detail=f"Already {wr.status}.")

    wallet_r = await db.execute(select(Wallet).where(Wallet.user_id == wr.user_id))
    wallet = wallet_r.scalar_one_or_none()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found.")

    wallet.balance = round(wallet.balance + wr.amount, 2)
    wr.status = "rejected"
    wr.rejection_reason = reason
    wr.reviewed_at = datetime.utcnow()
    wr.reviewed_by = "admin"
    db.add(wallet)
    db.add(
        Transaction(
            user_id=wr.user_id,
            amount=wr.amount,
            type=TransactionType.REFUND,
            description=f"Withdrawal #{wr.id} cancelled — {reason[:120]}",
        )
    )
    await db.commit()
    return {"status": "rejected", "refunded": wr.amount, "user_id": wr.user_id}


# ── Platform revenue summary ───────────────────────────────────────────────────

@router.get("/revenue")
async def revenue_summary(
    admin_key: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    """
    Total platform revenue from all sources:

      - Platform fees on approved payouts (PendingPayout.platform_fee where status='approved')
      - Net amounts on rejected payouts (forfeited winnings kept by platform)
      - Penalties applied via video review
      - Direct revenue retained at game-settle time (Game.platform_revenue) —
        primarily vs-CPU losses where the full bet becomes platform income.
    """
    _auth(admin_key)
    from sqlalchemy import func as f
    approved = await db.execute(
        select(f.coalesce(f.sum(PendingPayout.platform_fee), 0))
        .where(PendingPayout.status == "approved")
    )
    withheld = await db.execute(
        select(f.coalesce(f.sum(PendingPayout.net_amount), 0))
        .where(PendingPayout.status == "rejected")
    )
    penalties = await db.execute(
        select(f.coalesce(f.sum(PendingPayout.penalty_amount), 0))
    )
    cpu_losses = await db.execute(
        select(f.coalesce(f.sum(Game.platform_revenue), 0))
        .where(Game.status == GameStatus.COMPLETED)
    )
    total_fees = float(approved.scalar_one())
    total_withheld = float(withheld.scalar_one())
    total_penalties = float(penalties.scalar_one())
    total_cpu = float(cpu_losses.scalar_one())
    return {
        "platform_fees_collected": round(total_fees, 2),
        "payouts_withheld": round(total_withheld, 2),
        "penalties_collected": round(total_penalties, 2),
        "cpu_game_revenue": round(total_cpu, 2),
        "total_revenue": round(total_fees + total_withheld + total_penalties + total_cpu, 2),
    }


# ── Penalties ─────────────────────────────────────────────────────────────────

@router.get("/penalties")
async def list_penalties(
    admin_key: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    _auth(admin_key)
    q = select(Penalty, User).join(User, User.id == Penalty.user_id)\
        .order_by(Penalty.created_at.desc()).limit(100)
    rows = (await db.execute(q)).all()
    return [
        {
            "id": p.id,
            "user_id": p.user_id,
            "username": u.username,
            "game_id": p.game_id,
            "amount_deducted": p.amount_deducted,
            "account_banned": p.account_banned,
            "reason": p.reason,
            "reviewed_by": p.reviewed_by,
            "created_at": p.created_at.isoformat(),
        }
        for p, u in rows
    ]
