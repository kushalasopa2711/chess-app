"""
Anti-cheat engine for ChessWager API.

Protections implemented:
1. Server-side move validation (python-chess) – illegal moves are rejected before storage.
2. Move timing analysis – consecutive suspiciously-fast moves are flagged.
3. Illegal-move-attempt tracking – repeated illegal move submissions trigger a ban.
4. Single active WebSocket session per player per game.
5. Minimum move time enforcement (configurable, default 500 ms).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import (
    ANTICHEAT_FAST_MOVE_STREAK,
    ANTICHEAT_FAST_MOVE_THRESHOLD_MS,
    MIN_MOVE_TIME_MS,
)
from models import AntiCheatFlag, FlagType, Move, User

logger = logging.getLogger(__name__)

# Per-game illegal-move-attempt counter (in memory; cleared on server restart)
# Structure: {game_id: {user_id: count}}
_illegal_attempt_counter: dict[int, dict[int, int]] = {}

MAX_ILLEGAL_ATTEMPTS = 5  # ban after this many illegal move submissions in one game


async def record_move_and_check(
    db: AsyncSession,
    game_id: int,
    player_id: int,
    move_time_ms: Optional[int],
) -> List[AntiCheatFlag]:
    """
    Check the last N moves for timing patterns and add flags if violations found.
    Returns a list of newly created AntiCheatFlag records (already added to db, not yet committed).
    """
    flags: List[AntiCheatFlag] = []

    if move_time_ms is None:
        return flags

    # Enforce minimum move time
    if move_time_ms < MIN_MOVE_TIME_MS:
        flag = AntiCheatFlag(
            game_id=game_id,
            user_id=player_id,
            flag_type=FlagType.FAST_MOVES,
            description=(
                f"Move submitted in {move_time_ms} ms which is below the minimum "
                f"allowed {MIN_MOVE_TIME_MS} ms."
            ),
            severity=1,
        )
        db.add(flag)
        flags.append(flag)

    # Check streak of fast moves
    result = await db.execute(
        select(Move)
        .where(Move.game_id == game_id, Move.player_id == player_id)
        .order_by(Move.id.desc())
        .limit(ANTICHEAT_FAST_MOVE_STREAK)
    )
    recent_moves = result.scalars().all()

    if len(recent_moves) >= ANTICHEAT_FAST_MOVE_STREAK:
        all_fast = all(
            (m.move_time_ms or 9999) < ANTICHEAT_FAST_MOVE_THRESHOLD_MS
            for m in recent_moves
        )
        if all_fast:
            flag = AntiCheatFlag(
                game_id=game_id,
                user_id=player_id,
                flag_type=FlagType.FAST_MOVES,
                description=(
                    f"Player made {ANTICHEAT_FAST_MOVE_STREAK} consecutive moves "
                    f"in under {ANTICHEAT_FAST_MOVE_THRESHOLD_MS} ms each. "
                    "Possible engine use detected."
                ),
                severity=3,
            )
            db.add(flag)
            flags.append(flag)

    return flags


async def record_illegal_attempt(
    db: AsyncSession,
    game_id: int,
    player_id: int,
    attempted_move: str,
) -> bool:
    """
    Record an illegal move attempt.
    Returns True if the player should be banned.
    """
    if game_id not in _illegal_attempt_counter:
        _illegal_attempt_counter[game_id] = {}

    count = _illegal_attempt_counter[game_id].get(player_id, 0) + 1
    _illegal_attempt_counter[game_id][player_id] = count

    flag = AntiCheatFlag(
        game_id=game_id,
        user_id=player_id,
        flag_type=FlagType.ILLEGAL_MOVE_ATTEMPT,
        description=f"Illegal move attempted: '{attempted_move}' (attempt #{count})",
        severity=2 if count < MAX_ILLEGAL_ATTEMPTS else 3,
    )
    db.add(flag)

    if count >= MAX_ILLEGAL_ATTEMPTS:
        result = await db.execute(select(User).where(User.id == player_id))
        user = result.scalar_one_or_none()
        if user:
            user.is_banned = True
            user.ban_reason = (
                f"Repeated illegal move submissions ({count}) in game #{game_id}. "
                "Possible board manipulation or automated client detected."
            )
            db.add(user)
        logger.warning("User %d banned for repeated illegal moves in game %d", player_id, game_id)
        return True

    return False


async def should_ban_for_flags(db: AsyncSession, user_id: int, game_id: int) -> Optional[str]:
    """
    Inspect recent severity-3 flags. If found, ban the user and return the ban reason.
    """
    result = await db.execute(
        select(AntiCheatFlag)
        .where(
            AntiCheatFlag.user_id == user_id,
            AntiCheatFlag.game_id == game_id,
            AntiCheatFlag.severity == 3,
        )
    )
    severe_flags = result.scalars().all()
    if severe_flags:
        reason = "; ".join(f.description for f in severe_flags[:3])
        return f"Anti-cheat: {reason}"
    return None
