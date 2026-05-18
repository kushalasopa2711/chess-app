from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import create_access_token, hash_password, verify_password, get_current_user
from database import get_db
from models import User, Wallet
from schemas import Token, UserLogin, UserRegister, UserProfile

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/register", response_model=UserProfile, status_code=201)
async def register(payload: UserRegister, db: AsyncSession = Depends(get_db)):
    """Create a new player account. A wallet is automatically created with ₹0 balance."""
    # Check uniqueness
    existing = await db.execute(
        select(User).where(
            (User.username == payload.username) | (User.email == payload.email)
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username or email already registered.",
        )

    user = User(
        username=payload.username,
        email=payload.email,
        hashed_password=hash_password(payload.password),
    )
    db.add(user)
    await db.flush()  # get user.id

    wallet = Wallet(user_id=user.id, balance=0.0, total_invested=0.0)
    db.add(wallet)
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/login", response_model=Token)
async def login(payload: UserLogin, db: AsyncSession = Depends(get_db)):
    """Login and receive a JWT bearer token."""
    result = await db.execute(select(User).where(User.username == payload.username))
    user = result.scalar_one_or_none()

    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password.",
        )
    if not user.is_active:
        raise HTTPException(status_code=400, detail="Account is deactivated.")
    if user.is_banned:
        raise HTTPException(
            status_code=403,
            detail=f"Account banned: {user.ban_reason or 'Anti-cheat violation'}",
        )

    token = create_access_token(user.id)
    return Token(access_token=token)


@router.get("/me", response_model=UserProfile)
async def me(user: User = Depends(get_current_user)):
    """Get the authenticated user's profile."""
    return user
