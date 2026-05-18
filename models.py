from datetime import datetime
from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey,
    Integer, String, Text, Enum as SAEnum
)
from sqlalchemy.orm import relationship
import enum

from database import Base


class GameStatus(str, enum.Enum):
    WAITING = "waiting"
    ACTIVE = "active"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class TransactionType(str, enum.Enum):
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    BET = "bet"
    WIN = "win"
    REFUND = "refund"


class FlagType(str, enum.Enum):
    FAST_MOVES = "fast_moves"
    ILLEGAL_MOVE_ATTEMPT = "illegal_move_attempt"
    HIGH_ACCURACY = "high_accuracy"
    MULTIPLE_SESSIONS = "multiple_sessions"
    SUSPICIOUS_PATTERN = "suspicious_pattern"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True)
    is_banned = Column(Boolean, default=False)
    ban_reason = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Stats
    games_played = Column(Integer, default=0)
    games_won = Column(Integer, default=0)
    total_earned = Column(Float, default=0.0)

    wallet = relationship("Wallet", back_populates="user", uselist=False)
    transactions = relationship("Transaction", back_populates="user")
    anticheat_flags = relationship("AntiCheatFlag", back_populates="user")


class Wallet(Base):
    __tablename__ = "wallets"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    balance = Column(Float, default=0.0)
    total_invested = Column(Float, default=0.0)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="wallet")


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    amount = Column(Float, nullable=False)
    type = Column(SAEnum(TransactionType), nullable=False)
    game_id = Column(Integer, ForeignKey("games.id"), nullable=True)
    description = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="transactions")


class Game(Base):
    __tablename__ = "games"

    id = Column(Integer, primary_key=True, index=True)
    white_player_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    black_player_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    status = Column(SAEnum(GameStatus), default=GameStatus.WAITING)
    fen = Column(Text, default="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1")
    pgn = Column(Text, default="")
    bet_amount = Column(Float, nullable=False)
    winner_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    result = Column(String(20), nullable=True)  # "white", "black", "draw"
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    ended_at = Column(DateTime, nullable=True)

    moves = relationship("Move", back_populates="game")
    anticheat_flags = relationship("AntiCheatFlag", back_populates="game")


class Move(Base):
    __tablename__ = "moves"

    id = Column(Integer, primary_key=True, index=True)
    game_id = Column(Integer, ForeignKey("games.id"), nullable=False)
    player_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    move_uci = Column(String(10), nullable=False)
    move_san = Column(String(20), nullable=False)
    fen_before = Column(Text, nullable=False)
    fen_after = Column(Text, nullable=False)
    move_number = Column(Integer, nullable=False)
    move_time_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    game = relationship("Game", back_populates="moves")


class AntiCheatFlag(Base):
    __tablename__ = "anticheat_flags"

    id = Column(Integer, primary_key=True, index=True)
    game_id = Column(Integer, ForeignKey("games.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    flag_type = Column(SAEnum(FlagType), nullable=False)
    description = Column(String(1000), nullable=False)
    severity = Column(Integer, default=1)  # 1=warning, 2=review, 3=ban
    created_at = Column(DateTime, default=datetime.utcnow)

    game = relationship("Game", back_populates="anticheat_flags")
    user = relationship("User", back_populates="anticheat_flags")


class VideoChunk(Base):
    """Stores metadata for each 30-second webcam recording chunk uploaded during a bet game."""
    __tablename__ = "video_chunks"

    id = Column(Integer, primary_key=True, index=True)
    game_id = Column(Integer, ForeignKey("games.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    chunk_number = Column(Integer, nullable=False)
    file_path = Column(String(500), nullable=False)
    file_size_bytes = Column(Integer, default=0)
    flagged = Column(Boolean, default=False)
    flag_reason = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Penalty(Base):
    """Records financial penalties applied to players via video evidence review."""
    __tablename__ = "penalties"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    game_id = Column(Integer, ForeignKey("games.id"), nullable=True)
    amount_deducted = Column(Float, default=0.0)
    account_banned = Column(Boolean, default=False)
    reason = Column(String(1000), nullable=False)
    reviewed_by = Column(String(100), default="system")
    created_at = Column(DateTime, default=datetime.utcnow)


class DepositRequest(Base):
    """
    UPI deposit request submitted by a player.
    Player pays via Google Pay / PhonePe and enters the UTR number.
    Admin verifies the UTR and approves/rejects the credit.
    """
    __tablename__ = "deposit_requests"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    amount = Column(Float, nullable=False)
    utr_number = Column(String(50), nullable=False)          # UPI transaction reference
    upi_id_paid_to = Column(String(100), nullable=True)      # UPI ID they paid to
    screenshot_path = Column(String(500), nullable=True)     # optional screenshot upload
    status = Column(String(20), default="pending")           # pending / approved / rejected
    rejection_reason = Column(String(300), nullable=True)
    reviewed_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    reviewed_at = Column(DateTime, nullable=True)


class PendingPayout(Base):
    """
    Winnings are held here for PAYOUT_HOLD_HOURS (default 24h) while
    admin reviews webcam video footage before releasing funds.
    """
    __tablename__ = "pending_payouts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    game_id = Column(Integer, ForeignKey("games.id"), nullable=False)
    gross_amount = Column(Float, nullable=False)      # 2 × bet (before fee)
    platform_fee = Column(Float, nullable=False)      # amount kept by platform
    net_amount = Column(Float, nullable=False)        # gross - fee (what player gets)
    status = Column(String(20), default="pending")    # pending / approved / rejected / penalized
    rejection_reason = Column(String(500), nullable=True)
    penalty_amount = Column(Float, default=0.0)       # extra deduction if cheating found
    reviewed_by = Column(String(100), nullable=True)
    auto_release_at = Column(DateTime, nullable=True) # auto-approve after this time if no review
    created_at = Column(DateTime, default=datetime.utcnow)
    reviewed_at = Column(DateTime, nullable=True)
