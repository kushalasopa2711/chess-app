from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user
from database import get_db
from models import AntiCheatFlag, User
from schemas import AntiCheatFlagOut, UserPublic

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
