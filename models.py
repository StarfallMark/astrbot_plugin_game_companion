from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Literal

from .gomoku import Difficulty, GomokuGame

RoomSource = Literal["group", "private"]
RoomStatus = Literal[
    "waiting", "setup", "active", "finished", "rematch_pending", "paused", "closed"
]


@dataclass(slots=True)
class Visitor:
    """One browser identity inside a room."""

    number: int
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    joined_at: float = field(default_factory=time.time)
    last_seen_at: float = field(default_factory=time.time)
    connected: bool = True

    def public_snapshot(self, *, is_player: bool) -> dict[str, object]:
        """Return fields safe to show to all room members."""
        return {
            "number": self.number,
            "online": self.connected and time.time() - self.last_seen_at < 15,
            "is_player": is_player,
        }


@dataclass(slots=True)
class GameRoom:
    """In-memory room state. Active rooms intentionally do not survive reloads."""

    room_id: str
    access_token: str
    source: RoomSource
    session_id: str
    platform: str
    group_id: str
    creator_qq: str
    creator_name: str
    admin_room: bool
    difficulty: Difficulty
    created_at: float = field(default_factory=time.time)
    last_activity_at: float = field(default_factory=time.time)
    player_empty_since: float | None = field(default_factory=time.time)
    status: RoomStatus = "waiting"
    visitors: dict[str, Visitor] = field(default_factory=dict)
    next_visitor_number: int = 1
    player_token: str = ""
    player_qq: str = ""
    player_identity_confirmed: bool = False
    player_seat_locked: bool = False
    game: GomokuGame | None = None
    completed_games: int = 0
    human_wins: int = 0
    bot_wins: int = 0
    draws: int = 0
    messages: list[dict[str, object]] = field(default_factory=list)
    close_reason: str = ""
    last_commentary_at: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    conversation_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def player(self) -> Visitor | None:
        return self.visitors.get(self.player_token)

    def add_message(self, role: str, content: str) -> None:
        """Append a bounded game-related message for the WebUI."""
        self.messages.append(
            {"role": role, "content": content[:800], "at": time.time()}
        )
        del self.messages[:-30]

    def touch(self) -> None:
        """Mark a meaningful room operation."""
        self.last_activity_at = time.time()

    def public_snapshot(self, visitor_token: str = "") -> dict[str, object]:
        """Return room state without QQ identities or management secrets."""
        visitor = self.visitors.get(visitor_token)
        player = self.player
        return {
            "room_id": self.room_id,
            "game_type": "gomoku",
            "source": self.source,
            "admin_room": self.admin_room,
            "status": self.status,
            "created_at": self.created_at,
            "visitor_number": visitor.number if visitor else None,
            "is_player": bool(visitor and visitor.token == self.player_token),
            "player_number": player.number if player else None,
            "player_confirmed": self.player_identity_confirmed,
            "difficulty": self.difficulty,
            "visitors": [
                item.public_snapshot(is_player=item.token == self.player_token)
                for item in sorted(
                    self.visitors.values(), key=lambda value: value.number
                )
            ],
            "game": self.game.snapshot() if self.game else None,
            "score": {
                "human": self.human_wins,
                "bot": self.bot_wins,
                "draws": self.draws,
                "games": self.completed_games,
            },
            "messages": self.messages[-20:],
            "close_reason": self.close_reason,
        }

    def admin_snapshot(self) -> dict[str, object]:
        """Return operational data for the authenticated plugin page."""
        player = self.player
        return {
            "room_id": self.room_id,
            "source": self.source,
            "session_id": self.session_id,
            "group_id": self.group_id,
            "creator_qq": self.creator_qq,
            "creator_name": self.creator_name,
            "admin_room": self.admin_room,
            "status": self.status,
            "created_at": self.created_at,
            "last_activity_at": self.last_activity_at,
            "difficulty": self.difficulty,
            "player_number": player.number if player else None,
            "player_qq": self.player_qq,
            "player_confirmed": self.player_identity_confirmed,
            "visitors": [
                item.public_snapshot(is_player=item.token == self.player_token)
                for item in sorted(
                    self.visitors.values(), key=lambda value: value.number
                )
            ],
        }
