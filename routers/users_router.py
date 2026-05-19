import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user
from config import ALGORITHM, SECRET_KEY
from database import AsyncSessionLocal, get_db
from models import AntiCheatFlag, User
from schemas import AntiCheatFlagOut, UserPublic
from websocket_manager import manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["Users"])


@router.get("/{user_id}", response_model=UserPublic)
async def get_user(user_id: int, db: AsyncSession = Depends(get_db)):
    """Get public profile of any player."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    return user


@router.get("/me/flags", response_model=list[AntiCheatFlagOut])
async def my_flags(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """View anti-cheat flags on your own account."""
    result = await db.execute(
        select(AntiCheatFlag)
        .where(AntiCheatFlag.user_id == user.id)
        .order_by(AntiCheatFlag.created_at.desc())
    )
    return result.scalars().all()


@router.websocket("/me/ws")
async def user_notifications_ws(
    websocket: WebSocket,
    token: str = Query(..., description="JWT access token"),
):
    """
    Per-user notification channel.

    Pushes:
      - ``wallet_update``     { balance } whenever the server credits / debits
        the user's wallet (deposit approved, payout approved, withdrawal
        rejected/refunded, admin add/deduct funds, etc.).
      - ``deposit_approved``  { amount, deposit_id, message }
      - ``deposit_rejected``  { deposit_id, reason }
      - ``withdrawal_completed`` / ``withdrawal_rejected``
      - ``payout_approved`` / ``payout_rejected``
      - ``account_banned``

    Frontend uses these to refresh balance + show toasts without polling.
    """
    user_id: int | None = None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        sub = payload.get("sub")
        if sub is None:
            await websocket.close(code=4001)
            return
        user_id = int(sub)
    except (JWTError, ValueError):
        await websocket.close(code=4001)
        return

    async with AsyncSessionLocal() as db:
        u = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if not u or not u.is_active:
            await websocket.close(code=4001)
            return

    await manager.connect_user(websocket, user_id)
    try:
        await websocket.send_json({"type": "ready", "data": {"user_id": user_id}})
        while True:
            # We only care about keepalives — drain whatever the client sends.
            try:
                msg = await asyncio.wait_for(websocket.receive_text(), timeout=60)
            except asyncio.TimeoutError:
                # Idle heartbeat from server to keep proxies from killing the socket.
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    break
                continue
            if msg and msg.lower().startswith("ping"):
                try:
                    await websocket.send_json({"type": "pong"})
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.info("User notification socket %d closed: %s", user_id, e)
    finally:
        manager.disconnect_user(user_id, websocket)
