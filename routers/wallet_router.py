from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_active_unbanned_user
from config import MAX_WALLET_BALANCE as MAX_INVESTMENT_RUPEES
from database import get_db
from models import Transaction, TransactionType, User, Wallet
from schemas import DepositRequest, TransactionOut, WalletOut, WithdrawRequest

router = APIRouter(prefix="/wallet", tags=["Wallet"])


async def _get_wallet(user: User, db: AsyncSession) -> Wallet:
    result = await db.execute(select(Wallet).where(Wallet.user_id == user.id))
    wallet = result.scalar_one_or_none()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found.")
    return wallet


@router.get("/balance", response_model=WalletOut)
async def get_balance(
    user: User = Depends(get_active_unbanned_user),
    db: AsyncSession = Depends(get_db),
):
    """Get current wallet balance and total amount invested in active games."""
    return await _get_wallet(user, db)


@router.post("/deposit", response_model=WalletOut)
async def deposit(
    payload: DepositRequest,
    user: User = Depends(get_active_unbanned_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Deposit rupees into your wallet.
    Maximum wallet balance is capped at ₹100 at any time.
    """
    wallet = await _get_wallet(user, db)

    if wallet.balance + payload.amount > MAX_INVESTMENT_RUPEES:
        available = round(MAX_INVESTMENT_RUPEES - wallet.balance, 2)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Deposit would exceed the ₹{MAX_INVESTMENT_RUPEES} limit. "
                f"You can deposit at most ₹{available} right now."
            ),
        )

    wallet.balance = round(wallet.balance + payload.amount, 2)
    tx = Transaction(
        user_id=user.id,
        amount=payload.amount,
        type=TransactionType.DEPOSIT,
        description=f"Deposit of ₹{payload.amount}",
    )
    db.add(tx)
    await db.commit()
    await db.refresh(wallet)
    return wallet


@router.post("/withdraw", response_model=WalletOut)
async def withdraw(
    payload: WithdrawRequest,
    user: User = Depends(get_active_unbanned_user),
    db: AsyncSession = Depends(get_db),
):
    """Withdraw rupees from your wallet (only available/uninvested funds)."""
    wallet = await _get_wallet(user, db)

    available = round(wallet.balance - wallet.total_invested, 2)
    if payload.amount > available:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Insufficient available balance. Available: ₹{available} "
                f"(₹{wallet.total_invested} is locked in active games)."
            ),
        )

    wallet.balance = round(wallet.balance - payload.amount, 2)
    tx = Transaction(
        user_id=user.id,
        amount=payload.amount,
        type=TransactionType.WITHDRAWAL,
        description=f"Withdrawal of ₹{payload.amount}",
    )
    db.add(tx)
    await db.commit()
    await db.refresh(wallet)
    return wallet


@router.get("/transactions", response_model=list[TransactionOut])
async def list_transactions(
    limit: int = 50,
    user: User = Depends(get_active_unbanned_user),
    db: AsyncSession = Depends(get_db),
):
    """List last N wallet transactions."""
    result = await db.execute(
        select(Transaction)
        .where(Transaction.user_id == user.id)
        .order_by(Transaction.created_at.desc())
        .limit(limit)
    )
    return result.scalars().all()
