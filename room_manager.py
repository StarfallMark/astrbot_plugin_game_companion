from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .gomoku import BLACK, WHITE, Difficulty, GomokuGame
from .models import GameRoom, GameType, RoomSource, Visitor
from .pikafish import PikafishService
from .tictactoe import NOUGHT as TICTACTOE_NOUGHT
from .tictactoe import X as TICTACTOE_X
from .tictactoe import TicTacToeGame
from .xiangqi import BLACK as XIANGQI_BLACK
from .xiangqi import RED as XIANGQI_RED
from .xiangqi import XiangqiGame

RoomCallback = Callable[[str, GameRoom, dict[str, Any]], Awaitable[None]]


class RoomManager:
    """Own room quotas, seats, game transitions, and expiry rules."""

    CLOSED_ACCESS_TTL_SECONDS = 15 * 60
    MAX_CLOSED_ACCESS_RECORDS = 256
    FINISHED_PLAYER_LEAVE_GRACE_SECONDS = 8
    FINISHED_PLAYER_HEARTBEAT_TIMEOUT_SECONDS = 60

    def __init__(
        self,
        *,
        max_group_rooms: int = 1,
        max_private_rooms: int = 1,
        empty_player_timeout: int = 60,
        idle_timeout: int = 300,
        xiangqi_engine: PikafishService | None = None,
        event_callback: RoomCallback | None = None,
    ) -> None:
        self.max_group_rooms = max(0, int(max_group_rooms))
        self.max_private_rooms = max(0, int(max_private_rooms))
        self.empty_player_timeout = max(0, int(empty_player_timeout))
        self.idle_timeout = max(0, int(idle_timeout))
        self.xiangqi_engine = xiangqi_engine
        self.event_callback = event_callback
        self.rooms: dict[str, GameRoom] = {}
        self._access_index: dict[str, str] = {}
        self._closed_access: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    async def create_room(
        self,
        *,
        source: RoomSource,
        session_id: str,
        platform: str,
        group_id: str,
        creator_qq: str,
        creator_name: str,
        admin_room: bool,
        game_type: GameType = "gomoku",
        difficulty: Difficulty,
    ) -> GameRoom:
        """Create a room atomically under the source-wide quota."""
        async with self._lock:
            limit = (
                self.max_group_rooms if source == "group" else self.max_private_rooms
            )
            active_count = sum(room.source == source for room in self.rooms.values())
            if limit and active_count >= limit:
                label = "群聊" if source == "group" else "私聊"
                raise ValueError(f"{label}房间已达到并行上限 {limit}")
            room_id = secrets.token_hex(4)
            while room_id in self.rooms:
                room_id = secrets.token_hex(4)
            access_token = secrets.token_urlsafe(32)
            room = GameRoom(
                room_id=room_id,
                access_token=access_token,
                source=source,
                session_id=session_id,
                platform=platform,
                group_id=group_id,
                creator_qq=creator_qq,
                creator_name=creator_name,
                admin_room=admin_room,
                game_type=game_type,
                difficulty=difficulty,
            )
            self.rooms[room_id] = room
            self._access_index[access_token] = room_id
            return room

    def by_access_token(self, access_token: str) -> GameRoom | None:
        """Resolve an unguessable public room token."""
        room_id = self._access_index.get(str(access_token or ""))
        return self.rooms.get(room_id or "")

    def closed_reason_by_access_token(self, access_token: str) -> str:
        """Return a short-lived close reason without retaining room data."""
        self._purge_closed_access()
        record = self._closed_access.get(str(access_token or ""))
        return record[0] if record else ""

    def for_session(self, session_id: str) -> list[GameRoom]:
        """Return rooms attached to one real AstrBot conversation."""
        return [room for room in self.rooms.values() if room.session_id == session_id]

    async def join(self, room: GameRoom, visitor_token: str = "") -> Visitor:
        """Resume a browser identity or assign the next stable room number."""
        async with room.lock:
            visitor = room.visitors.get(str(visitor_token or ""))
            if visitor is None:
                visitor = Visitor(number=room.next_visitor_number)
                room.next_visitor_number += 1
                room.visitors[visitor.token] = visitor
            visitor.connected = True
            visitor.last_seen_at = time.time()
            visitor.left_at = None
            return visitor

    async def heartbeat(self, room: GameRoom, visitor_token: str) -> None:
        """Refresh presence without extending the room activity deadline."""
        async with room.lock:
            visitor = self._visitor(room, visitor_token)
            visitor.connected = True
            visitor.last_seen_at = time.time()
            visitor.left_at = None

    async def leave(self, room: GameRoom, visitor_token: str) -> None:
        """Record a browser departure without extending meaningful activity."""
        async with room.lock:
            visitor = self._visitor(room, visitor_token)
            visitor.connected = False
            visitor.left_at = time.time()

    async def claim_and_start(
        self, room: GameRoom, visitor_token: str, side: str
    ) -> None:
        """Claim a normal room's empty player seat and start the first game."""
        async with room.lock:
            visitor = self._visitor(room, visitor_token)
            if room.admin_room:
                raise ValueError("这个房间需要管理员从游戏管理台安排玩家")
            if room.player_token and room.player_token != visitor.token:
                raise ValueError("玩家席已经有人，请联系创建者处理")
            if room.player_seat_locked and room.player_token != visitor.token:
                raise ValueError("玩家席已由创建者锁定")
            room.player_token = visitor.token
            room.player_qq = room.creator_qq
            room.player_empty_since = None
            room.status = "setup"
            room.touch()
        await self.start_game(room, visitor_token, side)

    async def assign_player(
        self, room: GameRoom, visitor_number: int, player_qq: str
    ) -> None:
        """Assign an administrator-reviewed visitor and QQ identity."""
        async with room.lock:
            visitor = self._visitor_by_number(room, visitor_number)
            player_qq = str(player_qq or "").strip()
            if not player_qq.isdigit():
                raise ValueError("玩家 QQ 号必须只包含数字")
            room.player_token = visitor.token
            room.player_qq = player_qq
            room.player_identity_confirmed = True
            room.player_seat_locked = True
            room.player_empty_since = None
            room.game = None
            room.status = "setup"
            room.touch()
        await self._emit("player_confirmed", room, {})

    async def correct_creator(
        self, room: GameRoom, actor_qq: str, visitor_number: int
    ) -> None:
        """Reset a stolen normal room and swap the creator into the player seat."""
        async with room.lock:
            if room.admin_room:
                raise ValueError("管理员房间请从游戏管理台重新安排玩家")
            if str(actor_qq) != room.creator_qq:
                raise PermissionError("只有房间创建者能纠正玩家身份")
            visitor = self._visitor_by_number(room, visitor_number)
            room.player_token = visitor.token
            room.player_qq = room.creator_qq
            room.player_identity_confirmed = True
            room.player_seat_locked = True
            room.player_empty_since = None
            room.game = None
            room.status = "setup"
            room.touch()
            room.add_message(
                "system", f"身份已纠正：{visitor.number} 号成为玩家，对局已重置。"
            )
        await self._emit("player_confirmed", room, {"corrected": True})

    async def confirm_creator(self, room: GameRoom, actor_qq: str) -> None:
        """Confirm that the current browser player is the room creator."""
        async with room.lock:
            if str(actor_qq) != room.creator_qq:
                raise PermissionError("只有房间创建者能确认自己的身份")
            if not room.player_token:
                raise ValueError("当前还没有人在玩家席")
            room.player_qq = room.creator_qq
            room.player_identity_confirmed = True
            room.player_seat_locked = True
            room.touch()
        await self._emit("player_confirmed", room, {})

    async def start_game(self, room: GameRoom, visitor_token: str, side: str) -> None:
        """Start a game after a seat has been assigned."""
        async with room.lock:
            visitor = self._visitor(room, visitor_token)
            if visitor.token != room.player_token:
                raise PermissionError("只有当前玩家可以开始对局")
            if room.status not in {"setup", "finished", "rematch_pending"}:
                raise ValueError("当前房间状态不能开始新对局")
            if room.game_type == "xiangqi":
                engine = self._require_xiangqi_engine()
                normalized_side = str(side or "human_red").strip().lower()
                if normalized_side not in {"human_red", "human_black", "random"}:
                    normalized_side = "human_red"
                if normalized_side == "random":
                    normalized_side = secrets.choice(("human_red", "human_black"))
                human_side = (
                    XIANGQI_RED if normalized_side == "human_red" else XIANGQI_BLACK
                )
                room.game = await XiangqiGame.create(
                    engine,
                    human_side=human_side,
                    difficulty=room.difficulty,
                )
                side_label = "红" if human_side == XIANGQI_RED else "黑"
            elif room.game_type == "gomoku":
                normalized_side = str(side or "human_black").strip().lower()
                if normalized_side not in {"human_black", "bot_black", "random"}:
                    normalized_side = "human_black"
                if normalized_side == "random":
                    normalized_side = secrets.choice(("human_black", "bot_black"))
                human_color = BLACK if normalized_side == "human_black" else WHITE
                room.game = GomokuGame(
                    human_color=human_color, difficulty=room.difficulty
                )
                side_label = "黑" if human_color == BLACK else "白"
            else:
                normalized_side = str(side or "human_x").strip().lower()
                if normalized_side not in {"human_x", "human_o", "random"}:
                    normalized_side = "human_x"
                if normalized_side == "random":
                    normalized_side = secrets.choice(("human_x", "human_o"))
                human_mark = (
                    TICTACTOE_X
                    if normalized_side == "human_x"
                    else TICTACTOE_NOUGHT
                )
                room.game = TicTacToeGame(
                    human_mark=human_mark, difficulty=room.difficulty
                )
                side_label = "X" if human_mark == TICTACTOE_X else "O"
            room.status = "active"
            room.touch()
            room.add_message(
                "system",
                f"新对局开始，玩家执{side_label}，Bot 使用{self._difficulty_label(room.difficulty)}棋力。",
            )
        await self._emit("game_started", room, {})
        if self._is_bot_turn(room):
            await self._bot_turn(room)

    async def player_move(
        self,
        room: GameRoom,
        visitor_token: str,
        *,
        row: int = -1,
        column: int = -1,
        from_row: int = -1,
        from_column: int = -1,
        to_row: int = -1,
        to_column: int = -1,
    ) -> None:
        """Apply one browser move, then calculate the Bot response off-loop."""
        async with room.lock:
            visitor = self._visitor(room, visitor_token)
            if visitor.token != room.player_token:
                raise PermissionError("当前浏览器不在玩家席")
            if room.status != "active" or room.game is None:
                raise ValueError("当前没有正在进行的对局")
            if isinstance(room.game, XiangqiGame):
                await room.game.place_human(
                    self._require_xiangqi_engine(),
                    int(from_row),
                    int(from_column),
                    int(to_row),
                    int(to_column),
                )
            elif isinstance(room.game, GomokuGame):
                room.game.place(int(row), int(column), room.game.human_color)
            else:
                room.game.place(int(row), int(column), room.game.human_mark)
            room.touch()
            finished = room.game.finished
        await self._emit("board_changed", room, {"actor": "human"})
        if finished:
            await self._finish_game(room)
            return
        await self._bot_turn(room)

    async def request_rematch(self, room: GameRoom, visitor_token: str) -> None:
        """Put a finished room into a Bot-decided rematch request state."""
        async with room.lock:
            visitor = self._visitor(room, visitor_token)
            if visitor.token != room.player_token:
                raise PermissionError("只有当前玩家能申请再来一局")
            if room.status != "finished":
                raise ValueError("当前还不能申请再来一局")
            room.status = "rematch_pending"
            room.touch()
            room.add_message("user", "想再来一局。")
        await self._emit("rematch_requested", room, {})

    async def resolve_rematch(
        self,
        room: GameRoom,
        *,
        accepted: bool,
        message: str = "",
        difficulty: Difficulty | None = None,
    ) -> bool:
        """Apply a pending Bot decision and reject stale concurrent decisions."""
        async with room.lock:
            if room.status != "rematch_pending":
                return False
            if message:
                room.add_message("bot", message)
            if accepted:
                if difficulty is not None:
                    room.difficulty = difficulty
                room.game = None
                room.status = "setup"
                room.touch()
                return True
        if not accepted:
            await self.destroy(room.room_id, "Bot 没有接受再来一局")
        return True

    async def restart_finished_game(
        self, room: GameRoom, *, difficulty: Difficulty
    ) -> None:
        """Start another game in the same room, preserving its player and score."""
        async with room.lock:
            if not room.player_token or room.player is None:
                raise ValueError("当前房间没有可以继续对局的玩家")
            if room.status not in {"finished", "rematch_pending"} or room.game is None:
                raise ValueError("当前对局尚未结束，不能直接再来一局")
            room.difficulty = difficulty
            if isinstance(room.game, XiangqiGame):
                human_side = room.game.human_side
                room.game = await XiangqiGame.create(
                    self._require_xiangqi_engine(),
                    human_side=human_side,
                    difficulty=difficulty,
                )
                side_label = "红" if human_side == XIANGQI_RED else "黑"
            elif isinstance(room.game, GomokuGame):
                human_color = room.game.human_color
                room.game = GomokuGame(
                    human_color=human_color, difficulty=difficulty
                )
                side_label = "黑" if human_color == BLACK else "白"
            else:
                human_mark = room.game.human_mark
                room.game = TicTacToeGame(
                    human_mark=human_mark, difficulty=difficulty
                )
                side_label = "X" if human_mark == TICTACTOE_X else "O"
            room.status = "active"
            room.touch()
            room.add_message(
                "system",
                f"新对局开始，玩家继续执{side_label}，Bot 使用{self._difficulty_label(difficulty)}棋力。",
            )
        await self._emit("game_started", room, {"rematch": True})
        if self._is_bot_turn(room):
            await self._bot_turn(room)

    async def undo(self, room: GameRoom) -> int:
        """Undo the latest complete player round after a QQ-side decision."""
        async with room.lock:
            if room.status != "active" or room.game is None:
                raise ValueError("当前没有可以悔棋的对局")
            if isinstance(room.game, XiangqiGame):
                removed = await room.game.undo_round(self._require_xiangqi_engine())
            else:
                removed = room.game.undo_round()
            room.touch()
            room.add_message("system", "Bot 同意了悔棋。")
            return removed

    async def resign(self, room: GameRoom) -> None:
        """Finish the current game as a Bot win."""
        async with room.lock:
            if room.status != "active" or room.game is None:
                raise ValueError("当前没有正在进行的对局")
            if isinstance(room.game, XiangqiGame):
                room.game.winner = room.game.bot_side
            elif isinstance(room.game, GomokuGame):
                room.game.winner = room.game.bot_color
            else:
                room.game.winner = room.game.bot_mark
            room.touch()
        await self._finish_game(room)

    async def remove_player(
        self, room: GameRoom, *, reason: str = "玩家已被移到观众席"
    ) -> None:
        """Clear the player seat and reset the unfinished game."""
        async with room.lock:
            room.player_token = ""
            room.player_qq = ""
            room.player_identity_confirmed = False
            room.player_seat_locked = room.admin_room
            room.player_empty_since = time.time()
            room.game = None
            room.status = "waiting"
            room.touch()
            room.add_message("system", reason)

    async def kick_visitor(self, room: GameRoom, visitor_number: int) -> None:
        """Invalidate one browser identity and clear its seat if necessary."""
        async with room.lock:
            visitor = self._visitor_by_number(room, visitor_number)
            was_player = visitor.token == room.player_token
            room.visitors.pop(visitor.token, None)
            if was_player:
                room.player_token = ""
                room.player_qq = ""
                room.player_identity_confirmed = False
                room.player_empty_since = time.time()
                room.game = None
                room.status = "waiting"
            room.touch()
            room.add_message("system", f"{visitor.number} 号已被移出房间。")

    async def pause(self, room: GameRoom) -> None:
        """Pause an active game without changing its board."""
        async with room.lock:
            if room.status != "active":
                raise ValueError("当前对局不能暂停")
            room.status = "paused"
            room.touch()
            room.add_message("system", "对局已暂停。")

    async def resume(self, room: GameRoom) -> None:
        """Resume a paused game."""
        async with room.lock:
            if room.status != "paused" or room.game is None:
                raise ValueError("当前没有已暂停的对局")
            room.status = "active"
            room.touch()
            room.add_message("system", "对局继续。")
            bot_turn = self._game_is_bot_turn(room.game)
        if bot_turn:
            await self._bot_turn(room)

    async def switch_game(
        self,
        room: GameRoom,
        game_type: GameType,
        *,
        force: bool = False,
    ) -> bool:
        """Switch one room's game while preserving access, seats, and scores."""
        if game_type not in {"gomoku", "xiangqi", "tictactoe"}:
            raise ValueError("不支持的游戏类型")
        if game_type == "xiangqi":
            await self._require_xiangqi_engine().ensure_ready()
        async with room.lock:
            if room.game_type == game_type:
                return False
            if room.status in {"active", "paused"} and not force:
                raise ValueError("当前对局尚未结束，需要明确放弃本局后才能切换游戏")
            previous = room.game_type
            room.game_type = game_type
            room.game = None
            room.status = "setup" if room.player_token else "waiting"
            room.player_empty_since = None if room.player_token else time.time()
            room.touch()
            room.add_message(
                "system",
                f"游戏已从{self._game_label(previous)}切换为{self._game_label(game_type)}。",
            )
        await self._emit(
            "game_switched", room, {"from": previous, "to": game_type, "forced": force}
        )
        return True

    async def destroy(self, room_id: str, reason: str) -> GameRoom | None:
        """Destroy a room and release its quota exactly once."""
        async with self._lock:
            room = self.rooms.pop(str(room_id or ""), None)
            if room is None:
                return None
            self._access_index.pop(room.access_token, None)
            room.status = "closed"
            room.close_reason = str(reason or "房间已结束")[:200]
            self._closed_access[room.access_token] = (
                room.close_reason,
                time.time() + self.CLOSED_ACCESS_TTL_SECONDS,
            )
            self._purge_closed_access()
        await self._emit("room_destroyed", room, {"reason": room.close_reason})
        return room

    async def sweep_expired(self, *, now: float | None = None) -> list[str]:
        """Destroy rooms that exceed the configured empty or idle timeout."""
        current = time.time() if now is None else float(now)
        expired: list[tuple[str, str]] = []
        for room in list(self.rooms.values()):
            player = room.player
            if room.status == "finished" and player is not None:
                explicitly_left = (
                    player.left_at is not None
                    and current - player.left_at
                    >= self.FINISHED_PLAYER_LEAVE_GRACE_SECONDS
                )
                heartbeat_lost = (
                    current - player.last_seen_at
                    >= self.FINISHED_PLAYER_HEARTBEAT_TIMEOUT_SECONDS
                )
                if explicitly_left or heartbeat_lost:
                    expired.append(
                        (room.room_id, "本局结束后玩家已离开，房间已自动销毁")
                    )
                    continue
            if (
                self.empty_player_timeout
                and room.player_empty_since is not None
                and current - room.player_empty_since >= self.empty_player_timeout
            ):
                expired.append((room.room_id, "玩家席长时间无人，房间已自动销毁"))
                continue
            if (
                self.idle_timeout
                and current - room.last_activity_at >= self.idle_timeout
            ):
                expired.append((room.room_id, "房间长时间无操作，已自动销毁"))
        for room_id, reason in expired:
            await self.destroy(room_id, reason)
        return [room_id for room_id, _reason in expired]

    async def close_all(self, reason: str = "插件已重载") -> None:
        """Destroy every in-memory room without persistence."""
        for room_id in list(self.rooms):
            await self.destroy(room_id, reason)

    async def _bot_turn(self, room: GameRoom) -> None:
        await asyncio.sleep(
            {"easy": 0.55, "normal": 0.85, "hard": 1.05}[room.difficulty]
        )
        async with room.lock:
            if room.status != "active" or room.game is None or room.game.finished:
                return
            game = room.game
            if isinstance(game, XiangqiGame):
                try:
                    await game.place_bot(self._require_xiangqi_engine())
                except Exception:
                    room.status = "paused"
                    room.add_message(
                        "system",
                        "象棋引擎暂时不可用，对局已暂停。恢复引擎后可在 QQ 或管理台继续。",
                    )
                    raise
            elif isinstance(game, GomokuGame):
                move = await asyncio.to_thread(game.choose_bot_move)
                game.place(move[0], move[1], game.bot_color)
            else:
                move = game.choose_bot_move()
                game.place(move[0], move[1], game.bot_mark)
            finished = game.finished
        await self._emit("board_changed", room, {"actor": "bot"})
        if finished:
            await self._finish_game(room)

    async def _finish_game(self, room: GameRoom) -> None:
        async with room.lock:
            if room.game is None or not room.game.finished or room.status == "finished":
                return
            room.status = "finished"
            room.completed_games += 1
            if room.game.draw:
                room.draws += 1
                result = "draw"
            elif self._game_human_won(room.game):
                room.human_wins += 1
                result = "human_win"
            else:
                room.bot_wins += 1
                result = "bot_win"
            room.touch()
        await self._emit("game_finished", room, {"result": result})

    async def _emit(self, event: str, room: GameRoom, payload: dict[str, Any]) -> None:
        if self.event_callback is not None:
            await self.event_callback(event, room, payload)

    def _purge_closed_access(self) -> None:
        now = time.time()
        expired = [
            token
            for token, (_reason, expires_at) in self._closed_access.items()
            if expires_at <= now
        ]
        for token in expired:
            self._closed_access.pop(token, None)
        overflow = len(self._closed_access) - self.MAX_CLOSED_ACCESS_RECORDS
        if overflow > 0:
            oldest = sorted(
                self._closed_access, key=lambda token: self._closed_access[token][1]
            )[:overflow]
            for token in oldest:
                self._closed_access.pop(token, None)

    @staticmethod
    def _visitor(room: GameRoom, token: str) -> Visitor:
        visitor = room.visitors.get(str(token or ""))
        if visitor is None:
            raise PermissionError("访客身份无效，请重新打开房间")
        return visitor

    @staticmethod
    def _visitor_by_number(room: GameRoom, number: int) -> Visitor:
        visitor = next(
            (item for item in room.visitors.values() if item.number == int(number)),
            None,
        )
        if visitor is None:
            raise ValueError(f"房间内没有 {number} 号访客")
        return visitor

    @staticmethod
    def _difficulty_label(difficulty: Difficulty) -> str:
        return {"easy": "简单", "normal": "普通", "hard": "困难"}[difficulty]

    def _require_xiangqi_engine(self) -> PikafishService:
        if self.xiangqi_engine is None:
            raise RuntimeError("象棋引擎服务尚未配置")
        return self.xiangqi_engine

    def _is_bot_turn(self, room: GameRoom) -> bool:
        return bool(room.game and self._game_is_bot_turn(room.game))

    @staticmethod
    def _game_is_bot_turn(
        game: GomokuGame | XiangqiGame | TicTacToeGame,
    ) -> bool:
        if isinstance(game, XiangqiGame):
            return game.turn == game.bot_side
        if isinstance(game, GomokuGame):
            return game.turn == game.bot_color
        return game.turn == game.bot_mark

    @staticmethod
    def _game_human_won(
        game: GomokuGame | XiangqiGame | TicTacToeGame,
    ) -> bool:
        if isinstance(game, XiangqiGame):
            return game.winner == game.human_side
        if isinstance(game, GomokuGame):
            return game.winner == game.human_color
        return game.winner == game.human_mark

    @staticmethod
    def _game_label(game_type: GameType) -> str:
        return {
            "gomoku": "五子棋",
            "xiangqi": "象棋",
            "tictactoe": "井字棋",
        }[game_type]
