import logging
import os
import secrets
import sys
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

ENV: str = os.getenv("ENV", "development").lower()
IS_PROD: bool = ENV in ("prod", "production")


def _resolve_secret_key() -> str:
    """
    SECRET_KEY rules:
      - production  : MUST be set in the environment and ≥ 32 chars; refuse to boot otherwise.
      - development : if missing, generate a random one (warns) so local dev still works.
    """
    raw = os.getenv("SECRET_KEY", "").strip()
    if IS_PROD:
        if not raw or len(raw) < 32:
            sys.stderr.write(
                "FATAL: SECRET_KEY must be set to a value of at least 32 characters when ENV=production.\n"
            )
            raise RuntimeError("SECRET_KEY not configured for production")
        return raw
    if not raw:
        generated = secrets.token_urlsafe(48)
        logger.warning(
            "SECRET_KEY not set — generated an ephemeral one for development. "
            "Sessions will be invalidated on restart. Set SECRET_KEY in .env for stable dev tokens."
        )
        return generated
    return raw


def _normalize_database_url(raw: str) -> str:
    """
    Accept the URL forms that hosted Postgres providers hand out and convert
    them to SQLAlchemy's async driver format.
      - postgres://USER:PASS@HOST/DB           -> postgresql+asyncpg://...
      - postgresql://USER:PASS@HOST/DB         -> postgresql+asyncpg://...
      - postgresql+asyncpg://...               -> unchanged
      - sqlite:///./chess.db                   -> sqlite+aiosqlite:///./chess.db
      - sqlite+aiosqlite:///./chess.db         -> unchanged
    """
    if not raw:
        return "sqlite+aiosqlite:///./chess.db"
    url = raw.strip()
    if url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://") and "+asyncpg" not in url:
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    elif url.startswith("sqlite:") and "+aiosqlite" not in url:
        url = url.replace("sqlite:", "sqlite+aiosqlite:", 1)
    return url


SECRET_KEY: str                = _resolve_secret_key()
ALGORITHM: str                 = os.getenv("ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
DATABASE_URL: str              = _normalize_database_url(
    os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./chess.db")
)

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
_admin_raw = os.getenv("ADMIN_SECRET", "").strip()
if IS_PROD:
    if not _admin_raw or _admin_raw == "admin-secret-change-me" or len(_admin_raw) < 16:
        sys.stderr.write(
            "FATAL: ADMIN_SECRET must be set to a non-default value of at least 16 characters when ENV=production.\n"
        )
        raise RuntimeError("ADMIN_SECRET not configured for production")
ADMIN_SECRET: str = _admin_raw or "admin-secret-change-me"

# ── CORS (production) ─────────────────────────────────────────────────────────
# Comma-separated list of allowed origins; defaults to ``*`` for local dev.
# In production set ALLOWED_ORIGINS to e.g. "https://chesswager.example.com".
ALLOWED_ORIGINS: list[str] = [
    o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()
] or ["*"]
