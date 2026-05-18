"""CPU opponent: alpha-beta negamax + capture quiescence (no Stockfish)."""
from __future__ import annotations

import chess

# ── Search limits (tune for server CPU; higher = stronger / slower) ─────────
MAIN_DEPTH = 4
QUIESCENCE_CAP = 6
MATE_SCORE = 100_000

PIECE_VAL = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 20000,
}

# Piece-square tables (centipawns), white POV — sq a1=0 … h8=63
_PAWN_PST = (
    0, 0, 0, 0, 0, 0, 0, 0,
    50, 50, 50, 50, 50, 50, 50, 50,
    10, 10, 20, 30, 30, 20, 10, 10,
    5, 5, 10, 25, 25, 10, 5, 5,
    0, 0, 0, 20, 20, 0, 0, 0,
    5, -5, -10, 0, 0, -10, -5, 5,
    5, 10, 10, -20, -20, 10, 10, 5,
    0, 0, 0, 0, 0, 0, 0, 0,
)

_KNIGHT_PST = (
    -50, -40, -30, -30, -30, -30, -40, -50,
    -40, -20, 0, 0, 0, 0, -20, -40,
    -30, 0, 10, 15, 15, 10, 0, -30,
    -30, 5, 15, 20, 20, 15, 5, -30,
    -30, 0, 15, 20, 20, 15, 0, -30,
    -30, 5, 10, 15, 15, 10, 5, -30,
    -40, -20, 0, 5, 5, 0, -20, -40,
    -50, -40, -30, -30, -30, -30, -40, -50,
)

_BISHOP_PST = (
    -20, -10, -10, -10, -10, -10, -10, -20,
    -10, 0, 0, 0, 0, 0, 0, -10,
    -10, 0, 5, 10, 10, 5, 0, -10,
    -10, 5, 5, 10, 10, 5, 5, -10,
    -10, 0, 10, 10, 10, 10, 0, -10,
    -10, 10, 10, 10, 10, 10, 10, -10,
    -10, 5, 0, 0, 0, 0, 5, -10,
    -20, -10, -10, -10, -10, -10, -10, -20,
)

_ROOK_PST = (
    0, 0, 0, 0, 0, 0, 0, 0,
    5, 10, 10, 10, 10, 10, 10, 5,
    -5, 0, 0, 0, 0, 0, 0, -5,
    -5, 0, 0, 0, 0, 0, 0, -5,
    -5, 0, 0, 0, 0, 0, 0, -5,
    -5, 0, 0, 0, 0, 0, 0, -5,
    -5, 0, 0, 0, 0, 0, 0, -5,
    0, 0, 0, 5, 5, 0, 0, 0,
)

_QUEEN_PST = (
    -20, -10, -10, -5, -5, -10, -10, -20,
    -10, 0, 0, 0, 0, 0, 0, -10,
    -10, 0, 5, 5, 5, 5, 0, -10,
    -5, 0, 5, 5, 5, 5, 0, -5,
    0, 0, 5, 5, 5, 5, 5, -5,
    -10, 5, 5, 5, 5, 5, 0, -10,
    -10, 0, 5, 5, 5, 5, 0, -10,
    -20, -10, -10, -5, -5, -10, -10, -20,
)

_KING_MID_PST = (
    -30, -40, -40, -50, -50, -40, -40, -30,
    -30, -40, -40, -50, -50, -40, -40, -30,
    -30, -40, -40, -50, -50, -40, -40, -30,
    -30, -40, -40, -50, -50, -40, -40, -30,
    -20, -30, -30, -40, -40, -30, -30, -20,
    -20, -30, -30, -40, -40, -30, -30, -20,
    -20, -30, -30, -40, -40, -30, -30, -20,
    -20, -30, -30, -40, -40, -30, -30, -20,
)


def _pst_for_square(piece: chess.Piece, square: int) -> int:
    if piece.piece_type == chess.PAWN:
        pst = _PAWN_PST
    elif piece.piece_type == chess.KNIGHT:
        pst = _KNIGHT_PST
    elif piece.piece_type == chess.BISHOP:
        pst = _BISHOP_PST
    elif piece.piece_type == chess.ROOK:
        pst = _ROOK_PST
    elif piece.piece_type == chess.QUEEN:
        pst = _QUEEN_PST
    elif piece.piece_type == chess.KING:
        pst = _KING_MID_PST
    else:
        return 0
    idx = square if piece.color == chess.WHITE else chess.square_mirror(square)
    return pst[idx]


def evaluate_white_pov(board: chess.Board) -> int:
    """Positive = White is better (material + PST)."""
    score = 0
    for sq in chess.SQUARES:
        p = board.piece_at(sq)
        if p is None:
            continue
        v = PIECE_VAL[p.piece_type] + _pst_for_square(p, sq)
        if p.color == chess.WHITE:
            score += v
        else:
            score -= v
    return score


def evaluate_side_to_move(board: chess.Board) -> int:
    """Positive = good for whoever is to move."""
    w = evaluate_white_pov(board)
    return w if board.turn == chess.WHITE else -w


def _mvv_lva_capture_sort_key(board: chess.Board, move: chess.Move) -> tuple[int, int]:
    if not board.is_capture(move):
        return (0, 0)
    victim = board.piece_at(move.to_square)
    attacker = board.piece_at(move.from_square)
    vv = PIECE_VAL.get(victim.piece_type, 0) if victim else 0
    av = PIECE_VAL.get(attacker.piece_type, 0) if attacker else 0
    return (vv, -av)


def _order_moves(board: chess.Board, moves: list[chess.Move]) -> list[chess.Move]:
    def key(m: chess.Move) -> tuple[int, int, int]:
        cap = _mvv_lva_capture_sort_key(board, m)
        promo = 1 if m.promotion else 0
        chk = 2 if board.gives_check(m) else 0
        return (chk, promo, cap[0] * 250 + cap[1])

    return sorted(moves, key=key, reverse=True)


def _quiescence(board: chess.Board, alpha: int, beta: int, depth: int) -> int:
    stand = evaluate_side_to_move(board)
    if stand >= beta:
        return stand
    if alpha < stand:
        alpha = stand
    if depth <= 0:
        return alpha

    for move in _order_moves(board, list(board.legal_moves)):
        if not board.is_capture(move) and move.promotion is None:
            continue
        board.push(move)
        score = -_quiescence(board, -beta, -alpha, depth - 1)
        board.pop()
        if score >= beta:
            return beta
        if score > alpha:
            alpha = score
    return alpha


def _negamax(board: chess.Board, depth: int, alpha: int, beta: int) -> int:
    if board.is_game_over():
        if board.is_checkmate():
            # Side to move is mated
            return -MATE_SCORE + board.fullmove_number
        return 0

    if depth == 0:
        return _quiescence(board, alpha, beta, QUIESCENCE_CAP)

    best = -MATE_SCORE * 2
    for move in _order_moves(board, list(board.legal_moves)):
        board.push(move)
        score = -_negamax(board, depth - 1, -beta, -alpha)
        board.pop()
        if score > best:
            best = score
        if score > alpha:
            alpha = score
        if alpha >= beta:
            break
    return best


def choose_cpu_move(board: chess.Board) -> chess.Move:
    """Best move for Black (CPU); fixed-depth alpha-beta."""
    legal = list(board.legal_moves)
    if not legal:
        raise RuntimeError("No legal moves for CPU")
    if board.turn != chess.BLACK:
        return legal[0]

    best_move = legal[0]
    best_score = -MATE_SCORE * 2
    root_alpha = -MATE_SCORE * 2
    root_beta = MATE_SCORE * 2

    for move in _order_moves(board, legal):
        board.push(move)
        score = -_negamax(board, MAIN_DEPTH - 1, -root_beta, -root_alpha)
        board.pop()
        if score > best_score:
            best_score = score
            best_move = move
        if score > root_alpha:
            root_alpha = score

    return best_move
