from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Literal

from .gomoku import Difficulty, GomokuGame
from .pig_dice import PigDiceGame
from .tictactoe import TicTacToeGame
from .turtle_soup import TurtleSoupGame
from .xiangqi import XiangqiGame

RoomSource = Literal["group", "private"]
GameType = Literal["gomoku", "xiangqi", "tictactoe", "turtle_soup", "pig_dice"]
RoomStatus = Literal[
    "waiting", "setup", "active", "finished", "rematch_pending", "paused", "closed"
]
TurtleSoupMode = Literal["bot_host", "player_host"]


@dataclass(slots=True)
class GameScore:
    completed: int = 0
    human_wins: int = 0
    bot_wins: int = 0
    draws: int = 0


@dataclass(slots=True)
class TurtleSoupStats:
    questions: int = 0
    hints: int = 0
    answer_attempts: int = 0


@dataclass(slots=True)
class PlayerSeat:
    """A browser visitor occupying one reusable multiplayer seat."""

    visitor_token: str
    qq: str = ""
    identity_confirmed: bool = False
    seated_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class SeatSwapRequest:
    """A short-lived request from one spectator to one occupied seat."""

    request_id: str
    requester_token: str
    target_token: str
    created_at: float
    expires_at: float


@dataclass(slots=True)
class MultiplayerState:
    """Game-agnostic seats, round order and spectator swap requests."""

    enabled: bool = False
    capacity: int = 1
    turn_timeout_seconds: int = 0
    swap_cooldown_seconds: int = 0
    swap_request_expiry_seconds: int = 20
    seats: list[PlayerSeat] = field(default_factory=list)
    current_turn_index: int = 0
    turn_deadline: float = 0.0
    swap_requests: dict[str, SeatSwapRequest] = field(default_factory=dict)
    last_swap_request_at: dict[str, float] = field(default_factory=dict)

    @property
    def current_token(self) -> str:
        if not self.seats:
            return ""
        self.current_turn_index %= len(self.seats)
        return self.seats[self.current_turn_index].visitor_token

    def seat_for_token(self, token: str) -> PlayerSeat | None:
        return next(
            (seat for seat in self.seats if seat.visitor_token == str(token or "")),
            None,
        )

    def seat_for_qq(self, qq: str) -> PlayerSeat | None:
        return next(
            (seat for seat in self.seats if seat.qq and seat.qq == str(qq or "")),
            None,
        )


@dataclass(slots=True)
class Visitor:
    """One browser identity inside a room."""

    number: int
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    joined_at: float = field(default_factory=time.time)
    last_seen_at: float = field(default_factory=time.time)
    left_at: float | None = None
    connected: bool = True

    def public_snapshot(
        self, *, is_player: bool, is_current_player: bool = False
    ) -> dict[str, object]:
        """Return fields safe to show to all room members."""
        return {
            "number": self.number,
            "online": self.connected and time.time() - self.last_seen_at < 15,
            "is_player": is_player,
            "is_current_player": is_current_player,
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
    game_type: GameType
    difficulty: Difficulty
    turtle_soup_mode: TurtleSoupMode = "bot_host"
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
    confirmed_participant_qqs: set[str] = field(default_factory=set)
    multiplayer: MultiplayerState = field(default_factory=MultiplayerState)
    game: (
        GomokuGame | XiangqiGame | TicTacToeGame | TurtleSoupGame | PigDiceGame | None
    ) = None
    scores: dict[GameType, GameScore] = field(
        default_factory=lambda: {
            "gomoku": GameScore(),
            "xiangqi": GameScore(),
            "tictactoe": GameScore(),
            "turtle_soup": GameScore(),
            "pig_dice": GameScore(),
        }
    )
    turtle_soup_stats: TurtleSoupStats = field(default_factory=TurtleSoupStats)
    turtle_soup_recent_signatures: list[str] = field(default_factory=list)
    messages: list[dict[str, object]] = field(default_factory=list)
    close_reason: str = ""
    last_commentary_at: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def player(self) -> Visitor | None:
        return self.visitors.get(self.player_token)

    @property
    def current_score(self) -> GameScore:
        return self.scores[self.game_type]

    @property
    def completed_games(self) -> int:
        return self.current_score.completed

    @completed_games.setter
    def completed_games(self, value: int) -> None:
        self.current_score.completed = int(value)

    @property
    def human_wins(self) -> int:
        return self.current_score.human_wins

    @human_wins.setter
    def human_wins(self, value: int) -> None:
        self.current_score.human_wins = int(value)

    @property
    def bot_wins(self) -> int:
        return self.current_score.bot_wins

    @bot_wins.setter
    def bot_wins(self, value: int) -> None:
        self.current_score.bot_wins = int(value)

    @property
    def draws(self) -> int:
        return self.current_score.draws

    @draws.setter
    def draws(self, value: int) -> None:
        self.current_score.draws = int(value)

    def add_message(self, role: str, content: str) -> None:
        """Append a bounded game-related message for the WebUI."""
        self.messages.append(
            {
                "role": role,
                "content": content[:800],
                "at": time.time(),
                "game_type": self.game_type,
            }
        )
        del self.messages[:-30]

    def touch(self) -> None:
        """Mark a meaningful room operation."""
        self.last_activity_at = time.time()

    def public_snapshot(self, visitor_token: str = "") -> dict[str, object]:
        """Return room state without QQ identities or management secrets."""
        visitor = self.visitors.get(visitor_token)
        player = self.player
        multiplayer = self.multiplayer
        player_tokens = (
            [seat.visitor_token for seat in multiplayer.seats]
            if multiplayer.enabled
            else ([self.player_token] if self.player_token else [])
        )
        current_token = (
            multiplayer.current_token if multiplayer.enabled else self.player_token
        )
        now = time.time()
        incoming_requests: list[dict[str, object]] = []
        outgoing_request: dict[str, object] | None = None
        visitor_seat = (
            multiplayer.seat_for_token(visitor.token)
            if multiplayer.enabled and visitor
            else None
        )
        if multiplayer.enabled and visitor:
            for swap in multiplayer.swap_requests.values():
                requester = self.visitors.get(swap.requester_token)
                target = self.visitors.get(swap.target_token)
                if swap.target_token == visitor.token and requester:
                    incoming_requests.append(
                        {
                            "request_id": swap.request_id,
                            "requester_number": requester.number,
                            "expires_at": swap.expires_at,
                        }
                    )
                if swap.requester_token == visitor.token and target:
                    outgoing_request = {
                        "request_id": swap.request_id,
                        "target_number": target.number,
                        "expires_at": swap.expires_at,
                    }
        return {
            "room_id": self.room_id,
            "game_type": self.game_type,
            "source": self.source,
            "admin_room": self.admin_room,
            "status": self.status,
            "created_at": self.created_at,
            "visitor_number": visitor.number if visitor else None,
            "is_player": bool(visitor and visitor.token in player_tokens),
            "is_current_player": bool(visitor and visitor.token == current_token),
            "player_number": player.number if player else None,
            "player_numbers": [
                self.visitors[token].number
                for token in player_tokens
                if token in self.visitors
            ],
            "player_capacity": multiplayer.capacity if multiplayer.enabled else 1,
            "multiplayer_enabled": multiplayer.enabled,
            "current_player_number": (
                self.visitors[current_token].number
                if current_token in self.visitors
                else None
            ),
            "turn_deadline": multiplayer.turn_deadline if multiplayer.enabled else 0,
            "turn_timeout_seconds": (
                multiplayer.turn_timeout_seconds if multiplayer.enabled else 0
            ),
            "incoming_swap_requests": incoming_requests,
            "outgoing_swap_request": outgoing_request,
            "swap_cooldown_until": (
                multiplayer.last_swap_request_at.get(visitor.token, 0)
                + multiplayer.swap_cooldown_seconds
                if multiplayer.enabled and visitor
                else 0
            ),
            "server_time": now,
            "player_confirmed": (
                visitor_seat.identity_confirmed
                if visitor_seat is not None
                else self.player_identity_confirmed
            ),
            "turtle_soup_mode": self.turtle_soup_mode,
            "difficulty": self.difficulty,
            "visitors": [
                item.public_snapshot(
                    is_player=item.token in player_tokens,
                    is_current_player=item.token == current_token,
                )
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
            "turtle_soup_stats": {
                "questions": self.turtle_soup_stats.questions,
                "hints": self.turtle_soup_stats.hints,
                "answer_attempts": self.turtle_soup_stats.answer_attempts,
            },
            "messages": self.messages[-20:],
            "close_reason": self.close_reason,
        }

    def admin_snapshot(self) -> dict[str, object]:
        """Return operational data for the authenticated plugin page."""
        player = self.player
        multiplayer = self.multiplayer
        player_tokens = (
            [seat.visitor_token for seat in multiplayer.seats]
            if multiplayer.enabled
            else ([self.player_token] if self.player_token else [])
        )
        current_token = (
            multiplayer.current_token if multiplayer.enabled else self.player_token
        )
        return {
            "room_id": self.room_id,
            "source": self.source,
            "session_id": self.session_id,
            "group_id": self.group_id,
            "creator_qq": self.creator_qq,
            "creator_name": self.creator_name,
            "admin_room": self.admin_room,
            "game_type": self.game_type,
            "turtle_soup_mode": self.turtle_soup_mode,
            "status": self.status,
            "created_at": self.created_at,
            "last_activity_at": self.last_activity_at,
            "difficulty": self.difficulty,
            "scores": {
                game_type: {
                    "human": score.human_wins,
                    "bot": score.bot_wins,
                    "draws": score.draws,
                    "games": score.completed,
                }
                for game_type, score in self.scores.items()
            },
            "turtle_soup_progress": self._turtle_soup_progress(),
            "pig_dice_progress": self._pig_dice_progress(),
            "player_number": player.number if player else None,
            "player_numbers": [
                self.visitors[token].number
                for token in player_tokens
                if token in self.visitors
            ],
            "player_capacity": multiplayer.capacity if multiplayer.enabled else 1,
            "current_player_number": (
                self.visitors[current_token].number
                if current_token in self.visitors
                else None
            ),
            "player_qq": self.player_qq,
            "player_confirmed": self.player_identity_confirmed,
            "visitors": [
                {
                    **item.public_snapshot(
                        is_player=item.token in player_tokens,
                        is_current_player=item.token == current_token,
                    ),
                    "player_qq": (
                        multiplayer.seat_for_token(item.token).qq
                        if multiplayer.enabled
                        and multiplayer.seat_for_token(item.token) is not None
                        else self.player_qq
                        if item.token == self.player_token
                        else ""
                    ),
                }
                for item in sorted(
                    self.visitors.values(), key=lambda value: value.number
                )
            ],
        }

    def _turtle_soup_progress(self) -> dict[str, object] | None:
        if not isinstance(self.game, TurtleSoupGame):
            return None
        return {
            "phase": self.game.phase,
            "processing": self.game.processing,
            "question_count": self.game.question_count,
            "hints_used": self.game.hints_used,
            "answer_attempts": self.game.answer_attempts,
            "solved": self.game.solved,
            "gave_up": self.game.gave_up,
            "content_level": self.game.content_level,
            "mode": self.game.mode,
            "turn_count": self.game.turn_count,
        }

    def _pig_dice_progress(self) -> dict[str, object] | None:
        if not isinstance(self.game, PigDiceGame):
            return None
        return {
            "turn": self.game.turn,
            "human_score": self.game.human_score,
            "bot_score": self.game.bot_score,
            "turn_total": self.game.turn_total,
            "target_score": self.game.target_score,
            "risk_style": self.game.bot_risk_style,
        }
