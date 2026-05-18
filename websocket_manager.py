"""
WebSocket connection manager.

Enforces one connection per player per game to prevent multi-session cheating.
"""
from __future__ import annotations

import json
import logging
from typing import Dict, Optional, Tuple

from fastapi import WebSocket

logger = logging.getLogger(__name__)

# {game_id: {user_id: WebSocket}}
_connections: Dict[int, Dict[int, WebSocket]] = {}


class ConnectionManager:
    async def connect(
        self, websocket: WebSocket, game_id: int, user_id: int
    ) -> bool:
        """
        Accept a WebSocket. If the player already has a connection for this game,
        close the old one first (prevents multi-tab cheating).
        Returns True if connected, False if rejected.
        """
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


manager = ConnectionManager()
