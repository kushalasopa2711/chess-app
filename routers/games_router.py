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
from datetime import datetime
from typing import Optional

import chess
import chess.pgn
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import anticheat
from auth import get_active_unbanned_user, get_current_user
from config import MAX_WALLET_BALANCE, MIN_BET, MAX_BET, PLATFORM_FEE_PERCENT, PAYOUT_HOLD_HOURS
from database import get_db, AsyncSessionLocal
from models import (
    Game, GameStatus, Move, PendingPayout, Transaction, TransactionType, User, Wallet
)
from schemas import GameCreate, GameDetail, GameListItem, GameOut, MoveOut, MoveRequest
from websocket_manager import manager

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


async def _settle_game(
    db: AsyncSession,
    game: Game,
    board: chess.Board,
) -> None:
    """
    End a game, release locked bets, and create PendingPayout records.

    Platform fee (PLATFORM_FEE_PERCENT %) is taken from every prize pool.
    Winnings are held in escrow for PAYOUT_HOLD_HOURS before admin review releases them.
    Draws are refunded immediately (no platform fee on draws).
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

    game.status = GameStatus.COMPLETED
    game.result = result_str
    game.ended_at = datetime.utcnow()

    white_wallet = await _get_wallet_or_404(game.white_player_id, db)
    black_wallet = await _get_wallet_or_404(game.black_player_id, db)
    white_r = await db.execute(select(User).where(User.id == game.white_player_id))
    black_r = await db.execute(select(User).where(User.id == game.black_player_id))
    white_user = white_r.scalar_one()
    black_user = black_r.scalar_one()

    # Always release the bet lock so funds aren't stuck
    white_wallet.total_invested = max(0.0, round(white_wallet.total_invested - game.bet_amount, 2))
    black_wallet.total_invested = max(0.0, round(black_wallet.total_invested - game.bet_amount, 2))

    if result_str == "draw":
        # Refund both players immediately — no platform fee on draws
        white_wallet.balance = round(white_wallet.balance + game.bet_amount, 2)
        black_wallet.balance = round(black_wallet.balance + game.bet_amount, 2)
        for uid in (game.white_player_id, game.black_player_id):
            db.add(Transaction(
                user_id=uid, amount=game.bet_amount,
                type=TransactionType.REFUND, game_id=game.id,
                description=f"Draw refund – Game #{game.id}",
            ))
    else:
        # There is a winner — apply platform fee and put winnings in escrow
        winner_id = game.white_player_id if result_str == "white" else game.black_player_id
        game.winner_id = winner_id

        gross = round(game.bet_amount * 2, 2)
        fee   = round(gross * PLATFORM_FEE_PERCENT / 100, 2)
        net   = round(gross - fee, 2)

        from datetime import timedelta
        auto_release = datetime.utcnow() + timedelta(hours=PAYOUT_HOLD_HOURS)

        payout = PendingPayout(
            user_id=winner_id,
            game_id=game.id,
            gross_amount=gross,
            platform_fee=fee,
            net_amount=net,
            status="pending",
            auto_release_at=auto_release,
        )
        db.add(payout)

        # Loser's bet is already consumed — record it as platform revenue
        loser_id = game.black_player_id if result_str == "white" else game.white_player_id
        db.add(Transaction(
            user_id=loser_id, amount=game.bet_amount,
            type=TransactionType.BET, game_id=game.id,
            description=f"Lost Game #{game.id} – bet forfeited",
        ))

        logger.info(
            "Game #%d ended: %s wins. Gross=₹%.2f Fee=₹%.2f Net=₹%.2f held 24h",
            game.id, result_str, gross, fee, net,
        )

    # Stats
    white_user.games_played += 1
    black_user.games_played += 1
    if result_str == "white":
        white_user.games_won += 1
    elif result_str == "black":
        black_user.games_won += 1

    db.add(game)
    db.add(white_wallet); db.add(black_wallet)
    db.add(white_user);   db.add(black_user)


async def _apply_move(
    db: AsyncSession,
    game: Game,
    player: User,
    move_str: str,
    client_timestamp_ms: Optional[int],
    last_move_time: Optional[datetime],
) -> tuple[chess.Move, chess.Board, Optional[int]]:
    """
    Validate and apply a chess move.
    Returns (move, updated board, move_time_ms).
    Raises HTTPException on illegal move or anti-cheat violation.
    """
    board = _board_from_game(game)

    # Determine whose turn it is
    is_white_turn = board.turn == chess.WHITE
    expected_player_id = game.white_player_id if is_white_turn else game.black_player_id
    if player.id != expected_player_id:
        raise HTTPException(status_code=400, detail="It is not your turn.")

    # Parse and validate move (UCI or SAN)
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

    # Compute timing
    move_time_ms: Optional[int] = None
    now = datetime.utcnow()
    if last_move_time:
        move_time_ms = int((now - last_move_time).total_seconds() * 1000)
    elif client_timestamp_ms:
        move_time_ms = client_timestamp_ms  # fallback to client-reported time

    # Anti-cheat timing flags
    flags = await anticheat.record_move_and_check(db, game.id, player.id, move_time_ms)
    for flag in flags:
        if flag.severity == 3:
            # Auto-ban
            player.is_banned = True
            player.ban_reason = flag.description
            db.add(player)
            await db.commit()
            raise HTTPException(
                status_code=403,
                detail=f"Account banned: {flag.description}",
            )

    # Apply move
    san = board.san(chess_move)
    fen_before = board.fen()
    board.push(chess_move)
    fen_after = board.fen()

    # Count total moves
    result = await db.execute(
        select(Move).where(Move.game_id == game.id)
    )
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

    return chess_move, board, move_time_ms


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
        q = q.where(Game.status == gs)
    q = q.order_by(Game.created_at.desc()).limit(100)
    result = await db.execute(q)
    rows = result.all()
    return [
        GameListItem(
            id=g.id,
            white_player_id=g.white_player_id,
            white_username=u.username,
            status=g.status.value,
            bet_amount=g.bet_amount,
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
    Create a new game as white.  The bet amount is locked from your wallet immediately.
    Another player must join before the game starts.
    """
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

    # Lock funds
    wallet.total_invested = round(wallet.total_invested + payload.bet_amount, 2)
    db.add(Transaction(
        user_id=user.id,
        amount=payload.bet_amount,
        type=TransactionType.BET,
        description=f"Bet locked for new game",
    ))

    game = Game(white_player_id=user.id, bet_amount=payload.bet_amount)
    db.add(game)
    await db.commit()
    await db.refresh(game)
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
    if wallet.total_invested + game.bet_amount > MAX_INVESTMENT_RUPEES:
        raise HTTPException(
            status_code=400,
            detail=f"Total at-stake cannot exceed ₹{MAX_INVESTMENT_RUPEES}.",
        )

    wallet.total_invested = round(wallet.total_invested + game.bet_amount, 2)
    game.black_player_id = user.id
    game.status = GameStatus.ACTIVE
    game.started_at = datetime.utcnow()

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
        },
    })
    return game


@router.post("/{game_id}/move", response_model=MoveOut)
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

    # Get time of last move for timing
    last_result = await db.execute(
        select(Move)
        .where(Move.game_id == game_id)
        .order_by(Move.id.desc())
        .limit(1)
    )
    last_move = last_result.scalar_one_or_none()
    last_time = last_move.created_at if last_move else game.started_at

    chess_move, board, move_time_ms = await _apply_move(
        db, game, user, payload.move, payload.client_timestamp, last_time
    )

    if board.is_game_over():
        await _settle_game(db, game, board)

    await db.commit()

    # Get the stored move record
    move_result = await db.execute(
        select(Move).where(Move.game_id == game_id).order_by(Move.id.desc()).limit(1)
    )
    move_record = move_result.scalar_one()

    # Broadcast to WebSocket clients
    await manager.broadcast_to_game(game_id, {
        "type": "move",
        "data": {
            "move_uci": move_record.move_uci,
            "move_san": move_record.move_san,
            "fen": game.fen,
            "player_id": user.id,
            "move_number": move_record.move_number,
            "game_over": board.is_game_over(),
            "result": game.result,
        },
    })
    return MoveOut.model_validate(move_record)


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
    winner_id = game.black_player_id if is_white else game.white_player_id

    game.status = GameStatus.COMPLETED
    game.result = result_str
    game.winner_id = winner_id
    game.ended_at = datetime.utcnow()

    # Settle wallets manually
    white_wallet = await _get_wallet_or_404(game.white_player_id, db)
    black_wallet = await _get_wallet_or_404(game.black_player_id, db)
    white_wallet.total_invested = max(0, round(white_wallet.total_invested - game.bet_amount, 2))
    black_wallet.total_invested = max(0, round(black_wallet.total_invested - game.bet_amount, 2))

    winner_wallet = black_wallet if is_white else white_wallet
    winner_wallet.balance = round(winner_wallet.balance + game.bet_amount * 2, 2)

    winner_result = await db.execute(select(User).where(User.id == winner_id))
    winner_user = winner_result.scalar_one()
    winner_user.games_won += 1
    loser_result = await db.execute(select(User).where(User.id == user.id))
    loser_user = loser_result.scalar_one()
    winner_user.games_played += 1
    loser_user.games_played += 1
    winner_user.total_earned = round(winner_user.total_earned + game.bet_amount, 2)

    db.add(Transaction(
        user_id=winner_id,
        amount=game.bet_amount * 2,
        type=TransactionType.WIN,
        game_id=game.id,
        description=f"Won game #{game.id} (opponent resigned)",
    ))

    db.add(game)
    await db.commit()
    await db.refresh(game)

    await manager.broadcast_to_game(game_id, {
        "type": "game_over",
        "data": {"result": result_str, "reason": "resignation", "winner_id": winner_id},
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
    from auth import get_current_user as _get_user
    from fastapi.security import HTTPAuthorizationCredentials
    from fastapi import Request

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
            await websocket.send_json({
                "type": "connected",
                "data": {
                    "game_id": game_id,
                    "user_id": user_id,
                    "fen": game.fen,
                    "status": game.status.value,
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
                    user_result = await db.execute(select(User).where(User.id == user_id))
                    user = user_result.scalar_one()

                    if game.status != GameStatus.ACTIVE:
                        await websocket.send_json({"type": "error", "data": {"message": "Game is not active."}})
                        continue

                    is_white = user_id == game.white_player_id
                    result_str = "black" if is_white else "white"
                    winner_id = game.black_player_id if is_white else game.white_player_id

                    game.status = GameStatus.COMPLETED
                    game.result = result_str
                    game.winner_id = winner_id
                    game.ended_at = datetime.utcnow()

                    white_wallet_r = await db.execute(select(Wallet).where(Wallet.user_id == game.white_player_id))
                    black_wallet_r = await db.execute(select(Wallet).where(Wallet.user_id == game.black_player_id))
                    white_wallet = white_wallet_r.scalar_one()
                    black_wallet = black_wallet_r.scalar_one()
                    white_wallet.total_invested = max(0, round(white_wallet.total_invested - game.bet_amount, 2))
                    black_wallet.total_invested = max(0, round(black_wallet.total_invested - game.bet_amount, 2))
                    winner_wallet = black_wallet if is_white else white_wallet
                    winner_wallet.balance = round(winner_wallet.balance + game.bet_amount * 2, 2)

                    winner_r = await db.execute(select(User).where(User.id == winner_id))
                    loser_r = await db.execute(select(User).where(User.id == user_id))
                    winner_u = winner_r.scalar_one()
                    loser_u = loser_r.scalar_one()
                    winner_u.games_won += 1
                    winner_u.games_played += 1
                    loser_u.games_played += 1
                    winner_u.total_earned = round(winner_u.total_earned + game.bet_amount, 2)

                    db.add(Transaction(
                        user_id=winner_id,
                        amount=game.bet_amount * 2,
                        type=TransactionType.WIN,
                        game_id=game.id,
                        description=f"Won game #{game.id} (opponent resigned)",
                    ))
                    await db.commit()

                await manager.broadcast_to_game(game_id, {
                    "type": "game_over",
                    "data": {"result": result_str, "reason": "resignation", "winner_id": winner_id},
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
                        chess_move, board, move_time_ms = await _apply_move(
                            db, game, user, move_str, client_ts, last_time
                        )
                    except HTTPException as exc:
                        await websocket.send_json({"type": "error", "data": {"message": exc.detail}})
                        if exc.status_code == 403:
                            break
                        continue

                    if board.is_game_over():
                        await _settle_game(db, game, board)

                    await db.commit()

                    move_record_result = await db.execute(
                        select(Move).where(Move.game_id == game_id).order_by(Move.id.desc()).limit(1)
                    )
                    move_record = move_record_result.scalar_one()

                broadcast_data = {
                    "type": "move",
                    "data": {
                        "move_uci": move_record.move_uci,
                        "move_san": move_record.move_san,
                        "fen": game.fen,
                        "player_id": user_id,
                        "move_number": move_record.move_number,
                        "game_over": board.is_game_over(),
                        "result": game.result,
                    },
                }
                await manager.broadcast_to_game(game_id, broadcast_data)
                if board.is_game_over():
                    await manager.broadcast_to_game(game_id, {
                        "type": "game_over",
                        "data": {"result": game.result, "reason": "checkmate_or_draw", "winner_id": game.winner_id},
                    })
                    break

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.exception("WebSocket error for user %d game %d: %s", user_id, game_id, e)
    finally:
        manager.disconnect(game_id, user_id)
