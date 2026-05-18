"""
Games router — REST endpoints + WebSocket for real-time chess.

Move flow:
  Client → POST /games/{id}/move  OR  WS /ws/{id}
    → server validates with python-chess (illegal moves rejected)
    → anti-cheat timing check
    → move stored
    → game state broadcast over WebSocket to both players
    → if game over: wallets settled, stats updated
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

import chess
import chess.pgn
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import anticheat
from auth import get_active_unbanned_user, get_current_user
from config import MAX_WALLET_BALANCE, MIN_BET, MAX_BET, PLATFORM_FEE_PERCENT, PAYOUT_HOLD_HOURS, CPU_BOT_USERNAME
from database import get_db, AsyncSessionLocal
from models import (
    Game, GameStatus, Move, PendingPayout, Transaction, TransactionType, User, Wallet
)
from schemas import GameCreate, GameDetail, GameListItem, GameOut, MoveOut, MoveRequest
from websocket_manager import manager
from cpu_ai import choose_cpu_move

router = APIRouter(prefix="/games", tags=["Games"])
logger = logging.getLogger(__name__)


# ─── Helpers ─────────────────────────────────────────────────────────────────

async def _get_game_or_404(game_id: int, db: AsyncSession) -> Game:
    result = await db.execute(select(Game).where(Game.id == game_id))
    game = result.scalar_one_or_none()
    if not game:
        raise HTTPException(status_code=404, detail="Game not found.")
    return game


async def _get_wallet_or_404(user_id: int, db: AsyncSession) -> Wallet:
    result = await db.execute(select(Wallet).where(Wallet.user_id == user_id))
    wallet = result.scalar_one_or_none()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found.")
    return wallet


def _board_from_game(game: Game) -> chess.Board:
    board = chess.Board()
    board.set_fen(game.fen)
    return board


async def _get_cpu_user(db: AsyncSession) -> User:
    r = await db.execute(select(User).where(User.username == CPU_BOT_USERNAME))
    cpu = r.scalar_one_or_none()
    if not cpu or not cpu.is_bot:
        raise HTTPException(
            status_code=503,
            detail="Server is still starting — CPU opponent unavailable. Try again shortly.",
        )
    return cpu


def _tick_clock_before_move(game: Game, board: chess.Board) -> Optional[str]:
    """
    Apply elapsed time to the side to move (mutates game.*_time_ms).
    Does not change clock_last_tick_at — that is set when a move completes.
    Returns winning color on flag fall ('white'|'black'), else None.
    """
    if game.status != GameStatus.ACTIVE or game.clock_last_tick_at is None:
        return None
    now = datetime.utcnow()
    elapsed_ms = int((now - game.clock_last_tick_at).total_seconds() * 1000)
    if elapsed_ms < 0:
        elapsed_ms = 0
    if board.turn == chess.WHITE:
        game.white_time_ms = max(0, int(game.white_time_ms) - elapsed_ms)
        if game.white_time_ms <= 0:
            return "black"
    else:
        game.black_time_ms = max(0, int(game.black_time_ms) - elapsed_ms)
        if game.black_time_ms <= 0:
            return "white"
    return None


def _clock_display_millis(game: Game, board: chess.Board) -> tuple[int, int]:
    """Remaining times as shown now (read-only), for API responses."""
    if game.status != GameStatus.ACTIVE or game.clock_last_tick_at is None:
        return int(game.white_time_ms), int(game.black_time_ms)
    now = datetime.utcnow()
    elapsed_ms = int((now - game.clock_last_tick_at).total_seconds() * 1000)
    if elapsed_ms < 0:
        elapsed_ms = 0
    ew, eb = int(game.white_time_ms), int(game.black_time_ms)
    if board.turn == chess.WHITE:
        ew = max(0, ew - elapsed_ms)
    else:
        eb = max(0, eb - elapsed_ms)
    return ew, eb


def _after_move_clock(game: Game, mover_was_white: bool) -> None:
    """Fischer: add increment to the player who just moved; next side's clock runs from now."""
    inc = int(game.clock_increment_sec) * 1000
    if mover_was_white:
        game.white_time_ms = int(game.white_time_ms) + inc
    else:
        game.black_time_ms = int(game.black_time_ms) + inc
    game.clock_last_tick_at = datetime.utcnow()


async def _settle_by_result(db: AsyncSession, game: Game, result_str: str) -> None:
    """
    Apply wallet + stats for a finished game. result_str is 'white', 'black', or 'draw'.
    Human vs CPU: only the human locks stake; refunds and payouts follow that rule.
    """
    game.status = GameStatus.COMPLETED
    game.result = result_str
    game.ended_at = datetime.utcnow()

    white_wallet = await _get_wallet_or_404(game.white_player_id, db)
    black_wallet = await _get_wallet_or_404(game.black_player_id, db)
    white_r = await db.execute(select(User).where(User.id == game.white_player_id))
    black_r = await db.execute(select(User).where(User.id == game.black_player_id))
    white_user = white_r.scalar_one()
    black_user = black_r.scalar_one()

    if game.is_vs_cpu:
        # Only white (human) locked a real stake for vs_cpu (white is always human creator).
        white_wallet.total_invested = max(0.0, round(white_wallet.total_invested - game.bet_amount, 2))
        if result_str == "draw":
            white_wallet.balance = round(white_wallet.balance + game.bet_amount, 2)
            db.add(Transaction(
                user_id=game.white_player_id,
                amount=game.bet_amount,
                type=TransactionType.REFUND,
                game_id=game.id,
                description=f"Draw refund – Game #{game.id} (vs CPU)",
            ))
        else:
            winner_id = game.white_player_id if result_str == "white" else game.black_player_id
            game.winner_id = winner_id
            winner_u = white_user if result_str == "white" else black_user
            if winner_u.is_bot:
                db.add(Transaction(
                    user_id=game.white_player_id,
                    amount=game.bet_amount,
                    type=TransactionType.BET,
                    game_id=game.id,
                    description=f"Lost Game #{game.id} – bet forfeited (vs CPU)",
                ))
            else:
                gross = round(game.bet_amount * 2, 2)
                fee = round(gross * PLATFORM_FEE_PERCENT / 100, 2)
                net = round(gross - fee, 2)
                auto_release = datetime.utcnow() + timedelta(hours=PAYOUT_HOLD_HOURS)
                db.add(
                    PendingPayout(
                        user_id=winner_id,
                        game_id=game.id,
                        gross_amount=gross,
                        platform_fee=fee,
                        net_amount=net,
                        status="pending",
                        auto_release_at=auto_release,
                    )
                )
                logger.info(
                    "Game #%d vs CPU ended: human wins. Gross=₹%.2f Fee=₹%.2f Net=₹%.2f escrow",
                    game.id,
                    gross,
                    fee,
                    net,
                )
        white_user.games_played += 1
        if result_str == "white":
            white_user.games_won += 1
        db.add(white_user)
    else:
        white_wallet.total_invested = max(0.0, round(white_wallet.total_invested - game.bet_amount, 2))
        black_wallet.total_invested = max(0.0, round(black_wallet.total_invested - game.bet_amount, 2))

        if result_str == "draw":
            white_wallet.balance = round(white_wallet.balance + game.bet_amount, 2)
            black_wallet.balance = round(black_wallet.balance + game.bet_amount, 2)
            for uid in (game.white_player_id, game.black_player_id):
                db.add(
                    Transaction(
                        user_id=uid,
                        amount=game.bet_amount,
                        type=TransactionType.REFUND,
                        game_id=game.id,
                        description=f"Draw refund – Game #{game.id}",
                    )
                )
        else:
            winner_id = game.white_player_id if result_str == "white" else game.black_player_id
            game.winner_id = winner_id
            gross = round(game.bet_amount * 2, 2)
            fee = round(gross * PLATFORM_FEE_PERCENT / 100, 2)
            net = round(gross - fee, 2)
            auto_release = datetime.utcnow() + timedelta(hours=PAYOUT_HOLD_HOURS)
            db.add(
                PendingPayout(
                    user_id=winner_id,
                    game_id=game.id,
                    gross_amount=gross,
                    platform_fee=fee,
                    net_amount=net,
                    status="pending",
                    auto_release_at=auto_release,
                )
            )
            loser_id = game.black_player_id if result_str == "white" else game.white_player_id
            db.add(
                Transaction(
                    user_id=loser_id,
                    amount=game.bet_amount,
                    type=TransactionType.BET,
                    game_id=game.id,
                    description=f"Lost Game #{game.id} – bet forfeited",
                )
            )
            logger.info(
                "Game #%d ended: %s wins. Gross=₹%.2f Fee=₹%.2f Net=₹%.2f held %sh",
                game.id,
                result_str,
                gross,
                fee,
                net,
                PAYOUT_HOLD_HOURS,
            )

        white_user.games_played += 1
        black_user.games_played += 1
        if result_str == "white":
            white_user.games_won += 1
        elif result_str == "black":
            black_user.games_won += 1
        db.add(white_user)
        db.add(black_user)
        db.add(white_wallet)
        db.add(black_wallet)

    if game.is_vs_cpu:
        db.add(white_wallet)
        db.add(black_wallet)

    db.add(game)


async def _settle_game(db: AsyncSession, game: Game, board: chess.Board) -> None:
    """
    End a game from chess position, release locks, escrow or refund.

    Platform fee is taken from the prize pool when there is a human winner (PendingPayout).
    """
    result_str: Optional[str] = None
    if board.is_checkmate():
        result_str = "black" if board.turn == chess.WHITE else "white"
    elif board.is_stalemate() or board.is_insufficient_material() or board.is_seventyfive_moves():
        result_str = "draw"
    elif board.is_game_over():
        result_str = "draw"

    if result_str is None:
        return

    await _settle_by_result(db, game, result_str)


async def _apply_move(
    db: AsyncSession,
    game: Game,
    player: User,
    move_str: str,
    client_timestamp_ms: Optional[int],
    last_move_time: Optional[datetime],
) -> tuple[Optional[chess.Move], chess.Board, Optional[int], bool]:
    """
    Validate and apply a chess move.
    Returns (move or None if already settled elsewhere, board, move_time_ms, time_forfeit).
    If time_forfeit is True, game was ended by clock and move is None.
    """
    board = _board_from_game(game)

    is_white_turn = board.turn == chess.WHITE
    expected_player_id = game.white_player_id if is_white_turn else game.black_player_id
    if player.id != expected_player_id:
        raise HTTPException(status_code=400, detail="It is not your turn.")

    flag = _tick_clock_before_move(game, board)
    if flag is not None:
        await _settle_by_result(db, game, flag)
        return None, board, None, True

    chess_move: Optional[chess.Move] = None
    try:
        chess_move = chess.Move.from_uci(move_str)
        if chess_move not in board.legal_moves:
            chess_move = None
    except ValueError:
        pass

    if chess_move is None:
        try:
            chess_move = board.parse_san(move_str)
        except (chess.InvalidMoveError, chess.IllegalMoveError, chess.AmbiguousMoveError):
            pass

    if chess_move is None or chess_move not in board.legal_moves:
        should_ban = await anticheat.record_illegal_attempt(db, game.id, player.id, move_str)
        await db.commit()
        if should_ban:
            raise HTTPException(
                status_code=403,
                detail="Account banned due to repeated illegal move submissions.",
            )
        raise HTTPException(status_code=400, detail=f"Illegal move: '{move_str}'.")

    move_time_ms: Optional[int] = None
    now = datetime.utcnow()
    if not player.is_bot:
        if last_move_time:
            move_time_ms = int((now - last_move_time).total_seconds() * 1000)
        elif client_timestamp_ms:
            move_time_ms = client_timestamp_ms

        flags = await anticheat.record_move_and_check(db, game.id, player.id, move_time_ms)
        for flag in flags:
            if flag.severity == 3:
                player.is_banned = True
                player.ban_reason = flag.description
                db.add(player)
                await db.commit()
                raise HTTPException(
                    status_code=403,
                    detail=f"Account banned: {flag.description}",
                )

    san = board.san(chess_move)
    fen_before = board.fen()
    board.push(chess_move)
    fen_after = board.fen()
    mover_was_white = is_white_turn
    _after_move_clock(game, mover_was_white)

    result = await db.execute(select(Move).where(Move.game_id == game.id))
    move_number = len(result.scalars().all()) + 1

    move_record = Move(
        game_id=game.id,
        player_id=player.id,
        move_uci=chess_move.uci(),
        move_san=san,
        fen_before=fen_before,
        fen_after=fen_after,
        move_number=move_number,
        move_time_ms=move_time_ms,
    )
    db.add(move_record)
    game.fen = fen_after
    db.add(game)

    return chess_move, board, move_time_ms, False


async def _play_one_cpu_move_if_needed(
    db: AsyncSession,
    game: Game,
) -> tuple[Optional[Move], chess.Board, bool]:
    """
    If black is the CPU and it is black to move, play one engine move.
    Returns (move_record or None, board after any updates, game_ended).
    """
    if game.status != GameStatus.ACTIVE or not game.black_player_id:
        return None, _board_from_game(game), False
    user_r = await db.execute(select(User).where(User.id == game.black_player_id))
    black_user = user_r.scalar_one()
    if not black_user.is_bot:
        return None, _board_from_game(game), False

    board = _board_from_game(game)
    if board.turn != chess.BLACK:
        return None, board, False

    uci = choose_cpu_move(board).uci()
    _, board_after, _, forfeited = await _apply_move(
        db, game, black_user, uci, None, None,
    )
    if forfeited:
        return None, board_after, True

    move_r = await db.execute(
        select(Move).where(Move.game_id == game.id).order_by(Move.id.desc()).limit(1)
    )
    move_record = move_r.scalar_one()
    ended = board_after.is_game_over()
    if ended:
        await _settle_game(db, game, board_after)
    return move_record, board_after, ended


# ─── REST Endpoints ───────────────────────────────────────────────────────────

@router.get("", response_model=list[GameListItem])
async def list_games(
    status_filter: Optional[str] = Query(None, alias="status"),
    db: AsyncSession = Depends(get_db),
):
    """List games. Optionally filter by status: waiting, active, completed."""
    q = select(Game, User).join(User, User.id == Game.white_player_id)
    if status_filter:
        try:
            gs = GameStatus(status_filter)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status '{status_filter}'.")
        if gs == GameStatus.COMPLETED:
            q = q.where(Game.status.in_([GameStatus.COMPLETED, GameStatus.ABANDONED]))
        else:
            q = q.where(Game.status == gs)
    q = q.order_by(Game.created_at.desc()).limit(100)
    result = await db.execute(q)
    rows = result.all()
    return [
        GameListItem(
            id=g.id,
            white_player_id=g.white_player_id,
            black_player_id=g.black_player_id,
            white_username=u.username,
            status=g.status.value,
            bet_amount=g.bet_amount,
            is_vs_cpu=g.is_vs_cpu,
            created_at=g.created_at,
        )
        for g, u in rows
    ]


@router.post("", response_model=GameOut, status_code=201)
async def create_game(
    payload: GameCreate,
    user: User = Depends(get_active_unbanned_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a new game as white. Bet is locked immediately.

    Use ``vs_cpu=true`` to play the computer as black — the game starts at once
    (no join step). Otherwise another player must join before moves are allowed.
    """
    if not payload.video_prize_terms_ack:
        raise HTTPException(
            status_code=400,
            detail=(
                "You must accept the prize eligibility terms: for games against another player, "
                "usable video from both sides is typically required before winnings can be approved; "
                "if recording is off or unclear, payouts may not be credited."
            ),
        )

    if payload.bet_amount < MIN_BET:
        raise HTTPException(status_code=400, detail=f"Minimum bet is ₹{MIN_BET}.")
    if payload.bet_amount > MAX_BET:
        raise HTTPException(status_code=400, detail=f"Maximum bet is ₹{MAX_BET}.")

    wallet = await _get_wallet_or_404(user.id, db)
    available = round(wallet.balance - wallet.total_invested, 2)
    if payload.bet_amount > available:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient available balance. Available: ₹{available}.",
        )
    if wallet.total_invested + payload.bet_amount > MAX_WALLET_BALANCE:
        raise HTTPException(
            status_code=400,
            detail=f"Total at-stake amount cannot exceed ₹{MAX_WALLET_BALANCE}.",
        )

    wallet.total_invested = round(wallet.total_invested + payload.bet_amount, 2)
    db.add(wallet)

    t0 = datetime.utcnow()
    init_ms = int(payload.clock_initial_sec) * 1000

    if payload.vs_cpu:
        cpu = await _get_cpu_user(db)
        game = Game(
            white_player_id=user.id,
            black_player_id=cpu.id,
            bet_amount=payload.bet_amount,
            status=GameStatus.ACTIVE,
            is_vs_cpu=True,
            video_prize_terms_ack=True,
            clock_initial_sec=payload.clock_initial_sec,
            clock_increment_sec=payload.clock_increment_sec,
            white_time_ms=init_ms,
            black_time_ms=init_ms,
            started_at=t0,
            clock_last_tick_at=t0,
        )
        db.add(game)
        await db.flush()
        db.add(
            Transaction(
                user_id=user.id,
                amount=payload.bet_amount,
                type=TransactionType.BET,
                game_id=game.id,
                description=f"Bet locked for game #{game.id} (vs CPU)",
            )
        )
    else:
        game = Game(
            white_player_id=user.id,
            bet_amount=payload.bet_amount,
            video_prize_terms_ack=payload.video_prize_terms_ack,
            clock_initial_sec=payload.clock_initial_sec,
            clock_increment_sec=payload.clock_increment_sec,
            white_time_ms=init_ms,
            black_time_ms=init_ms,
        )
        db.add(game)
        await db.flush()
        db.add(
            Transaction(
                user_id=user.id,
                amount=payload.bet_amount,
                type=TransactionType.BET,
                game_id=game.id,
                description=f"Bet locked for game #{game.id}",
            )
        )

    await db.commit()
    await db.refresh(game)
    if payload.vs_cpu:
        await manager.broadcast_to_game(game.id, {
            "type": "game_started",
            "data": {
                "game_id": game.id,
                "black_player_id": game.black_player_id,
                "fen": game.fen,
                "is_vs_cpu": True,
                "clock": {
                    "white_time_ms": game.white_time_ms,
                    "black_time_ms": game.black_time_ms,
                    "increment_sec": game.clock_increment_sec,
                },
            },
        })
    return game


@router.get("/{game_id}", response_model=GameDetail)
async def get_game(game_id: int, db: AsyncSession = Depends(get_db)):
    """Get full game state including move history."""
    game = await _get_game_or_404(game_id, db)
    # Load moves
    result = await db.execute(
        select(Move).where(Move.game_id == game_id).order_by(Move.move_number)
    )
    moves = result.scalars().all()
    detail = GameDetail.model_validate(game)
    b = _board_from_game(game)
    w_m, b_m = _clock_display_millis(game, b)
    detail.white_time_ms = w_m
    detail.black_time_ms = b_m
    detail.moves = [MoveOut.model_validate(m) for m in moves]
    return detail


@router.post("/{game_id}/join", response_model=GameOut)
async def join_game(
    game_id: int,
    user: User = Depends(get_active_unbanned_user),
    db: AsyncSession = Depends(get_db),
):
    """Join a waiting game as black. Your bet is locked immediately."""
    game = await _get_game_or_404(game_id, db)
    if game.is_vs_cpu:
        raise HTTPException(
            status_code=400,
            detail="This game is against the computer — it cannot be joined by a second human.",
        )
    if game.status != GameStatus.WAITING:
        raise HTTPException(status_code=400, detail="This game is not open to join.")
    if game.white_player_id == user.id:
        raise HTTPException(status_code=400, detail="You cannot play against yourself.")

    wallet = await _get_wallet_or_404(user.id, db)
    available = round(wallet.balance - wallet.total_invested, 2)
    if game.bet_amount > available:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient balance. Need ₹{game.bet_amount}, available ₹{available}.",
        )
    if wallet.total_invested + game.bet_amount > MAX_WALLET_BALANCE:
        raise HTTPException(
            status_code=400,
            detail=f"Total at-stake cannot exceed ₹{MAX_WALLET_BALANCE}.",
        )

    wallet.total_invested = round(wallet.total_invested + game.bet_amount, 2)
    db.add(wallet)
    game.black_player_id = user.id
    game.status = GameStatus.ACTIVE
    start = datetime.utcnow()
    game.started_at = start
    game.clock_last_tick_at = start
    init_ms = int(game.clock_initial_sec) * 1000
    game.white_time_ms = init_ms
    game.black_time_ms = init_ms

    db.add(Transaction(
        user_id=user.id,
        amount=game.bet_amount,
        type=TransactionType.BET,
        game_id=game.id,
        description=f"Bet locked for game #{game.id}",
    ))
    db.add(game)
    await db.commit()
    await db.refresh(game)

    # Notify white player via WebSocket
    await manager.broadcast_to_game(game_id, {
        "type": "game_started",
        "data": {
            "game_id": game_id,
            "black_player_id": user.id,
            "fen": game.fen,
            "is_vs_cpu": False,
            "clock": {
                "white_time_ms": game.white_time_ms,
                "black_time_ms": game.black_time_ms,
                "increment_sec": game.clock_increment_sec,
            },
        },
    })
    return game


@router.post("/{game_id}/cancel", response_model=GameOut)
async def cancel_waiting_game(
    game_id: int,
    user: User = Depends(get_active_unbanned_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Host (White) may cancel a table that is still waiting for an opponent.
    The locked bet is released (total_invested reduced; refund transaction logged).
    """
    game = await _get_game_or_404(game_id, db)
    if game.status != GameStatus.WAITING:
        raise HTTPException(
            status_code=400,
            detail="Only games still waiting for an opponent can be cancelled. For an ongoing match, use Resign or finish the game.",
        )
    if game.white_player_id != user.id:
        raise HTTPException(status_code=403, detail="Only the host (White) can cancel this table.")
    if game.is_vs_cpu:
        raise HTTPException(status_code=400, detail="This game type cannot be cancelled from the lobby.")

    wallet = await _get_wallet_or_404(user.id, db)
    wallet.total_invested = max(0.0, round(wallet.total_invested - game.bet_amount, 2))
    db.add(
        Transaction(
            user_id=user.id,
            amount=game.bet_amount,
            type=TransactionType.REFUND,
            game_id=game.id,
            description=f"Lobby cancelled – bet unlocked – Game #{game.id}",
        )
    )
    game.status = GameStatus.ABANDONED
    game.ended_at = datetime.utcnow()
    db.add(wallet)
    db.add(game)
    await db.commit()
    await db.refresh(game)
    return game


@router.post("/{game_id}/move")
async def make_move_rest(
    game_id: int,
    payload: MoveRequest,
    user: User = Depends(get_active_unbanned_user),
    db: AsyncSession = Depends(get_db),
):
    """Make a move via REST (fallback for clients without WebSocket support)."""
    game = await _get_game_or_404(game_id, db)
    if game.status != GameStatus.ACTIVE:
        raise HTTPException(status_code=400, detail="Game is not active.")
    if user.id not in (game.white_player_id, game.black_player_id):
        raise HTTPException(status_code=403, detail="You are not a player in this game.")

    last_result = await db.execute(
        select(Move)
        .where(Move.game_id == game_id)
        .order_by(Move.id.desc())
        .limit(1)
    )
    last_move = last_result.scalar_one_or_none()
    last_time = last_move.created_at if last_move else game.started_at

    _, board, _move_time_ms, forfeited = await _apply_move(
        db, game, user, payload.move, payload.client_timestamp, last_time
    )

    if forfeited:
        await db.commit()
        await manager.broadcast_to_game(game_id, {
            "type": "game_over",
            "data": {
                "result": game.result,
                "reason": "time_forfeit",
                "winner_id": game.winner_id,
                "fen": game.fen,
            },
        })
        return JSONResponse(
            status_code=200,
            content={
                "time_forfeit": True,
                "game_id": game_id,
                "result": game.result,
                "winner_id": game.winner_id,
                "fen": game.fen,
            },
        )

    human_move_r = await db.execute(
        select(Move).where(Move.game_id == game_id).order_by(Move.id.desc()).limit(1)
    )
    human_move = human_move_r.scalar_one()

    game_over_after_human = board.is_game_over()
    if game_over_after_human:
        await _settle_game(db, game, board)

    cpu_move = None
    cpu_forfeit = False
    if (
        game.status == GameStatus.ACTIVE
        and game.is_vs_cpu
        and not game_over_after_human
    ):
        cpu_move, _, cpu_terminal = await _play_one_cpu_move_if_needed(db, game)
        if cpu_terminal and cpu_move is None:
            cpu_forfeit = True

    await db.commit()

    disp_b = _board_from_game(game)
    w_m, b_m = _clock_display_millis(game, disp_b)

    async def emit_move(mrec: Move, player_id: int, position_terminal: bool) -> None:
        await manager.broadcast_to_game(game_id, {
            "type": "move",
            "data": {
                "move_uci": mrec.move_uci,
                "move_san": mrec.move_san,
                "fen": game.fen,
                "player_id": player_id,
                "move_number": mrec.move_number,
                "game_over": position_terminal,
                "result": game.result,
                "white_time_ms": w_m,
                "black_time_ms": b_m,
            },
        })

    if cpu_forfeit:
        await emit_move(human_move, user.id, False)
        await manager.broadcast_to_game(game_id, {
            "type": "game_over",
            "data": {
                "result": game.result,
                "reason": "time_forfeit",
                "winner_id": game.winner_id,
                "fen": game.fen,
            },
        })
        return JSONResponse(
            status_code=200,
            content={
                "time_forfeit": True,
                "game_id": game_id,
                "result": game.result,
                "winner_id": game.winner_id,
                "fen": game.fen,
                "opponent_time_forfeit": True,
            },
        )

    await emit_move(human_move, user.id, game_over_after_human)

    if cpu_move:
        disp_b2 = _board_from_game(game)
        w2, b2 = _clock_display_millis(game, disp_b2)
        await manager.broadcast_to_game(game_id, {
            "type": "move",
            "data": {
                "move_uci": cpu_move.move_uci,
                "move_san": cpu_move.move_san,
                "fen": game.fen,
                "player_id": game.black_player_id,
                "move_number": cpu_move.move_number,
                "game_over": game.status == GameStatus.COMPLETED,
                "result": game.result,
                "white_time_ms": w2,
                "black_time_ms": b2,
            },
        })

    if game.status == GameStatus.COMPLETED:
        await manager.broadcast_to_game(game_id, {
            "type": "game_over",
            "data": {
                "result": game.result,
                "reason": "checkmate_or_draw",
                "winner_id": game.winner_id,
            },
        })

    out_move = cpu_move if cpu_move else human_move
    return MoveOut.model_validate(out_move)


@router.post("/{game_id}/resign", response_model=GameOut)
async def resign(
    game_id: int,
    user: User = Depends(get_active_unbanned_user),
    db: AsyncSession = Depends(get_db),
):
    """Resign the game. The opponent wins."""
    game = await _get_game_or_404(game_id, db)
    if game.status != GameStatus.ACTIVE:
        raise HTTPException(status_code=400, detail="Game is not active.")
    if user.id not in (game.white_player_id, game.black_player_id):
        raise HTTPException(status_code=403, detail="You are not a player in this game.")

    is_white = user.id == game.white_player_id
    result_str = "black" if is_white else "white"

    await _settle_by_result(db, game, result_str)
    await db.commit()
    await db.refresh(game)

    await manager.broadcast_to_game(game_id, {
        "type": "game_over",
        "data": {
            "result": result_str,
            "reason": "resignation",
            "winner_id": game.winner_id,
        },
    })
    return game


# ─── WebSocket ────────────────────────────────────────────────────────────────

@router.websocket("/ws/{game_id}")
async def websocket_game(
    websocket: WebSocket,
    game_id: int,
    token: str = Query(..., description="JWT access token"),
):
    """
    Real-time WebSocket endpoint.

    Connect with: ws://host/games/ws/{game_id}?token=<JWT>

    Incoming message types:
      {"type": "move", "data": {"move": "e2e4", "client_timestamp": 1700000000000}}
      {"type": "resign"}
      {"type": "ping"}

    Outgoing message types:
      {"type": "connected", "data": {...}}
      {"type": "game_started", "data": {...}}
      {"type": "move", "data": {...}}
      {"type": "game_over", "data": {...}}
      {"type": "error", "data": {"message": "..."}}
      {"type": "kicked", "data": {"reason": "..."}}
      {"type": "pong"}
    """
    # Authenticate
    try:
        from jose import jwt as _jwt
        from config import SECRET_KEY, ALGORITHM
        payload = _jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload["sub"])
    except Exception:
        await websocket.close(code=4001, reason="Invalid token")
        return

    async with AsyncSessionLocal() as db:
        user_result = await db.execute(select(User).where(User.id == user_id))
        user = user_result.scalar_one_or_none()
        if not user or not user.is_active or user.is_banned:
            await websocket.close(code=4003, reason="Forbidden")
            return

        game_result = await db.execute(select(Game).where(Game.id == game_id))
        game = game_result.scalar_one_or_none()
        if not game:
            await websocket.close(code=4004, reason="Game not found")
            return
        if user.id not in (game.white_player_id, game.black_player_id):
            await websocket.close(code=4003, reason="Not a player in this game")
            return

    await manager.connect(websocket, game_id, user_id)

    try:
        async with AsyncSessionLocal() as db:
            game_result = await db.execute(select(Game).where(Game.id == game_id))
            game = game_result.scalar_one()
            brd = _board_from_game(game)
            w_live, b_live = _clock_display_millis(game, brd)
            await websocket.send_json({
                "type": "connected",
                "data": {
                    "game_id": game_id,
                    "user_id": user_id,
                    "fen": game.fen,
                    "status": game.status.value,
                    "is_vs_cpu": game.is_vs_cpu,
                    "clock": {
                        "white_time_ms": w_live,
                        "black_time_ms": b_live,
                        "increment_sec": game.clock_increment_sec,
                    },
                },
            })

        while True:
            raw = await websocket.receive_json()
            msg_type = raw.get("type", "")
            data = raw.get("data", {})

            if msg_type == "ping":
                await websocket.send_json({"type": "pong"})
                continue

            if msg_type == "resign":
                async with AsyncSessionLocal() as db:
                    game_result = await db.execute(select(Game).where(Game.id == game_id))
                    game = game_result.scalar_one()

                    if game.status != GameStatus.ACTIVE:
                        await websocket.send_json({"type": "error", "data": {"message": "Game is not active."}})
                        continue

                    if user_id not in (game.white_player_id, game.black_player_id):
                        await websocket.send_json({"type": "error", "data": {"message": "Not a player in this game."}})
                        continue

                    is_white = user_id == game.white_player_id
                    result_str = "black" if is_white else "white"
                    await _settle_by_result(db, game, result_str)
                    wid = game.winner_id
                    await db.commit()

                await manager.broadcast_to_game(game_id, {
                    "type": "game_over",
                    "data": {
                        "result": result_str,
                        "reason": "resignation",
                        "winner_id": wid,
                    },
                })
                break

            if msg_type == "move":
                move_str = data.get("move", "")
                client_ts = data.get("client_timestamp")

                async with AsyncSessionLocal() as db:
                    game_result = await db.execute(select(Game).where(Game.id == game_id))
                    game = game_result.scalar_one()
                    user_result = await db.execute(select(User).where(User.id == user_id))
                    user = user_result.scalar_one()

                    if game.status != GameStatus.ACTIVE:
                        await websocket.send_json({"type": "error", "data": {"message": "Game is not active."}})
                        continue

                    last_result = await db.execute(
                        select(Move)
                        .where(Move.game_id == game_id)
                        .order_by(Move.id.desc())
                        .limit(1)
                    )
                    last_move = last_result.scalar_one_or_none()
                    last_time = last_move.created_at if last_move else game.started_at

                    try:
                        _, board, _, forfeited = await _apply_move(
                            db, game, user, move_str, client_ts, last_time
                        )
                    except HTTPException as exc:
                        await websocket.send_json({"type": "error", "data": {"message": exc.detail}})
                        if exc.status_code == 403:
                            break
                        continue

                    if forfeited:
                        await db.commit()
                        await manager.broadcast_to_game(game_id, {
                            "type": "game_over",
                            "data": {
                                "result": game.result,
                                "reason": "time_forfeit",
                                "winner_id": game.winner_id,
                                "fen": game.fen,
                            },
                        })
                        break

                    human_move_r = await db.execute(
                        select(Move).where(Move.game_id == game_id).order_by(Move.id.desc()).limit(1)
                    )
                    human_move = human_move_r.scalar_one()

                    game_over_after_human = board.is_game_over()
                    if game_over_after_human:
                        await _settle_game(db, game, board)

                    cpu_move = None
                    cpu_forfeit = False
                    if (
                        game.status == GameStatus.ACTIVE
                        and game.is_vs_cpu
                        and not game_over_after_human
                    ):
                        cpu_move, _, cpu_terminal = await _play_one_cpu_move_if_needed(db, game)
                        if cpu_terminal and cpu_move is None:
                            cpu_forfeit = True

                    await db.commit()

                    disp_b = _board_from_game(game)
                    w_m, b_m = _clock_display_millis(game, disp_b)

                    if cpu_forfeit:
                        await manager.broadcast_to_game(game_id, {
                            "type": "move",
                            "data": {
                                "move_uci": human_move.move_uci,
                                "move_san": human_move.move_san,
                                "fen": game.fen,
                                "player_id": user_id,
                                "move_number": human_move.move_number,
                                "game_over": False,
                                "result": game.result,
                                "white_time_ms": w_m,
                                "black_time_ms": b_m,
                            },
                        })
                        await manager.broadcast_to_game(game_id, {
                            "type": "game_over",
                            "data": {
                                "result": game.result,
                                "reason": "time_forfeit",
                                "winner_id": game.winner_id,
                                "fen": game.fen,
                            },
                        })
                        break

                    await manager.broadcast_to_game(game_id, {
                        "type": "move",
                        "data": {
                            "move_uci": human_move.move_uci,
                            "move_san": human_move.move_san,
                            "fen": game.fen,
                            "player_id": user_id,
                            "move_number": human_move.move_number,
                            "game_over": game_over_after_human,
                            "result": game.result,
                            "white_time_ms": w_m,
                            "black_time_ms": b_m,
                        },
                    })

                    if cpu_move:
                        disp_b2 = _board_from_game(game)
                        w2, b2 = _clock_display_millis(game, disp_b2)
                        await manager.broadcast_to_game(game_id, {
                            "type": "move",
                            "data": {
                                "move_uci": cpu_move.move_uci,
                                "move_san": cpu_move.move_san,
                                "fen": game.fen,
                                "player_id": game.black_player_id,
                                "move_number": cpu_move.move_number,
                                "game_over": game.status == GameStatus.COMPLETED,
                                "result": game.result,
                                "white_time_ms": w2,
                                "black_time_ms": b2,
                            },
                        })

                    if game.status == GameStatus.COMPLETED:
                        await manager.broadcast_to_game(game_id, {
                            "type": "game_over",
                            "data": {
                                "result": game.result,
                                "reason": "checkmate_or_draw",
                                "winner_id": game.winner_id,
                            },
                        })
                        break

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.exception("WebSocket error for user %d game %d: %s", user_id, game_id, e)
    finally:
        manager.disconnect(game_id, user_id)
