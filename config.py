import os
from dotenv import load_dotenv

load_dotenv()

SECRET_KEY: str                = os.getenv("SECRET_KEY", "dev-secret-key-replace-in-production-xyz123")
ALGORITHM: str                 = os.getenv("ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
DATABASE_URL: str              = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./chess.db")

# ── Wallet limits ─────────────────────────────────────────────────────────────
MAX_WALLET_BALANCE: float      = float(os.getenv("MAX_WALLET_BALANCE", "100"))  # ₹100 max in wallet
MIN_BET: float                 = float(os.getenv("MIN_BET", "10"))              # ₹10 minimum bet
MAX_BET: float                 = float(os.getenv("MAX_BET", "100"))             # ₹100 maximum bet

# ── Business model ────────────────────────────────────────────────────────────
# Platform takes PLATFORM_FEE_PERCENT % of prize pool on every completed game.
# Winner always receives (2 × bet) × (1 - fee%).  Platform ALWAYS profits.
PLATFORM_FEE_PERCENT: float    = float(os.getenv("PLATFORM_FEE_PERCENT", "10"))

# ── Payout delay ─────────────────────────────────────────────────────────────
# Winnings are held in escrow for this many hours while admin reviews video.
PAYOUT_HOLD_HOURS: int         = int(os.getenv("PAYOUT_HOLD_HOURS", "24"))

# ── UPI payment details (set these before going live!) ────────────────────────
UPI_ID: str                    = os.getenv("UPI_ID", "chesswager@upi")
UPI_NAME: str                  = os.getenv("UPI_NAME", "ChessWager")
UPI_NOTE: str                  = os.getenv("UPI_NOTE", "ChessWager Deposit")

# ── Anti-cheat thresholds ─────────────────────────────────────────────────────
MIN_MOVE_TIME_MS: int          = int(os.getenv("MIN_MOVE_TIME_MS", "500"))
ANTICHEAT_FAST_MOVE_THRESHOLD_MS: int = 500
ANTICHEAT_FAST_MOVE_STREAK: int       = 5
ANTICHEAT_ACCURACY_THRESHOLD: float   = 0.95

# ── CPU opponent (system user) ─────────────────────────────────────────────
CPU_BOT_USERNAME: str = os.getenv("CPU_BOT_USERNAME", "ChessWagerCPU")
CPU_BOT_EMAIL: str = os.getenv("CPU_BOT_EMAIL", "cpu@chesswager.internal")

# ── Admin ─────────────────────────────────────────────────────────────────────
ADMIN_SECRET: str              = os.getenv("ADMIN_SECRET", "admin-secret-change-me")
