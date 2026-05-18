"""
Video evidence checks before winnings can be released.

Multiplayer (human vs human): at least one usable chunk from **both** White and Black.
Vs CPU: only the human (White) must have usable chunks.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Game, VideoChunk

# Match client: tiny uploads are ignored (VideoMonitor skips size <= 1000)
MIN_CHUNK_BYTES = 1000


async def players_with_usable_video(db: AsyncSession, game_id: int) -> set[int]:
    r = await db.execute(
        select(VideoChunk.user_id)
        .where(VideoChunk.game_id == game_id)
        .where(VideoChunk.file_size_bytes >= MIN_CHUNK_BYTES)
        .distinct()
    )
    return set(r.scalars().all())


async def payout_video_requirement_error(db: AsyncSession, game: Game) -> str | None:
    """
    If payout must NOT proceed yet, return a human-readable reason.
    If OK to proceed (or no bet / nothing to check), return None.
    """
    if game.bet_amount <= 0:
        return None

    have = await players_with_usable_video(db, game.id)

    if game.is_vs_cpu:
        if game.white_player_id in have:
            return None
        return (
            "Payout blocked: this vs-CPU game has no usable webcam recording from the "
            "human player (minimum chunk size on file)."
        )

    if not game.black_player_id:
        return "Payout blocked: multiplayer game is missing black player id."

    w_miss = game.white_player_id not in have
    b_miss = game.black_player_id not in have
    if not w_miss and not b_miss:
        return None

    parts = []
    if w_miss:
        parts.append("White")
    if b_miss:
        parts.append("Black")
    return (
        "Payout blocked: multiplayer games require usable video from BOTH players for "
        f"verification. Missing recordings for: {', '.join(parts)}."
    )


async def video_evidence_summary_for_admin(db: AsyncSession, game: Game) -> dict:
    """Lightweight status for admin payout list UI."""
    have = await players_with_usable_video(db, game.id)
    if game.is_vs_cpu:
        ok = game.white_player_id in have
        return {
            "video_ok_for_payout": ok,
            "video_players_with_chunks": sorted(have),
            "video_note": "Vs computer: only White (you) must upload video.",
        }
    if not game.black_player_id:
        return {
            "video_ok_for_payout": False,
            "video_players_with_chunks": sorted(have),
            "video_note": "No black player on record.",
        }
    w_ok = game.white_player_id in have
    b_ok = game.black_player_id in have
    ok = w_ok and b_ok
    return {
        "video_ok_for_payout": ok,
        "video_players_with_chunks": sorted(have),
        "video_note": (
            "Both White and Black have usable chunks."
            if ok
            else f"White={'yes' if w_ok else 'no'}, Black={'yes' if b_ok else 'no'} — both required."
        ),
    }
