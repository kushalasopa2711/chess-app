"""Simple CPU move picker (no Stockfish) — capture-preferring heuristic."""
from __future__ import annotations

import chess

PIECE_VAL = {
    chess.PAWN: 1,
    chess.KNIGHT: 3,
    chess.BISHOP: 3,
    chess.ROOK: 5,
    chess.QUEEN: 9,
    chess.KING: 0,
}


def choose_cpu_move(board: chess.Board) -> chess.Move:
    legal = list(board.legal_moves)
    if not legal:
        raise RuntimeError("No legal moves for CPU")

    def score_move(m: chess.Move) -> tuple[int, int]:
        # Higher is better: (capture value, promotion bonus)
        bonus = 0
        if board.is_capture(m):
            victim = board.piece_at(m.to_square)
            bonus = 100 + (PIECE_VAL.get(victim.piece_type, 0) if victim else 0)
        if m.promotion:
            bonus += 50
        # Prefer center pawn moves slightly
        if board.piece_at(m.from_square) and board.piece_at(m.from_square).piece_type == chess.PAWN:
            to_r, to_f = chess.square_rank(m.to_square), chess.square_file(m.to_square)
            if 2 <= to_r <= 5 and 2 <= to_f <= 5:
                bonus += 1
        return (bonus, 0)

    legal.sort(key=lambda m: score_move(m), reverse=True)
    return legal[0]
