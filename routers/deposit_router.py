"""
UPI Deposit Router
==================
Flow:
  1. Player calls GET /deposit/upi-info?amount=50
     → gets UPI ID, QR link, and payment instructions
  2. Player pays via Google Pay / PhonePe
  3. Player calls POST /deposit/request with {amount, utr_number}
  4. Admin sees it in dashboard → approves (credits wallet) or rejects

No money is credited until admin manually verifies the UTR.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional
from pathlib import Path

import aiofiles
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_active_unbanned_user
from config import MAX_WALLET_BALANCE, MIN_BET, UPI_ID, UPI_NAME, UPI_NOTE
from database import get_db
from models import DepositRequest, Transaction, TransactionType, User, Wallet

router = APIRouter(prefix="/deposit", tags=["UPI Deposits"])
logger = logging.getLogger(__name__)

SCREENSHOTS_DIR = Path("screenshots")
SCREENSHOTS_DIR.mkdir(exist_ok=True)

MAX_DEPOSIT = 100.0
MIN_DEPOSIT = 10.0


@router.get("/upi-info")
async def upi_info(amount: float = Query(50.0, ge=10, le=100)):
    """
    Returns the UPI payment details and deep-link URL for the requested amount.
    The frontend uses this to generate a QR code.
    """
    amount = round(amount, 2)
    upi_url = (
        f"upi://pay?pa={UPI_ID}&pn={UPI_NAME}"
        f"&am={amount}&cu=INR&tn={UPI_NOTE}"
    )
    return {
        "upi_id":   UPI_ID,
        "upi_name": UPI_NAME,
        "amount":   amount,
        "upi_url":  upi_url,
        "instructions": [
            f"1. Open Google Pay or PhonePe on your phone",
            f"2. Scan the QR code OR send ₹{amount} to UPI ID: {UPI_ID}",
            f"3. After payment, copy the UTR / transaction ID from your payment app",
            f"4. Enter it below and submit your deposit request",
            f"5. Your wallet will be credited within a few hours after admin verification",
        ],
    }


@router.post("/request", status_code=201)
async def submit_deposit_request(
    amount:      float = Form(..., ge=10, le=100),
    utr_number:  str   = Form(..., min_length=6, max_length=50),
    screenshot:  Optional[UploadFile] = File(None),
    user: User = Depends(get_active_unbanned_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Submit a deposit request after paying via UPI.
    Attach an optional payment screenshot for faster verification.
    """
    amount = round(amount, 2)
    if amount < MIN_DEPOSIT:
        raise HTTPException(status_code=400, detail=f"Minimum deposit is ₹{MIN_DEPOSIT}.")
    if amount > MAX_DEPOSIT:
        raise HTTPException(status_code=400, detail=f"Maximum deposit is ₹{MAX_DEPOSIT}.")

    # Check wallet limit
    wallet_r = await db.execute(select(Wallet).where(Wallet.user_id == user.id))
    wallet = wallet_r.scalar_one_or_none()
    if wallet and wallet.balance + amount > MAX_WALLET_BALANCE:
        available = max(0.0, round(MAX_WALLET_BALANCE - wallet.balance, 2))
        raise HTTPException(
            status_code=400,
            detail=f"This would exceed the ₹{MAX_WALLET_BALANCE} wallet limit. "
                   f"You can deposit at most ₹{available} right now.",
        )

    # Check duplicate UTR
    dup = await db.execute(
        select(DepositRequest).where(DepositRequest.utr_number == utr_number.strip())
    )
    if dup.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail="This UTR number has already been submitted. "
                   "Each transaction can only be used once.",
        )

    # Save optional screenshot
    screenshot_path = None
    if screenshot and screenshot.filename:
        data = await screenshot.read()
        if len(data) > 5 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Screenshot too large (max 5 MB).")
        ext = os.path.splitext(screenshot.filename)[1] or ".jpg"
        fname = f"{user.id}_{utr_number.strip()}{ext}"
        fpath = SCREENSHOTS_DIR / fname
        async with aiofiles.open(fpath, "wb") as f:
            await f.write(data)
        screenshot_path = str(fpath)

    req = DepositRequest(
        user_id=user.id,
        amount=amount,
        utr_number=utr_number.strip().upper(),
        upi_id_paid_to=UPI_ID,
        screenshot_path=screenshot_path,
        status="pending",
    )
    db.add(req)
    await db.commit()
    await db.refresh(req)

    logger.info("Deposit request #%d: user=%d amount=₹%.2f UTR=%s",
                req.id, user.id, amount, req.utr_number)
    return {
        "id": req.id,
        "amount": req.amount,
        "utr_number": req.utr_number,
        "status": req.status,
        "message": "Deposit request submitted! Admin will verify and credit your wallet shortly.",
    }


@router.get("/my-requests")
async def my_deposit_requests(
    user: User = Depends(get_active_unbanned_user),
    db: AsyncSession = Depends(get_db),
):
    """List all deposit requests for the logged-in player."""
    result = await db.execute(
        select(DepositRequest)
        .where(DepositRequest.user_id == user.id)
        .order_by(DepositRequest.created_at.desc())
        .limit(20)
    )
    reqs = result.scalars().all()
    return [
        {
            "id": r.id,
            "amount": r.amount,
            "utr_number": r.utr_number,
            "status": r.status,
            "rejection_reason": r.rejection_reason,
            "created_at": r.created_at.isoformat(),
            "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
        }
        for r in reqs
    ]


@router.get("/my-payouts")
async def my_pending_payouts(
    user: User = Depends(get_active_unbanned_user),
    db: AsyncSession = Depends(get_db),
):
    """List all pending/completed payouts for the logged-in player."""
    from models import PendingPayout
    result = await db.execute(
        select(PendingPayout)
        .where(PendingPayout.user_id == user.id)
        .order_by(PendingPayout.created_at.desc())
        .limit(20)
    )
    payouts = result.scalars().all()
    return [
        {
            "id": p.id,
            "game_id": p.game_id,
            "gross_amount": p.gross_amount,
            "platform_fee": p.platform_fee,
            "net_amount": p.net_amount,
            "status": p.status,
            "rejection_reason": p.rejection_reason,
            "auto_release_at": p.auto_release_at.isoformat() if p.auto_release_at else None,
            "created_at": p.created_at.isoformat(),
        }
        for p in payouts
    ]
