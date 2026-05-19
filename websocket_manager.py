"""
WebSocket connection manager.

Two channels are maintained:

  - **Game rooms**: one connection per (game_id, user_id). Used for live move
    broadcasts during play. Multi-tab is rejected (prevents multi-session
    cheating).
  - **User channels**: zero-or-more connections per user_id, used to push
    account-wide notifications (wallet credits, withdrawal status changes,
    payout approvals, etc.) so the UI updates without requiring a refresh.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)

# {game_id: {user_id: WebSocket}}
_connections: Dict[int, Dict[int, WebSocket]] = {}
# {user_id: [WebSocket, ...]}  — one user may have multiple tabs open.
_user_channels: Dict[int, List[WebSocket]] = {}


class ConnectionManager:
    # ── Game-room channels ────────────────────────────────────────────────
    async def connect(
        self, websocket: WebSocket, game_id: int, user_id: int
    ) -> bool:
        await websocket.accept()
        if game_id not in _connections:
            _connections[game_id] = {}

        existing = _connections[game_id].get(user_id)
        if existing is not None:
            try:
                await existing.send_json({
                    "type": "kicked",
                    "data": {"reason": "New session opened from another location."},
                })
                await existing.close(code=4001)
            except Exception:
                pass

        _connections[game_id][user_id] = websocket
        logger.info("User %d connected to game %d", user_id, game_id)
        return True

    def disconnect(self, game_id: int, user_id: int) -> None:
        room = _connections.get(game_id, {})
        room.pop(user_id, None)
        if not room:
            _connections.pop(game_id, None)
        logger.info("User %d disconnected from game %d", user_id, game_id)

    async def send_to_player(self, game_id: int, user_id: int, message: dict) -> None:
        ws = _connections.get(game_id, {}).get(user_id)
        if ws:
            try:
                await ws.send_json(message)
            except Exception as e:
                logger.warning("Failed sending to user %d: %s", user_id, e)

    async def broadcast_to_game(
        self,
        game_id: int,
        message: dict,
        exclude_user: Optional[int] = None,
    ) -> None:
        room = _connections.get(game_id, {})
        for uid, ws in list(room.items()):
            if uid == exclude_user:
                continue
            try:
                await ws.send_json(message)
            except Exception as e:
                logger.warning("Broadcast failed for user %d: %s", uid, e)

    def active_players_in_game(self, game_id: int) -> list[int]:
        return list(_connections.get(game_id, {}).keys())

    # ── Per-user notification channels ────────────────────────────────────
    async def connect_user(self, websocket: WebSocket, user_id: int) -> None:
        await websocket.accept()
        _user_channels.setdefault(user_id, []).append(websocket)
        logger.info("User %d opened a notification channel (now %d open).",
                    user_id, len(_user_channels[user_id]))

    def disconnect_user(self, user_id: int, websocket: WebSocket) -> None:
        channels = _user_channels.get(user_id, [])
        if websocket in channels:
            channels.remove(websocket)
        if not channels:
            _user_channels.pop(user_id, None)
        logger.info("User %d closed a notification channel.", user_id)

    async def push_to_user(self, user_id: int, message: dict) -> int:
        """
        Best-effort fan-out to every open tab/device for ``user_id``.
        Returns the number of sockets the message reached. Failed sockets are
        culled so the next push doesn't try them again.
        """
        channels = list(_user_channels.get(user_id, []))
        delivered = 0
        for ws in channels:
            try:
                await ws.send_json(message)
                delivered += 1
            except Exception as e:
                logger.info("Pruning dead user channel for %d: %s", user_id, e)
                self.disconnect_user(user_id, ws)
        return delivered


manager = ConnectionManager()
