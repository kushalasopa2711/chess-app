from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, EmailStr, Field, field_validator


# ─── Auth ────────────────────────────────────────────────────────────────────

class UserRegister(BaseModel):
    username: str = Field(..., min_length=3, max_length=50, pattern=r"^[a-zA-Z0-9_]+$")
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)


class UserLogin(BaseModel):
    username: str
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TokenData(BaseModel):
    user_id: Optional[int] = None


# ─── User ─────────────────────────────────────────────────────────────────────

class UserPublic(BaseModel):
    id: int
    username: str
    games_played: int
    games_won: int
    total_earned: float
    created_at: datetime
    is_banned: bool

    model_config = {"from_attributes": True}


class UserProfile(UserPublic):
    email: str


class LoginResponse(BaseModel):
    """JWT plus profile in one round trip (avoids /auth/me before token is applied client-side)."""
    access_token: str
    token_type: str = "bearer"
    user: UserProfile


# ─── Wallet ───────────────────────────────────────────────────────────────────

class WalletOut(BaseModel):
    balance: float
    total_invested: float
    updated_at: Optional[datetime]

    model_config = {"from_attributes": True}


class DepositRequest(BaseModel):
    amount: float = Field(..., gt=0, le=100, description="Amount in rupees (max ₹100)")

    @field_validator("amount")
    @classmethod
    def round_to_paise(cls, v: float) -> float:
        return round(v, 2)


class WithdrawRequest(BaseModel):
    amount: float = Field(..., gt=0)

    @field_validator("amount")
    @classmethod
    def round_to_paise(cls, v: float) -> float:
        return round(v, 2)


class TransactionOut(BaseModel):
    id: int
    amount: float
    type: str
    game_id: Optional[int]
    description: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── Game ─────────────────────────────────────────────────────────────────────

class GameCreate(BaseModel):
    bet_amount: float = Field(..., gt=0, le=100, description="Bet in rupees (max ₹100)")
    vs_cpu: bool = Field(False, description="Play against computer (black). No human join.")
    video_prize_terms_ack: bool = Field(
        False,
        description=(
            "Required for cash games. Acknowledges that vs-player games need both players' usable "
            "video for payout verification (vs CPU: human only), and unclear/missing video may block payout."
        ),
    )
    clock_initial_sec: int = Field(600, ge=180, le=3600, description="Starting clock per side (seconds).")
    clock_increment_sec: int = Field(5, ge=0, le=60, description="Seconds added after each move.")

    @field_validator("bet_amount")
    @classmethod
    def round_bet(cls, v: float) -> float:
        return round(v, 2)


class MoveRequest(BaseModel):
    move: str = Field(..., description="Move in UCI format (e.g. e2e4) or SAN (e.g. e4)")
    client_timestamp: Optional[int] = Field(
        None, description="Client-side Unix timestamp in milliseconds for timing analysis"
    )


class MoveOut(BaseModel):
    move_uci: str
    move_san: str
    fen_after: str
    move_number: int
    move_time_ms: Optional[int]
    created_at: datetime

    model_config = {"from_attributes": True}


class GameOut(BaseModel):
    id: int
    white_player_id: int
    black_player_id: Optional[int]
    status: str
    fen: str
    pgn: str
    bet_amount: float
    winner_id: Optional[int]
    result: Optional[str]
    is_vs_cpu: bool = False
    video_prize_terms_ack: bool = False
    clock_initial_sec: int = 600
    clock_increment_sec: int = 5
    white_time_ms: int = 600_000
    black_time_ms: int = 600_000
    created_at: datetime
    started_at: Optional[datetime]
    ended_at: Optional[datetime]

    model_config = {"from_attributes": True}


class GameDetail(GameOut):
    moves: List[MoveOut] = []


class GameListItem(BaseModel):
    id: int
    white_player_id: int
    black_player_id: Optional[int] = None
    white_username: str
    status: str
    bet_amount: float
    is_vs_cpu: bool = False
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── Anti-cheat ───────────────────────────────────────────────────────────────

class AntiCheatFlagOut(BaseModel):
    id: int
    game_id: int
    flag_type: str
    description: str
    severity: int
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── WebSocket messages ───────────────────────────────────────────────────────

class WSMessage(BaseModel):
    type: str
    data: dict = {}
