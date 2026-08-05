from __future__ import annotations

import asyncio
import base64
import binascii
import inspect
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlsplit

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api.web import request

from .draw_guess import DrawGuessGame
from .gomoku import Difficulty, GomokuGame
from .models import GameRoom, GameType, TurtleSoupMode, Visitor
from .pig_dice import PigDiceGame
from .pikafish import PikafishService
from .room_manager import RoomManager
from .server import GameRoomServer
from .tictactoe import NOUGHT as TICTACTOE_NOUGHT
from .tictactoe import TicTacToeGame
from .tictactoe import X as TICTACTOE_X
from .tunnel import QuickTunnel
from .turtle_soup import (
    VERDICT_LABELS,
    TurtleSoupGame,
    fallback_puzzle,
    normalize_content_level,
    puzzle_from_mapping,
)
from .turtle_soup_ai import (
    answer_judge_prompt,
    extract_json_object,
    generation_prompt,
    parse_answer_judgment,
    parse_question_judgment,
    parse_reverse_turn,
    public_judge_history,
    question_judge_prompt,
    reverse_public_history,
    reverse_turn_prompt,
    validation_passed,
    validation_prompt,
)
from .xiangqi import BLACK as XIANGQI_BLACK
from .xiangqi import RED as XIANGQI_RED
from .xiangqi import XiangqiGame

PLUGIN_NAME = "astrbot_plugin_game_companion"
PLUGIN_VERSION = "0.1.9"
PAGE_API_PREFIX = f"/{PLUGIN_NAME}/page"


@register(
    PLUGIN_NAME,
    "StarfallMark",
    "让 Bot 与用户通过可视化房间自然地一起玩游戏。",
    PLUGIN_VERSION,
)
class GameCompanionPlugin(Star):
    """Game rooms that preserve AstrBot's normal conversation pipeline."""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self.plugin_root = Path(__file__).resolve().parent
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.server_enabled = self._cfg_bool("server.enabled", True)
        self.server_host = self._cfg_str("server.host", "127.0.0.1") or "127.0.0.1"
        self.server_port = self._cfg_int("server.port", 6331, minimum=1, maximum=65535)
        self.public_base_url = self._validated_public_url(
            self._cfg_str("server.public_base_url", "")
        )
        self.auto_quick_tunnel = self._cfg_bool("server.auto_quick_tunnel", True)

        self.group_rooms_enabled = self._cfg_bool("rooms.enable_group_rooms", True)
        self.private_rooms_enabled = self._cfg_bool("rooms.enable_private_rooms", True)
        self.allow_non_admin_group_creation = self._cfg_bool(
            "rooms.allow_non_admin_group_creation", False
        )
        self.game_admin_ids = self._parse_qq_ids(
            self._cfg("rooms.game_admin_qq_ids", "")
        )
        self.record_shared_experience = self._cfg_bool(
            "memory.record_shared_experience", True
        )
        self.companion_afterglow_enabled = self._cfg_bool(
            "companion_integration.enable_emotional_afterglow", False
        )
        self.companion_invites_enabled = self._cfg_bool(
            "companion_integration.enable_proactive_invites", False
        )
        self.companion_invite_probability = self._cfg_int(
            "companion_integration.proactive_invite_probability_percent",
            18,
            minimum=0,
            maximum=100,
        ) / 100.0
        self.companion_invite_cooldown_hours = self._cfg_int(
            "companion_integration.proactive_invite_cooldown_hours",
            24,
            minimum=0,
            maximum=24 * 30,
        )
        self.commentary_cooldown = self._cfg_int(
            "game.commentary_cooldown_seconds", 45, minimum=10, maximum=600
        )
        self.turtle_soup_max_hints = self._cfg_int(
            "turtle_soup.max_hints", 3, minimum=0, maximum=8
        )
        self.turtle_soup_content_level = normalize_content_level(
            self._cfg("turtle_soup.content_level", "normal")
        )
        self.turtle_soup_max_players = self._cfg_non_negative(
            "turtle_soup.max_players", 6
        )
        self.draw_guess_vision_provider_id = self._cfg_str(
            "draw_guess.vision_provider_id", ""
        )
        self.draw_guess_max_guesses = self._cfg_int(
            "draw_guess.max_guesses", 5, minimum=1, maximum=10
        )
        self.draw_guess_duration_seconds = self._cfg_int(
            "draw_guess.duration_seconds", 120, minimum=10, maximum=600
        )
        self.multiplayer_turn_timeout = self._cfg_non_negative(
            "multiplayer.turn_timeout_seconds", 60
        )
        self.swap_request_cooldown = self._cfg_non_negative(
            "multiplayer.swap_request_cooldown_seconds", 30
        )
        self.swap_request_expiry = self._cfg_int(
            "multiplayer.swap_request_expiry_seconds", 20, minimum=1, maximum=600
        )

        self.xiangqi_engine = PikafishService(
            data_dir=self.data_dir,
            configured_path=self._cfg_str("xiangqi.engine_path", ""),
            download_proxy=self._cfg_str("xiangqi.download_proxy", ""),
            allow_download=self._cfg_bool("xiangqi.allow_engine_download", True),
            auto_download=self._cfg_bool("xiangqi.auto_download_engine", False),
        )

        self.manager = RoomManager(
            max_group_rooms=self._cfg_non_negative("rooms.max_group_rooms", 1),
            max_private_rooms=self._cfg_non_negative("rooms.max_private_rooms", 1),
            empty_player_timeout=self._cfg_non_negative(
                "rooms.empty_player_timeout_seconds", 60
            ),
            idle_timeout=self._cfg_non_negative("rooms.idle_timeout_seconds", 300),
            turtle_soup_max_hints=self.turtle_soup_max_hints,
            turtle_soup_content_level=self.turtle_soup_content_level,
            turtle_soup_max_players=self.turtle_soup_max_players,
            multiplayer_turn_timeout=self.multiplayer_turn_timeout,
            swap_request_cooldown=self.swap_request_cooldown,
            swap_request_expiry=self.swap_request_expiry,
            draw_guess_max_guesses=self.draw_guess_max_guesses,
            draw_guess_duration_seconds=self.draw_guess_duration_seconds,
            xiangqi_engine=self.xiangqi_engine,
            event_callback=self._on_room_event,
        )
        self.room_server = GameRoomServer(
            self,
            self.manager,
            host=self.server_host,
            port=self.server_port,
            web_root=self.plugin_root / "web",
        )
        self.quick_tunnel = QuickTunnel(
            self.room_server.local_base_url,
            search_paths=[
                self.data_dir.parent.parent / "tools" / "bin",
                self.plugin_root / "tools",
            ],
        )
        self._watchdog_task: asyncio.Task | None = None
        self._tunnel_recovery_task: asyncio.Task | None = None
        self._next_tunnel_retry_at = 0.0
        self._background_tasks: set[asyncio.Task] = set()
        self._companion_round_event_tasks: dict[str, asyncio.Task] = {}
        self._companion_invite_api: Any | None = None
        self._next_companion_registration_at = 0.0
        self._register_page_api()

    async def initialize(self) -> None:
        """Start only the in-memory watchdog; the port opens lazily on demand."""
        self._watchdog_task = asyncio.create_task(self._watchdog())
        self._register_companion_invite_ability()
        logger.info(
            "[GameCompanion] 游戏伴侣已加载；房间服务将在首次创建房间时按需启动"
        )

    async def terminate(self) -> None:
        """Invalidate every room and stop only plugin-owned resources."""
        self._unregister_companion_invite_ability()
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            await asyncio.gather(self._watchdog_task, return_exceptions=True)
            self._watchdog_task = None
        await self.manager.close_all("AstrBot 或游戏插件已重载")
        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()
        await self.quick_tunnel.stop()
        await self.room_server.stop()
        await self.xiangqi_engine.close()
        logger.info("[GameCompanion] 所有运行态房间均已销毁")

    @filter.llm_tool(name="game_companion_create_room")
    async def create_room_tool(self, event: AstrMessageEvent, **kwargs: Any) -> str:
        """仅在用户明确想和 Bot 玩游戏时创建可视化游戏房间。

        难度必须由你结合当前人格、关系和用户请求自行决定，不能把难度选择交给网页用户。
        支持 gomoku（五子棋）、xiangqi（中国象棋）、tictactoe（井字棋）、
        turtle_soup（海龟汤）、pig_dice（贪心骰子）和 draw_guess（你画我猜）。
        不要因为普通聊天中偶然提到游戏名称就调用本工具。
        当前 QQ 会话已有房间时只返回原房间入口；切换游戏、再来一局和其他局内操作
        全部由用户进入 WebUI 后完成，不能在 QQ 中代替用户执行。

        Args:
            game_type(string): 游戏类型，只能是 gomoku、xiangqi、tictactoe、turtle_soup、pig_dice 或 draw_guess。
            difficulty(string): 你决定使用的难度，只能是 easy、normal、hard；贪心骰子中分别表示稳健、均衡和大胆。
            turtle_soup_mode(string): 海龟汤玩法；bot_host 表示 Bot 出题玩家猜，player_host 表示玩家给线索 Bot 猜。非海龟汤时忽略。
            confirm_abandon(boolean): 切换游戏且当前局未结束时，用户是否已明确同意放弃本局。
        """
        try:
            game_type = self._game_type(kwargs.get("game_type"))
        except ValueError as exc:
            return self._json_error(str(exc))
        difficulty = self._difficulty(kwargs.get("difficulty"))
        turtle_soup_mode = self._turtle_soup_mode(kwargs.get("turtle_soup_mode"))
        try:
            room, reused, restarted = await self._create_or_reuse_room_from_event(
                event,
                difficulty,
                game_type,
                turtle_soup_mode=turtle_soup_mode,
                confirm_abandon=self._value_bool(kwargs.get("confirm_abandon")),
            )
            url = self._room_url(room)
            link_delivered = await self._deliver_room_link(
                room,
                url,
                reused=reused,
                restarted=restarted,
            )
        except (ValueError, RuntimeError, PermissionError, OSError) as exc:
            return self._json_error(str(exc))
        return json.dumps(
            {
                "ok": True,
                "room_id": room.room_id,
                "room_url": "" if link_delivered else url,
                "link_delivered": link_delivered,
                "game_type": room.game_type,
                "difficulty": room.difficulty,
                "admin_room": room.admin_room,
                "turtle_soup_mode": room.turtle_soup_mode,
                "reused_room": reused,
                "restarted_game": restarted,
                "entry_timeout_seconds": self.manager.empty_player_timeout,
                "instruction": (
                    "房间链接已由插件作为独立纯文字消息发送；正常延续人格聊天，"
                    "不要复述、改写或重新生成链接。"
                    if link_delivered
                    else "已复用当前会话的原房间，不得关闭它或创建新房间；完整保留 room_url。"
                    if reused
                    else self._room_link_instruction(room)
                ),
            },
            ensure_ascii=False,
        )

    @filter.llm_tool(name="game_companion_control_room")
    async def control_room_tool(self, event: AstrMessageEvent, **kwargs: Any) -> str:
        """引导用户到 WebUI 完成游戏房间操作。

        QQ 只用于创建房间、取得入口和绑定身份。悔棋、暂停、继续、认输、再来一局、
        切换游戏和结束房间均不能在 QQ 中执行。

        Args:
            action(string): status、undo、pause、resume、resign、rematch、switch_game、close。
            room_id(string): 可选房间编号；当前会话只有一个房间时可以留空。
        """
        _ = event, kwargs
        return self._json_error(
            "游戏内操作已移至 WebUI，请打开当前房间后在 Bot 对话栏中操作"
        )

    @filter.llm_tool(name="game_companion_turtle_soup")
    async def turtle_soup_tool(self, event: AstrMessageEvent, **kwargs: Any) -> str:
        """引导用户到 WebUI 继续海龟汤问答。

        Args:
            action(string): Bot 出题模式使用 ask、answer、hint；玩家出题模式使用 respond 或 correct。
            text(string): 问题、完整推理，或玩家给 Bot 的公开回答/线索。
            room_id(string): 可选房间编号；当前会话只有一个房间时可以留空。
        """
        _ = event, kwargs
        return self._json_error("海龟汤问答已移至 WebUI，请在房间的 Bot 对话栏中继续")

    @filter.command("游戏伴侣")
    async def game_companion_status(self, event: AstrMessageEvent):
        """Return a small fallback status without taking over ordinary chat."""
        rooms = self.manager.for_session(event.unified_msg_origin)
        if not rooms:
            yield event.plain_result(
                "当前会话没有活动游戏房间。直接告诉我想玩五子棋、象棋、井字棋、海龟汤、贪心骰子或你画我猜即可。"
            )
            return
        labels = [
            f"{room.room_id}：{self._game_label(room.game_type)}，{room.status}"
            for room in rooms
        ]
        yield event.plain_result("当前游戏房间：\n" + "\n".join(labels))

    @filter.command_group("game")
    def game_commands(self):
        """游戏伴侣的显式 QQ 指令。"""
        pass

    @game_commands.command("游戏菜单", alias={"菜单", "menu"})
    async def game_menu(self, event: AstrMessageEvent):
        """列出游戏和全局房间容量。"""
        group_count = sum(
            room.source == "group" for room in self.manager.rooms.values()
        )
        private_count = sum(
            room.source == "private" for room in self.manager.rooms.values()
        )

        def capacity(current: int, limit: int, enabled: bool) -> str:
            maximum = "不限" if limit == 0 else str(limit)
            state = "允许创建" if enabled else "已关闭创建"
            return f"{current}/{maximum}（{state}）"

        lines = [
            "游戏伴侣 · 游戏菜单",
            "",
            "1. 五子棋：15×15 连成五子",
            "2. 中国象棋：使用 Pikafish 引擎",
            "3. 井字棋：三连即可获胜",
            "4. 海龟汤：通过是非提问还原汤底",
            "5. 贪心骰子：继续掷或收手，先到 50 分获胜",
            "6. 你画我猜：用户在网页作画，Bot 通过视觉模型猜词",
            "",
            "房间容量",
            f"群聊：{capacity(group_count, self.manager.max_group_rooms, self.group_rooms_enabled)}",
            f"私聊：{capacity(private_count, self.manager.max_private_rooms, self.private_rooms_enabled)}",
            "",
            "直接用自然语言告诉 Bot 想玩哪个游戏即可。",
        ]
        yield event.plain_result("\n".join(lines))

    async def _bind_game_player_text(
        self, event: AstrMessageEvent, identity_token: str
    ) -> str:
        """Bind a browser visitor to the QQ sender and return a short reply."""
        try:
            _room, visitor = await self.manager.bind_visitor_identity(
                session_id=event.unified_msg_origin,
                identity_token=identity_token,
                qq=str(event.get_sender_id() or "").strip(),
                display_name=str(event.get_sender_name() or "").strip(),
            )
        except (ValueError, RuntimeError, PermissionError) as exc:
            return f"玩家身份绑定失败：{exc}"
        label = (
            f"{visitor.display_name}（{visitor.number}号）"
            if visitor.display_name
            else f"{visitor.number}号玩家"
        )
        return f"已将你绑定为本房间的 {label}。请回到网页点击“加入玩家席”。"

    @filter.command("绑定玩家", alias={"绑定令牌"})
    async def bind_game_player(self, event: AstrMessageEvent, identity_token: str):
        """Bind a browser visitor using the one-time token shown in its WebUI."""
        yield event.plain_result(
            await self._bind_game_player_text(event, identity_token)
        )

    @filter.regex(r"^[A-HJ-NP-Za-hj-np-z2-9]{8}$")
    async def bind_game_player_bare_token(self, event: AstrMessageEvent):
        """Also accept a bare token when the user explicitly addresses the Bot."""
        if not getattr(event, "is_at_or_wake_command", False):
            return
        yield event.plain_result(
            await self._bind_game_player_text(event, event.message_str.strip())
        )

    @filter.on_llm_request(priority=-10)
    async def inject_game_context(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """Keep QQ conversation context isolated from every active WebUI room."""
        _ = event, req

    @staticmethod
    def _live_game_state(room: GameRoom) -> list[str]:
        game = room.game
        score = room.current_score
        lines = [
            f"本房间累计：玩家胜 {score.human_wins}，Bot 胜 {score.bot_wins}，"
            f"平局 {score.draws}，已完成 {score.completed}。"
        ]
        if game is None:
            lines.append("当前尚未开始具体一局。")
            return lines

        if isinstance(game, PigDiceGame):
            if game.human_score == game.bot_score:
                advantage = "双方已存总分相同"
            elif game.human_score > game.bot_score:
                advantage = f"玩家已存总分领先 {game.human_score - game.bot_score} 分"
            else:
                advantage = f"Bot 已存总分领先 {game.bot_score - game.human_score} 分"
            turn = "玩家" if game.turn == "human" else "Bot"
            lines.append(
                f"实时状态：玩家已存 {game.human_score} 分，Bot 已存 {game.bot_score} 分，"
                f"{advantage}；当前轮到{turn}，本回合暂存 {game.turn_total} 分，"
                f"最近点数={game.last_roll or '无'}，目标 {game.target_score} 分。"
            )
            return lines

        if isinstance(game, DrawGuessGame):
            state = (
                "已经猜中"
                if game.solved
                else "本轮已经结束"
                if game.finished
                else "正在看图"
                if game.processing
                else "等待玩家继续作画"
            )
            recent = "、".join(item["guess"] for item in game.guesses[-3:]) or "暂无"
            lines.append(
                f"实时进度：{state}，Bot 已猜 {len(game.guesses)}/{game.max_guesses} 次，"
                f"最近猜测：{recent}。这是合作玩法，不按双方对抗优劣描述。"
            )
            return lines

        if isinstance(game, TicTacToeGame):
            marks = {0: ".", TICTACTOE_X: "X", TICTACTOE_NOUGHT: "O"}
            board = "/".join("".join(marks[cell] for cell in row) for row in game.board)
            bot_mark = "X" if game.bot_mark == TICTACTOE_X else "O"
            human_mark = "X" if game.human_mark == TICTACTOE_X else "O"
            turn = "X" if game.turn == TICTACTOE_X else "O"
            lines.append(
                f"实时棋盘={board}；玩家执 {human_mark}，Bot 执 {bot_mark}，当前轮到 {turn}。"
            )
            return lines

        if isinstance(game, GomokuGame):
            human_stones = sum(
                cell == game.human_color for row in game.board for cell in row
            )
            bot_stones = sum(
                cell == game.bot_color for row in game.board for cell in row
            )
            turn = "玩家" if game.turn == game.human_color else "Bot"
            facts = [
                f"实时局面：玩家棋子 {human_stones}，Bot 棋子 {bot_stones}，当前轮到{turn}"
            ]
            human_tactical = game.tactical_state(game.human_color)
            bot_tactical = game.tactical_state(game.bot_color)
            tactical_labels = {
                "four": "存在四子威胁",
                "three": "存在三子潜力",
                "win": "已经获胜",
            }
            if human_tactical:
                facts.append(
                    f"玩家{tactical_labels.get(human_tactical, human_tactical)}"
                )
            if bot_tactical:
                facts.append(f"Bot {tactical_labels.get(bot_tactical, bot_tactical)}")
            lines.append(
                "；".join(facts) + "。局势只按已知威胁描述，不要仅凭棋子数判断优劣。"
            )
            return lines

        if isinstance(game, XiangqiGame):
            values = {"a": 2, "b": 2, "n": 4, "r": 9, "c": 4, "p": 1, "k": 0}
            red_material = sum(
                values.get(piece.lower(), 0)
                for row in game.board()
                for piece in row
                if piece != "." and piece.isupper()
            )
            black_material = sum(
                values.get(piece.lower(), 0)
                for row in game.board()
                for piece in row
                if piece != "." and piece.islower()
            )
            human_material = (
                red_material if game.human_side == XIANGQI_RED else black_material
            )
            bot_material = (
                black_material if game.bot_side == XIANGQI_BLACK else red_material
            )
            difference = bot_material - human_material
            material = (
                "材料大致相当"
                if abs(difference) <= 1
                else f"Bot 材料领先 {difference}"
                if difference > 0
                else f"玩家材料领先 {-difference}"
            )
            turn = "玩家" if game.turn == game.human_side else "Bot"
            lines.append(
                f"实时局面：当前轮到{turn}，已走 {len(game.moves)} 手，{material}。"
                "材料只是局部参考，不等同于引擎胜率。"
            )
            return lines

        if isinstance(game, TurtleSoupGame):
            if game.mode == "player_host":
                snapshot = room.public_snapshot()
                current_number = snapshot.get("current_player_number")
                current_name = snapshot.get("current_player_name")
                current_label = (
                    f"{current_name}（{current_number}号）"
                    if current_name and current_number
                    else f"{current_number}号" if current_number else "未知"
                )
                recent = [
                    f"玩家线索/回答：{entry.prompt}；Bot {('猜测' if entry.bot_action == 'guess' else '提问')}：{entry.response}"
                    for entry in game.entries[-2:]
                    if entry.kind == "reverse"
                ]
                lines.append(
                    f"实时进度：玩家出题、Bot 猜，公开回合 {game.turn_count} 次，"
                    f"Bot 提问 {game.question_count} 次、猜测 {game.answer_attempts} 次，"
                    f"当前轮到 {current_label} 玩家。Bot 不知道未公开汤底。"
                )
                lines.extend(recent)
                return lines
            puzzle = game.puzzle
            title = puzzle.title if puzzle else "出题中"
            snapshot = room.public_snapshot()
            current_number = snapshot.get("current_player_number")
            current_name = snapshot.get("current_player_name")
            current_label = (
                f"{current_name}（{current_number}号）"
                if current_name and current_number
                else f"{current_number}号" if current_number else "未知"
            )
            lines.append(
                f"实时进度：题目《{title}》，提问 {game.question_count} 次，"
                f"提示 {game.hints_used} 次，发现公开关键进度 {len(game.discovered_facts)}/"
                f"{len(puzzle.key_facts) if puzzle else 0}，当前轮到 {current_label}。"
                "不得推测或泄露隐藏汤底。"
            )
        return lines

    async def _create_room_from_event(
        self,
        event: AstrMessageEvent,
        difficulty: Difficulty,
        game_type: GameType,
        turtle_soup_mode: TurtleSoupMode = "bot_host",
    ) -> GameRoom:
        if not self.server_enabled:
            raise RuntimeError("游戏房间服务已在插件配置中关闭")
        group_id = str(event.get_group_id() or "").strip()
        source = "group" if group_id else "private"
        creator_qq = str(event.get_sender_id() or "").strip()
        if source == "group":
            if not self.group_rooms_enabled:
                raise PermissionError("群聊创建游戏房间已关闭")
            if (
                not self.allow_non_admin_group_creation
                and creator_qq not in self.game_admin_ids
            ):
                raise PermissionError("当前只允许插件配置中的游戏管理员创建群聊房间")
        elif not self.private_rooms_enabled:
            raise PermissionError("私聊创建游戏房间已关闭")
        await self._ensure_public_access()
        room = await self.manager.create_room(
            source=source,
            session_id=event.unified_msg_origin,
            platform=str(event.get_platform_id() or ""),
            group_id=group_id,
            creator_qq=creator_qq,
            creator_name=str(event.get_sender_name() or "").strip(),
            admin_room=source == "group" and creator_qq in self.game_admin_ids,
            game_type=game_type,
            difficulty=difficulty,
            turtle_soup_mode=turtle_soup_mode,
        )
        return room

    async def _create_or_reuse_room_from_event(
        self,
        event: AstrMessageEvent,
        difficulty: Difficulty,
        game_type: GameType,
        *,
        turtle_soup_mode: TurtleSoupMode = "bot_host",
        confirm_abandon: bool,
    ) -> tuple[GameRoom, bool, bool]:
        rooms = self.manager.for_session(event.unified_msg_origin)
        if len(rooms) > 1:
            raise ValueError("当前会话已有多个活动房间，请先说明要使用的房间编号")
        if not rooms:
            if game_type == "xiangqi":
                await self.xiangqi_engine.ensure_ready()
            room = await self._create_room_from_event(
                event, difficulty, game_type, turtle_soup_mode
            )
            return room, False, False
        room = rooms[0]
        _ = difficulty, game_type, turtle_soup_mode, confirm_abandon
        await self._ensure_public_access()
        return room, True, False

    async def _ensure_public_access(self) -> None:
        if not self.room_server.running:
            await self.room_server.start()
            if self.room_server.port != self.room_server.requested_port:
                logger.warning(
                    "[GameCompanion] 端口 %s 被占用，房间服务改用 %s",
                    self.room_server.requested_port,
                    self.room_server.port,
                )
        if self.public_base_url:
            return
        if not self.auto_quick_tunnel:
            await self.room_server.stop()
            raise RuntimeError("未配置外部 HTTPS 地址，并且临时公网访问已关闭")
        self.quick_tunnel.local_url = self.room_server.local_base_url
        try:
            await self.quick_tunnel.start(timeout=40)
        except Exception:
            if not self.manager.rooms:
                await self.room_server.stop()
            raise

    def _room_url(self, room: GameRoom) -> str:
        base = self.public_base_url or (
            self.quick_tunnel.url if self.quick_tunnel.running else ""
        )
        if not base:
            raise RuntimeError("外部访问地址尚未就绪")
        return f"{base.rstrip('/')}/room/{quote(room.access_token, safe='')}"

    def _resolve_event_room(self, event: AstrMessageEvent, room_id: str) -> GameRoom:
        actor = str(event.get_sender_id() or "")
        if room_id:
            room = self.manager.rooms.get(room_id)
            if room is None:
                raise ValueError("找不到指定房间")
            if (
                room.session_id != event.unified_msg_origin
                and actor not in self.game_admin_ids
            ):
                raise PermissionError("该房间不属于当前 QQ 会话")
            return room
        rooms = self.manager.for_session(event.unified_msg_origin)
        if len(rooms) != 1:
            raise ValueError("当前会话没有唯一活动房间，请说明房间编号")
        return rooms[0]

    async def _on_room_event(
        self, event_name: str, room: GameRoom, payload: dict[str, Any]
    ) -> None:
        game_label = self._game_label(room.game_type)
        if event_name == "soup_generation_requested":
            if isinstance(room.game, TurtleSoupGame):
                self._spawn(self._prepare_turtle_soup(room, room.game))
            return
        if event_name == "game_started":
            self._capture_round_participants(room, reset=True)
            if room.player_identity_confirmed:
                self._notify_companion_activity(room, "updated")
            opening = (
                (
                    "新的一局海龟汤已经出题完成，请用当前人格简短邀请玩家开始提问。"
                    if room.turtle_soup_mode == "bot_host"
                    else "玩家出题、Bot 猜的海龟汤已经开始，请用当前人格简短邀请当前玩家给出第一条线索。"
                )
                if room.game_type == "turtle_soup"
                else f"新的一局{game_label}刚刚开始，请用当前人格简短说一句开场话。"
            )
            self._spawn(
                self._comment(
                    room,
                    opening,
                )
            )
            return
        if event_name == "player_confirmed":
            self._capture_round_participants(room)
            self._notify_companion_activity(room, "started")
            return
        if event_name == "seats_changed":
            self._capture_round_participants(room)
            return
        if event_name == "board_changed" and room.game is not None:
            if room.game_type == "gomoku":
                side = (
                    room.game.human_color
                    if payload.get("actor") == "human"
                    else room.game.bot_color
                )
            elif room.game_type == "tictactoe":
                side = (
                    room.game.human_mark
                    if payload.get("actor") == "human"
                    else room.game.bot_mark
                )
            else:
                side = None
            tactical = room.game.tactical_state(side)
            tactical_prompt = {
                "four": "棋盘刚出现明显的四子威胁",
                "major_capture": "棋盘上刚发生了一次重要吃子",
                "fork": "井字棋盘面刚出现了双重威胁",
            }.get(tactical)
            if tactical_prompt and (
                time.time() - room.last_commentary_at >= self.commentary_cooldown
            ):
                room.last_commentary_at = time.time()
                self._spawn(
                    self._comment(
                        room,
                        f"{tactical_prompt}，请结合当前人格简短自然地回应。",
                    )
                )
            return
        if event_name == "soup_question_answered":
            if int(payload.get("new_facts") or 0) > 0:
                self._spawn(
                    self._comment(
                        room,
                        "玩家刚通过提问触及了海龟汤的关键事实。请用当前人格简短回应，"
                        "不要透露汤底或任何尚未公开的线索。",
                    )
                )
            return
        if event_name == "soup_answer_attempted":
            if int(payload.get("new_facts") or 0) > 0:
                self._spawn(
                    self._comment(
                        room,
                        "玩家提交的海龟汤推理已经接近答案但仍不完整。请简短鼓励，"
                        "不要指出缺少的事实。",
                    )
                )
            return
        if event_name == "soup_hint_revealed":
            if payload.get("source") == "web":
                hint = str(payload.get("hint") or "")
                visitor = room.visitors.get(str(payload.get("visitor_token") or ""))
                self._spawn(
                    self._announce_turtle_soup_hint(room, hint, visitor=visitor)
                )
            return
        if event_name == "dice_changed":
            action = str(payload.get("action") or "")
            actor = "玩家" if payload.get("actor") == "human" else "Bot"
            lost = int(payload.get("lost") or 0)
            banked = int(payload.get("banked") or 0)
            rolls = int(payload.get("turn_rolls") or 0)
            key_event = ""
            if action == "bust" and lost >= 10:
                key_event = f"{actor}掷出 1，本回合损失了 {lost} 分"
            elif action == "roll" and rolls == 4:
                key_event = f"{actor}已经连续成功掷了四次，仍在冒险"
            elif action in {"hold", "win"} and banked >= 15:
                key_event = f"{actor}一次存下了 {banked} 分"
            if key_event and (
                time.time() - room.last_commentary_at >= self.commentary_cooldown
            ):
                room.last_commentary_at = time.time()
                self._spawn(
                    self._comment(
                        room,
                        f"贪心骰子刚发生关键节点：{key_event}。请结合当前人格简短自然地回应。",
                    )
                )
            return
        if event_name in {"drawing_changed", "draw_guess_completed"}:
            return
        if event_name == "game_finished":
            self._queue_companion_round_event(room, payload)
            if room.game_type == "turtle_soup" and isinstance(
                room.game, TurtleSoupGame
            ):
                result = (
                    "Bot 成功猜中玩家的汤底"
                    if room.game.mode == "player_host" and room.game.bot_solved
                    else "玩家结束了出题"
                    if room.game.mode == "player_host"
                    else "玩家成功解开汤底"
                    if room.game.solved
                    else "玩家放弃，汤底已揭晓"
                )
            elif room.game_type == "draw_guess" and isinstance(
                room.game, DrawGuessGame
            ):
                result = (
                    f"Bot 在第 {len(room.game.guesses)} 次猜中了“{room.game.answer}”"
                    if room.game.solved
                    else f"这一轮没能猜中，答案是“{room.game.answer}”"
                )
            else:
                result = {
                    "human_win": "玩家获胜",
                    "bot_win": "Bot 获胜",
                    "draw": "平局",
                }.get(str(payload.get("result")), "对局结束")
            self._spawn(
                self._comment(
                    room,
                    f"{game_label}本局结果是：{result}。请用当前人格简短回应。",
                )
            )
            return
        if event_name == "rematch_requested":
            visitor = room.visitors.get(str(payload.get("visitor_token") or ""))
            pending = self._companion_round_event_tasks.get(room.room_id)
            if pending is not None and not pending.done():
                try:
                    await asyncio.wait_for(asyncio.shield(pending), timeout=8)
                except TimeoutError:
                    pass
            await self._report_companion_game_event(
                room,
                "rematch_requested",
                payload,
                visitors=[visitor] if visitor is not None else [],
            )
            self._spawn(self._decide_rematch(room, visitor=visitor))
            return
        if event_name == "game_switched":
            self._notify_companion_activity(room, "updated")
            return
        if event_name == "room_destroyed":
            self._notify_companion_activity(room, "ended")
            await self._record_room_memory(room)

    async def submit_room_chat(
        self, room: GameRoom, text: str, *, visitor_token: str
    ) -> dict[str, Any]:
        """Handle one isolated WebUI conversation turn without sending to QQ."""
        async with room.chat_lock:
            visitor, cleaned, is_player, is_current_player = (
                await self.manager.begin_room_chat(room, visitor_token, text)
            )
            action, options = self._room_chat_action(room, cleaned)
            if action in {"soup_question", "soup_answer", "soup_respond"}:
                action = await self._refine_turtle_chat_action(
                    room, cleaned, proposed_action=action
                )
            if action and not is_player:
                reply = "你现在在观众席，不能执行游戏指令；可以继续在这里和我聊天。"
                await self.manager.add_room_chat_reply(
                    room, visitor, reply, message_type="permission"
                )
                return {"action": "denied", "reply": reply}

            try:
                if action == "close":
                    reply = "好，这个房间就到这里。"
                    await self.manager.add_room_chat_reply(
                        room, visitor, reply, message_type="control"
                    )
                    await self.manager.destroy(room.room_id, "玩家通过 WebUI 结束了房间")
                    return {"action": action, "reply": reply}
                if action == "switch_game":
                    target = options["game_type"]
                    switched = await self.manager.switch_game(room, target, force=True)
                    soup_mode = options.get("turtle_soup_mode")
                    if target == "turtle_soup" and soup_mode:
                        await self.manager.switch_turtle_soup_mode(
                            room, soup_mode, force=True
                        )
                    reply = (
                        f"已经切换到{self._game_label(target)}，房间和玩家席都保留着。"
                        if switched
                        else f"现在玩的已经是{self._game_label(target)}。"
                    )
                elif action == "switch_soup_mode":
                    mode = options["turtle_soup_mode"]
                    switched = await self.manager.switch_turtle_soup_mode(
                        room, mode, force=True
                    )
                    label = "我出题、玩家猜" if mode == "bot_host" else "玩家出题、我来猜"
                    reply = f"海龟汤已切换为{label}。" if switched else f"现在已经是{label}。"
                elif action == "rematch":
                    await self.manager.request_rematch(
                        room,
                        visitor.token,
                        record_message=False,
                        request_text=cleaned,
                    )
                    return {"action": action, "reply": ""}
                elif action == "undo":
                    accepted, reply = await self._decide_ui_undo(room, visitor)
                    if accepted:
                        await self.manager.undo(room)
                elif action == "pause":
                    await self.manager.pause(room)
                    reply = "先暂停一下，我会保留当前进度。"
                elif action == "resume":
                    await self.manager.resume(room)
                    reply = "继续吧，当前进度没有变化。"
                elif action == "resign":
                    await self.manager.resign(room)
                    reply = (
                        "好，这一题就先揭晓到这里。"
                        if room.game_type == "turtle_soup"
                        else "收到，本局按你认输结束。"
                    )
                elif action == "soup_hint":
                    await self.manager.request_turtle_soup_hint(
                        room, source="web", visitor_token=visitor.token
                    )
                    return {"action": action, "reply": ""}
                elif action == "soup_correct":
                    await self.manager.confirm_reverse_turtle_soup_guess(
                        room, source="web", visitor_token=visitor.token
                    )
                    reply = "明白，这次猜测确认正确。"
                elif action == "soup_answer":
                    result = await self.submit_turtle_soup_answer(
                        room, cleaned, source="web", visitor_token=visitor.token
                    )
                    reply = str(result.get("reply") or "我已经看过这份推理。")
                elif action == "soup_question":
                    result = await self.submit_turtle_soup_question(
                        room, cleaned, source="web", visitor_token=visitor.token
                    )
                    reply = str(result.get("reply") or "无关")
                elif action == "soup_respond":
                    result = await self.submit_reverse_turtle_soup_turn(
                        room, cleaned, source="web", visitor_token=visitor.token
                    )
                    reply = str(result.get("reply") or "")
                    room.record_chat_memory(visitor, "bot", reply)
                    return {"action": action, "reply": reply}
                else:
                    reply = await self._generate_room_chat_reply(
                        room,
                        visitor,
                        cleaned,
                        is_player=is_player,
                        is_current_player=is_current_player,
                    )
            except (ValueError, RuntimeError, PermissionError, OSError) as exc:
                reply = str(exc)
                message_type = "permission" if isinstance(exc, PermissionError) else "error"
                await self.manager.add_room_chat_reply(
                    room, visitor, reply, message_type=message_type
                )
                return {"action": action or "chat", "reply": reply}

            if not reply:
                reply = "我在，继续说吧。"
            await self.manager.add_room_chat_reply(
                room,
                visitor,
                reply,
                message_type="control" if action else "chat",
            )
            return {"action": action or "chat", "reply": reply}

    async def submit_draw_guess(
        self, room: GameRoom, *, visitor_token: str, image_data_url: str
    ) -> dict[str, Any]:
        """Send one bounded canvas image to a visual provider for a single guess."""
        safe_image = self._validated_drawing_image(image_data_url)
        game = await self.manager.begin_draw_guess(room, visitor_token)
        try:
            guess = await self._guess_drawing(room, game, safe_image)
            item = await self.manager.complete_draw_guess(room, visitor_token, guess)
        except Exception:
            await self.manager.abort_draw_guess(room)
            raise
        return {
            "guess": item["guess"],
            "correct": item["correct"],
            "number": item["number"],
        }

    async def _guess_drawing(
        self, room: GameRoom, game: DrawGuessGame, image_data_url: str
    ) -> str:
        provider = None
        if self.draw_guess_vision_provider_id:
            getter = getattr(self.context, "get_provider_by_id", None)
            if callable(getter):
                provider = getter(self.draw_guess_vision_provider_id)
            if provider is None:
                raise RuntimeError("你画我猜配置的视觉模型 Provider 不存在")
        else:
            provider = self.context.get_using_provider(room.session_id)
        if provider is None or not callable(getattr(provider, "text_chat", None)):
            raise RuntimeError("当前会话没有可用的视觉模型")
        previous = "、".join(item["guess"] for item in game.guesses) or "暂无"
        prompt = (
            "请观察这张用户在白色画布上的简笔画，猜一个最可能的中文词语。"
            "只回答一个答案，不解释，不列举候选，不复述任务。"
            f"此前已经猜过且不正确的答案：{previous}。不要重复这些答案。"
        )
        system_prompt = (
            "你正在玩你画我猜。隐藏答案绝不会提供给你，必须只根据图片判断。"
            "输出一个简短中文名词或成语；不要使用斜杠、顿号或逗号列出多个答案。"
        )
        try:
            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    system_prompt=system_prompt,
                    image_urls=[image_data_url],
                ),
                timeout=45,
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError("视觉模型看图超时，请稍后再试") from exc
        except Exception as exc:
            raise RuntimeError(
                "视觉模型无法读取画布；请检查当前模型是否支持图片，或配置专用视觉 Provider"
            ) from exc
        raw = str(getattr(response, "completion_text", response) or "").strip()
        guess = self._clean_draw_guess(raw)
        if not guess:
            raise RuntimeError("视觉模型没有给出有效猜测")
        return guess

    @staticmethod
    def _clean_draw_guess(value: Any) -> str:
        text = str(value or "").strip().splitlines()[0] if str(value or "").strip() else ""
        text = re.sub(r"^(?:我猜(?:是)?|答案(?:是)?|可能是)[:：\s]*", "", text)
        text = re.split(r"[，,、/；;]", text, maxsplit=1)[0]
        return text.strip(" \t\r\n。！？!?\"'“”‘’《》")[:30]

    @staticmethod
    def _validated_drawing_image(value: Any) -> str:
        image = str(value or "").strip()
        match = re.fullmatch(
            r"data:image/(png|webp);base64,([A-Za-z0-9+/]+={0,2})", image
        )
        if not match:
            raise ValueError("画布图片必须是 PNG 或 WebP")
        try:
            content = base64.b64decode(match.group(2), validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("画布图片编码无效") from None
        if not 256 <= len(content) <= 384 * 1024:
            raise ValueError("画布图片大小必须在 256 B 到 384 KB 之间")
        if match.group(1) == "png" and not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("PNG 画布图片签名无效")
        if match.group(1) == "webp" and not (
            content.startswith(b"RIFF") and content[8:12] == b"WEBP"
        ):
            raise ValueError("WebP 画布图片签名无效")
        return image

    @staticmethod
    def _room_chat_action(
        room: GameRoom, text: str
    ) -> tuple[str, dict[str, Any]]:
        """Recognize authoritative game intents; ordinary conversation stays chat."""
        normalized = re.sub(r"[\s，。！!？?、]", "", str(text or "").lower())
        if any(phrase in normalized for phrase in ("关闭房间", "结束房间", "销毁房间")):
            return "close", {}

        aliases: tuple[tuple[GameType, tuple[str, ...]], ...] = (
            ("turtle_soup", ("海龟汤",)),
            ("tictactoe", ("井字棋", "圈叉棋")),
            ("xiangqi", ("中国象棋", "象棋")),
            ("gomoku", ("五子棋",)),
            ("pig_dice", ("贪心骰子", "小猪骰子", "骰子")),
            ("draw_guess", ("你画我猜", "画画猜词", "画图猜词")),
        )
        switch_words = (
            "切换",
            "换成",
            "换个游戏",
            "换游戏",
            "改成",
            "改玩",
            "想玩",
            "玩一局",
            "来一局",
            "来一盘",
        )
        for game_type, names in aliases:
            mentions_game = any(name in normalized for name in names)
            starts_game_request = normalized.startswith(
                ("玩", "来玩", "我们玩", "下", "来下", "开一局", "来一盘")
            )
            if mentions_game and (
                any(word in normalized for word in switch_words) or starts_game_request
            ):
                if game_type == room.game_type and any(
                    word in normalized for word in ("再来一局", "再玩一局", "下一局")
                ):
                    return "rematch", {}
                options: dict[str, Any] = {"game_type": game_type}
                if game_type == "turtle_soup":
                    if any(word in normalized for word in ("我出题", "你来猜", "bot猜")):
                        options["turtle_soup_mode"] = "player_host"
                    elif any(word in normalized for word in ("你出题", "我来猜", "bot出题")):
                        options["turtle_soup_mode"] = "bot_host"
                return "switch_game", options

        if room.game_type == "turtle_soup" and any(
            word in normalized for word in ("切换玩法", "换玩法", "我出题", "你出题")
        ):
            mode: TurtleSoupMode = (
                "player_host"
                if any(word in normalized for word in ("我出题", "你来猜", "bot猜"))
                else "bot_host"
            )
            return "switch_soup_mode", {"turtle_soup_mode": mode}
        if any(word in normalized for word in ("再来一局", "再来一题", "再玩一局", "重新开一局", "下一局")):
            return "rematch", {}
        if any(word in normalized for word in ("悔棋", "撤回上一步", "撤销上一步")):
            return "undo", {}
        if normalized in {"暂停", "先暂停", "暂停一下", "暂停游戏"}:
            return "pause", {}
        if normalized in {"继续", "继续游戏", "恢复游戏", "接着玩"}:
            return "resume", {}
        if any(word in normalized for word in ("投降", "认输", "揭晓答案", "公布答案", "看汤底", "放弃本局", "放弃这题")):
            return "resign", {}

        if room.game_type != "turtle_soup" or not isinstance(room.game, TurtleSoupGame):
            return "", {}
        if any(word in normalized for word in ("给个提示", "来个提示", "申请提示", "提示一下")):
            return "soup_hint", {}
        if room.game.mode == "player_host":
            if any(word in normalized for word in ("你猜对了", "bot猜对了", "猜中了", "答案正确")):
                return "soup_correct", {}
            if room.status == "active":
                return "soup_respond", {}
            return "", {}
        if any(word in normalized for word in ("我猜答案", "完整答案", "完整推理", "真相是", "答案是")):
            return "soup_answer", {}
        if room.status == "active" and any(
            marker in str(text) for marker in ("?", "？", "吗", "是否", "是不是", "有没有", "为什么", "会不会", "能否")
        ):
            return "soup_question", {}
        return "", {}

    async def _refine_turtle_chat_action(
        self, room: GameRoom, text: str, *, proposed_action: str
    ) -> str:
        """Separate turtle-soup gameplay from casual room chat using public facts only."""
        game = room.game
        if not isinstance(game, TurtleSoupGame):
            return proposed_action
        mode = game.mode
        allowed = (
            {"chat", "soup_respond"}
            if mode == "player_host"
            else {"chat", "soup_question", "soup_answer"}
        )
        puzzle_surface = (
            game.puzzle.surface if mode == "bot_host" and game.puzzle is not None else ""
        )
        public_entries = []
        for entry in game.entries[-6:]:
            public_entries.append(
                {
                    "prompt": entry.prompt,
                    "response": entry.response,
                    "kind": entry.kind,
                }
            )
        system_prompt = (
            "你只负责判断一条 WebUI 消息是海龟汤游戏输入还是普通闲聊。"
            "不得回答消息，不得推测汤底，只输出一个允许的动作名称。"
        )
        choices = "、".join(sorted(allowed))
        prompt = (
            f"玩法={mode}；允许动作={choices}；汤面={puzzle_surface or '玩家出题，Bot 只看公开线索'}；"
            f"最近公开回合={json.dumps(public_entries, ensure_ascii=False)}；消息={text}\n"
            "与当前汤题、Bot 最近问题或公开线索无关的内容必须判为 chat。"
        )
        try:
            raw = await self._call_room_model(
                room, system_prompt=system_prompt, prompt=prompt, timeout=15
            )
        except RuntimeError:
            return proposed_action
        normalized = raw.strip().lower().strip("`'\" 。")
        return normalized if normalized in allowed else proposed_action

    async def _decide_ui_undo(
        self, room: GameRoom, visitor: Visitor
    ) -> tuple[bool, str]:
        raw = await self._generate_persona_text(
            room,
            "玩家在房间对话中请求悔棋。结合当前人格决定是否同意，只输出 JSON："
            '{"accept":true或false,"reply":"一句简短自然回复"}。',
        )
        accepted = True
        reply = "这次可以，退回上一轮。"
        for candidate in re.findall(r"\{.*?\}", raw or "", re.DOTALL):
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            accepted = bool(data.get("accept"))
            reply = str(data.get("reply") or reply).strip()[:300]
            break
        return accepted, reply

    async def _generate_room_chat_reply(
        self,
        room: GameRoom,
        visitor: Visitor,
        text: str,
        *,
        is_player: bool,
        is_current_player: bool,
    ) -> str:
        persona = await self._persona_prompt()
        memory = await self._memory_context_for_visitor(room, visitor, text)
        scene = self._companion_scene_for_visitor(room, visitor)
        identity = (
            "玩家"
            if is_player
            else "已绑定观众"
            if visitor.identity_confirmed
            else "匿名观众"
        )
        public_name = (
            visitor.display_name if visitor.identity_confirmed and visitor.display_name else "匿名观众"
        )
        recent_lines: list[str] = []
        for message in room.messages[-16:]:
            role = str(message.get("role") or "system")
            if role == "user":
                sender = str(message.get("sender_name") or "匿名观众")
                number = message.get("sender_number")
                label = f"{sender}（{number}号）" if number else sender
            elif role == "bot":
                label = "Bot"
            else:
                label = "系统"
            recent_lines.append(f"{label}：{str(message.get('content') or '')[:500]}")
        state = "\n".join(self._live_game_state(room))
        system_prompt = (
            f"{persona}\n\n{scene}\n\n{memory}\n\n"
            f"你正在游戏伴侣 WebUI 的房间中与用户聊天，当前游戏是{self._game_label(room.game_type)}。"
            "这里的聊天只属于当前房间，不得声称已向 QQ 发消息。保持原有人格、关系和自然语气。"
            "系统会在模型调用前执行有权限的游戏指令；你不能自行声称已经落子、切换游戏、投降、"
            "暂停、悔棋或改变房间状态。海龟汤中绝不能透露未公开的汤底或隐藏事实。"
        ).strip()
        prompt = (
            f"当前发言者：{public_name}（{visitor.number}号），身份={identity}，"
            f"是否当前回合玩家={'是' if is_current_player else '否'}。\n"
            f"当前公开游戏状态：\n{state}\n\n"
            "房间最近公开对话：\n"
            + ("\n".join(recent_lines) or "暂无")
            + f"\n\n请只回复当前这条消息：{text}"
        )
        try:
            return (
                await self._call_room_model(
                    room, system_prompt=system_prompt, prompt=prompt, timeout=35
                )
            )[:500]
        except RuntimeError:
            return "我现在暂时没法组织好回复，稍后再和我说一次。"

    async def _memory_context_for_visitor(
        self, room: GameRoom, visitor: Visitor, query: str
    ) -> str:
        if (
            not visitor.identity_confirmed
            or not visitor.qq
            or room.source != "private"
            or len(room.visitors) != 1
        ):
            return ""
        bridge = self._memory_bridge()
        composer = getattr(bridge, "compose_context", None) if bridge else None
        if not callable(composer):
            return ""
        try:
            return str(
                await composer(
                    query=query,
                    session_context={
                        "scope": room.source,
                        "session_id": room.session_id,
                        "platform": room.platform,
                        "user_id": visitor.qq,
                        "group_id": room.group_id,
                    },
                    top_k=4,
                    max_chars=1800,
                    retrieval_profile="companion",
                )
                or ""
            )
        except Exception as exc:
            logger.debug("[GameCompanion] 读取 WebUI 发言者记忆失败: %s", exc)
            return ""

    def _companion_scene_for_visitor(self, room: GameRoom, visitor: Visitor) -> str:
        if (
            not visitor.identity_confirmed
            or not visitor.qq
            or room.source != "private"
            or len(room.visitors) != 1
        ):
            return ""
        api = self._private_companion_api()
        getter = getattr(api, "get_realtime_context", None) if api else None
        if not callable(getter):
            return ""
        try:
            result = getter(visitor.qq, purpose="game")
            return str(result.get("prompt") or "") if isinstance(result, dict) else ""
        except Exception as exc:
            logger.debug("[GameCompanion] 读取 WebUI 发言者陪伴场景失败: %s", exc)
            return ""

    async def _comment(self, room: GameRoom, prompt: str) -> None:
        text = await self._generate_persona_text(room, prompt)
        if not text or room.status == "closed":
            return
        room.add_message("bot", text)

    async def _decide_rematch(
        self, room: GameRoom, *, visitor: Visitor | None = None
    ) -> None:
        raw = await self._generate_persona_text(
            room,
            "玩家在网页申请再来一局。请结合当前人格决定是否接受，只输出 JSON："
            '{"accept":true或false,"difficulty":"easy/normal/hard","reply":"一句自然回复"}。'
            "如果接受，可以根据人格和此前胜负重新选择本局棋力；贪心骰子中 difficulty "
            "分别代表稳健、均衡和大胆的风险倾向。",
        )
        accept = True
        reply = "那就再来一局。"
        difficulty: Difficulty = room.difficulty
        for candidate in re.findall(r"\{.*?\}", raw or "", re.DOTALL):
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            accept = bool(data.get("accept"))
            reply = str(data.get("reply") or reply).strip()[:300]
            difficulty = self._difficulty(data.get("difficulty") or room.difficulty)
            break
        if room.room_id not in self.manager.rooms:
            return
        applied = await self.manager.resolve_rematch(
            room,
            accepted=accept,
            message=reply,
            difficulty=difficulty,
        )
        if not applied:
            return
        if visitor is not None:
            room.record_chat_memory(visitor, "bot", reply)
        if (
            accept
            and room.game_type == "turtle_soup"
            and room.status == "setup"
            and room.player_token
        ):
            await self.manager.start_game(room, room.player_token, "")

    async def _prepare_turtle_soup(self, room: GameRoom, game: TurtleSoupGame) -> None:
        if game.mode != "bot_host":
            return
        recent = list(room.turtle_soup_recent_signatures)
        persona = await self._persona_prompt()
        last_error = ""
        for attempt in range(1, 4):
            try:
                system_prompt, prompt = generation_prompt(
                    difficulty=room.difficulty,
                    content_level=game.content_level,
                    recent_signatures=recent,
                )
                if persona:
                    system_prompt = (
                        f"{persona}\n\n{system_prompt}\n"
                        "人格只影响叙事气质，不得把真实用户、私人记忆或生活场景写进题目。"
                    )
                raw = await self._call_room_model(
                    room, system_prompt=system_prompt, prompt=prompt, timeout=35
                )
                data = extract_json_object(raw)
                if data is None:
                    raise ValueError("出题结果不是有效 JSON")
                puzzle = puzzle_from_mapping(data, content_level=game.content_level)
                if puzzle.signature in set(recent):
                    raise ValueError("题目与本房间最近的主题重复")
                check_system, check_prompt = validation_prompt(puzzle)
                check = await self._call_room_model(
                    room,
                    system_prompt=check_system,
                    prompt=check_prompt,
                    timeout=30,
                )
                if not validation_passed(check):
                    raise ValueError("题目未通过独立自洽性校验")
                if await self.manager.complete_turtle_soup_generation(
                    room, game, puzzle
                ):
                    return
                return
            except (RuntimeError, ValueError, OSError) as exc:
                last_error = str(exc)
                logger.info(
                    "[GameCompanion] 海龟汤第 %s 次出题未采用: %s",
                    attempt,
                    exc,
                )
        puzzle = fallback_puzzle(
            content_level=game.content_level,
            excluded_signatures=set(recent),
        )
        applied = await self.manager.complete_turtle_soup_generation(room, game, puzzle)
        if applied:
            logger.warning(
                "[GameCompanion] Bot 出题连续失败，当前局使用内置兜底题: %s",
                last_error or "模型不可用",
            )

    async def submit_turtle_soup_question(
        self,
        room: GameRoom,
        text: str,
        *,
        source: Literal["web", "qq"],
        visitor_token: str = "",
        actor_qq: str = "",
    ) -> dict[str, Any]:
        game, question = await self.manager.begin_turtle_soup_interaction(
            room,
            text,
            source=source,
            visitor_token=visitor_token,
            actor_qq=actor_qq,
            limit=200,
        )
        try:
            if game.puzzle is None:
                raise RuntimeError("题目尚未准备完成")
            system_prompt, prompt = question_judge_prompt(
                game.puzzle,
                question=question,
                public_history=public_judge_history(game.entries),
            )
            raw = await self._call_room_model(
                room, system_prompt=system_prompt, prompt=prompt, timeout=30
            )
            verdict, matched_facts = parse_question_judgment(
                raw, fact_count=len(game.puzzle.key_facts)
            )
            if verdict == "compound":
                matched_facts.clear()
            applied = await self.manager.resolve_turtle_soup_question(
                room,
                game,
                question,
                verdict,
                source=source,
                matched_facts=matched_facts,
            )
            if not applied:
                raise RuntimeError("房间状态已经变化，请重新查看当前题目")
            return {
                "verdict": verdict,
                "reply": VERDICT_LABELS[verdict],
                "question_count": game.question_count,
            }
        except Exception as exc:
            await self.manager.cancel_turtle_soup_interaction(room, game, str(exc))
            if isinstance(exc, (ValueError, RuntimeError, PermissionError, OSError)):
                raise
            raise RuntimeError("Bot 暂时无法判断这个问题，请稍后重试") from exc

    async def submit_turtle_soup_answer(
        self,
        room: GameRoom,
        text: str,
        *,
        source: Literal["web", "qq"],
        visitor_token: str = "",
        actor_qq: str = "",
    ) -> dict[str, Any]:
        game, answer = await self.manager.begin_turtle_soup_interaction(
            room,
            text,
            source=source,
            visitor_token=visitor_token,
            actor_qq=actor_qq,
            limit=800,
        )
        try:
            if game.puzzle is None:
                raise RuntimeError("题目尚未准备完成")
            system_prompt, prompt = answer_judge_prompt(
                game.puzzle,
                answer=answer,
                discovered_facts=game.discovered_facts,
            )
            raw = await self._call_room_model(
                room, system_prompt=system_prompt, prompt=prompt, timeout=30
            )
            solved, coverage, matched_facts = parse_answer_judgment(
                raw, fact_count=len(game.puzzle.key_facts)
            )
            applied = await self.manager.resolve_turtle_soup_answer(
                room,
                game,
                answer,
                solved=solved,
                source=source,
                matched_facts=matched_facts,
            )
            if not applied:
                raise RuntimeError("房间状态已经变化，请重新查看当前题目")
            result: dict[str, Any] = {
                "solved": solved,
                "coverage": round(coverage, 2),
                "reply": (
                    "推理正确，汤底已经揭晓。"
                    if solved
                    else "已经接近了，但还缺少关键环节。"
                ),
            }
            if solved:
                result["solution"] = game.puzzle.solution
            return result
        except Exception as exc:
            await self.manager.cancel_turtle_soup_interaction(room, game, str(exc))
            if isinstance(exc, (ValueError, RuntimeError, PermissionError, OSError)):
                raise
            raise RuntimeError("Bot 暂时无法判断这份推理，请稍后重试") from exc

    async def submit_reverse_turtle_soup_turn(
        self,
        room: GameRoom,
        text: str,
        *,
        source: Literal["web", "qq"],
        visitor_token: str = "",
        actor_qq: str = "",
    ) -> dict[str, Any]:
        game, player_text = await self.manager.begin_turtle_soup_interaction(
            room,
            text,
            source=source,
            visitor_token=visitor_token,
            actor_qq=actor_qq,
            limit=800,
        )
        try:
            if game.mode != "player_host":
                raise ValueError("当前不是玩家出题、Bot 猜的玩法")
            system_prompt, prompt = reverse_turn_prompt(
                player_text=player_text,
                public_history=reverse_public_history(game.entries),
                persona=await self._persona_prompt(),
            )
            raw = await self._call_room_model(
                room, system_prompt=system_prompt, prompt=prompt, timeout=30
            )
            bot_action, bot_text = parse_reverse_turn(raw)
            applied = await self.manager.resolve_reverse_turtle_soup_turn(
                room,
                game,
                player_text,
                bot_action=bot_action,
                bot_text=bot_text,
                source=source,
            )
            if not applied:
                raise RuntimeError("房间状态已经变化，请重新查看当前回合")
            return {
                "bot_action": bot_action,
                "reply": bot_text,
                "turn_count": game.turn_count,
            }
        except Exception as exc:
            await self.manager.cancel_turtle_soup_interaction(room, game, str(exc))
            if isinstance(exc, (ValueError, RuntimeError, PermissionError, OSError)):
                raise
            raise RuntimeError("Bot 暂时无法继续推理，请稍后重试") from exc

    async def _call_room_model(
        self,
        room: GameRoom,
        *,
        system_prompt: str,
        prompt: str,
        timeout: int,
    ) -> str:
        provider = self.context.get_using_provider(room.session_id)
        if provider is None or not callable(getattr(provider, "text_chat", None)):
            raise RuntimeError("当前会话没有可用的大语言模型")
        try:
            response = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, system_prompt=system_prompt),
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError("模型响应超时") from exc
        except Exception as exc:
            raise RuntimeError("模型调用失败") from exc
        text = str(getattr(response, "completion_text", "") or "").strip()
        if not text:
            raise RuntimeError("模型没有返回有效内容")
        return text

    async def _announce_turtle_soup_hint(
        self, room: GameRoom, hint: str, *, visitor: Visitor | None = None
    ) -> None:
        if not hint or room.status == "closed":
            return
        intro = await self._generate_persona_text(
            room,
            "玩家刚申请了一次海龟汤提示。请用当前人格说一句很短的引子，"
            "不要猜测或补充任何线索。",
        )
        text = f"{intro}\n提示：{hint}" if intro else f"提示：{hint}"
        room.add_message("bot", text)
        if visitor is not None:
            room.record_chat_memory(visitor, "bot", text)

    async def _generate_persona_text(self, room: GameRoom, prompt: str) -> str:
        provider = self.context.get_using_provider(room.session_id)
        if provider is None or not callable(getattr(provider, "text_chat", None)):
            return ""
        persona = await self._persona_prompt()
        memory = await self._memory_context(room, prompt)
        companion_scene = self._companion_scene_prompt(room)
        system_prompt = (
            f"{persona}\n\n{companion_scene}\n\n{memory}\n\n"
            f"你正在与用户通过游戏伴侣 WebUI 玩{self._game_label(room.game_type)}。保持原有人格和关系语气，"
            "只回应当前游戏事件，不输出规则说明或格式标签。海龟汤中绝不能猜测或泄露尚未公开的汤底。"
        ).strip()
        try:
            response = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, system_prompt=system_prompt),
                timeout=30,
            )
        except Exception as exc:
            logger.debug("[GameCompanion] 生成人格化游戏回复失败: %s", exc)
            return ""
        return str(getattr(response, "completion_text", "") or "").strip()[:500]

    async def _persona_prompt(self) -> str:
        manager = getattr(self.context, "persona_manager", None)
        getter = getattr(manager, "get_default_persona_v3", None) if manager else None
        if not callable(getter):
            return ""
        try:
            persona = getter()
            if inspect.isawaitable(persona):
                persona = await asyncio.wait_for(persona, timeout=3)
            if isinstance(persona, dict):
                return str(persona.get("prompt") or persona.get("system_prompt") or "")
            return str(
                getattr(persona, "prompt", "") or getattr(persona, "system_prompt", "")
            )
        except Exception:
            return ""

    async def _memory_context(self, room: GameRoom, query: str) -> str:
        if (
            not room.player_identity_confirmed
            or room.source != "private"
            or len(room.visitors) != 1
            or (room.multiplayer.enabled and len(room.multiplayer.seats) > 1)
        ):
            return ""
        bridge = self._memory_bridge()
        composer = getattr(bridge, "compose_context", None) if bridge else None
        if not callable(composer):
            return ""
        try:
            return str(
                await composer(
                    query=query,
                    session_context={
                        "scope": room.source,
                        "session_id": room.session_id,
                        "platform": room.platform,
                        "user_id": room.player_qq or room.creator_qq,
                        "group_id": room.group_id,
                    },
                    top_k=4,
                    max_chars=1800,
                    retrieval_profile="companion",
                )
                or ""
            )
        except Exception as exc:
            logger.debug("[GameCompanion] 读取陪伴记忆上下文失败: %s", exc)
            return ""

    async def _record_room_memory(self, room: GameRoom) -> None:
        total_completed = sum(score.completed for score in room.scores.values())
        participants = self._memory_participant_qqs(room)
        has_bound_chat = any(room.chat_transcripts.values())
        if (
            not self.record_shared_experience
            or not participants
            or (total_completed < 1 and not has_bound_chat)
        ):
            return
        bridge = self._memory_bridge()
        recorder = getattr(bridge, "record_shared_experience", None) if bridge else None
        if not callable(recorder):
            return
        summaries = []
        for game_type, score in room.scores.items():
            if score.completed:
                if game_type == "turtle_soup":
                    summaries.append(
                        f"海龟汤 {score.completed} 题（玩家侧记分 {score.human_wins} 题，"
                        f"Bot 侧记分 {score.bot_wins} 题，共提问 {room.turtle_soup_stats.questions} 次，"
                        f"使用提示 {room.turtle_soup_stats.hints} 次）"
                    )
                elif game_type == "draw_guess":
                    summaries.append(
                        f"你画我猜 {score.completed} 轮（合作猜中 {score.human_wins} 轮，"
                        f"未猜中 {score.bot_wins} 轮）"
                    )
                else:
                    summaries.append(
                        f"{self._game_label(game_type)} {score.completed} 局（用户胜 {score.human_wins} 局，"
                        f"Bot 胜 {score.bot_wins} 局，平局 {score.draws} 局）"
                    )
        summary = (
            "Bot 与用户完成了游戏：" + "；".join(summaries) + "。"
            if summaries
            else "用户在游戏伴侣房间中与 Bot 和其他房间成员进行了交流。"
        )
        metadata = {
            "games": {
                game_type: {
                    "completed": score.completed,
                    "human_wins": score.human_wins,
                    "bot_wins": score.bot_wins,
                    "draws": score.draws,
                }
                for game_type, score in room.scores.items()
                if score.completed
            },
            "room_id": room.room_id,
            "difficulty": room.difficulty,
            "completed_games": total_completed,
            "turtle_soup": {
                "questions": room.turtle_soup_stats.questions,
                "hints": room.turtle_soup_stats.hints,
                "answer_attempts": room.turtle_soup_stats.answer_attempts,
            },
            "participant_count": len(participants),
        }
        for player_qq in participants:
            transcript = room.chat_transcripts.get(player_qq, [])
            chat_excerpt = self._chat_memory_excerpt(transcript)
            content = summary
            if chat_excerpt:
                content += " 与该用户有关的房间对话摘录：" + chat_excerpt
            try:
                await recorder(
                    content=content,
                    experience_type="game",
                    user_id=player_qq,
                    user_name=room.participant_names.get(
                        player_qq,
                        room.creator_name if player_qq == room.creator_qq else "",
                    ),
                    scope=room.source,
                    session_id=room.session_id,
                    platform=room.platform,
                    source_plugin=PLUGIN_NAME,
                    memory_id=f"game-companion-{room.room_id}-{player_qq}",
                    confidence=0.95,
                    importance=0.66,
                    metadata={**metadata, "chat_turns": len(transcript)},
                )
            except Exception as exc:
                logger.debug(
                    "[GameCompanion] 为玩家 %s 写入共同游戏经历失败: %s",
                    player_qq,
                    exc,
                )

    @staticmethod
    def _chat_memory_excerpt(transcript: list[dict[str, str]]) -> str:
        """Build a bounded per-user excerpt without mixing other visitors' speech."""
        parts: list[str] = []
        for entry in transcript[-12:]:
            content = " ".join(str(entry.get("content") or "").split())[:140]
            if not content:
                continue
            label = "用户" if entry.get("role") == "user" else "Bot"
            parts.append(f"{label}：{content}")
        return "；".join(parts)[:1600]

    @staticmethod
    def _memory_participant_qqs(room: GameRoom) -> list[str]:
        if room.multiplayer.enabled:
            return list(
                dict.fromkeys(
                    [
                        seat.qq
                        for seat in room.multiplayer.seats
                        if seat.identity_confirmed and seat.qq
                    ]
                    + sorted(room.confirmed_participant_qqs)
                )
            )
        return list(
            dict.fromkeys(
                (
                    [room.player_qq]
                    if room.player_identity_confirmed and room.player_qq
                    else []
                )
                + sorted(room.confirmed_participant_qqs)
            )
        )

    def _memory_bridge(self) -> Any | None:
        for name in (
            "data.plugins.astrbot_plugin_memory_companion.main",
            "astrbot_plugin_memory_companion.main",
        ):
            module = sys.modules.get(name)
            getter = (
                getattr(module, "get_memory_companion_bridge", None) if module else None
            )
            if callable(getter):
                bridge = getter()
                if bridge is not None:
                    return bridge
        return None

    def _private_companion_api(self) -> Any | None:
        for name in (
            "data.plugins.astrbot_plugin_private_companion.main",
            "astrbot_plugin_private_companion.main",
        ):
            module = sys.modules.get(name)
            getter = (
                getattr(module, "get_private_companion_api", None) if module else None
            )
            if callable(getter):
                api = getter()
                if api is not None:
                    return api
        return None

    def _companion_scene_prompt(self, room: GameRoom) -> str:
        if (
            not room.player_identity_confirmed
            or room.source != "private"
            or len(room.visitors) != 1
            or (room.multiplayer.enabled and len(room.multiplayer.seats) > 1)
        ):
            return ""
        api = self._private_companion_api()
        getter = getattr(api, "get_realtime_context", None) if api else None
        if not callable(getter):
            return ""
        try:
            result = getter(room.player_qq or room.creator_qq, purpose="game")
            return str(result.get("prompt") or "") if isinstance(result, dict) else ""
        except Exception as exc:
            logger.debug("[GameCompanion] 读取陪伴生活场景失败: %s", exc)
            return ""

    def _notify_companion_activity(self, room: GameRoom, phase: str) -> None:
        api = self._private_companion_api()
        if api is None:
            return
        activity_id = f"game-companion:{room.room_id}"
        try:
            if phase == "ended":
                notifier = getattr(api, "notify_external_activity_ended", None)
                if callable(notifier):
                    notifier(activity_id)
                return
            method_name = (
                "notify_external_activity_started"
                if phase == "started"
                else "notify_external_activity_updated"
            )
            notifier = getattr(api, method_name, None)
            if callable(notifier):
                notifier(
                    activity_id,
                    user_id=room.player_qq or room.creator_qq,
                    kind="shared_game",
                    label=f"正在和用户玩{self._game_label(room.game_type)}",
                    source_plugin=PLUGIN_NAME,
                    ttl_seconds=max(60, self.manager.idle_timeout or 300),
                    metadata={"room_id": room.room_id, "game": room.game_type},
                )
        except Exception as exc:
            logger.debug("[GameCompanion] 同步陪伴活动状态失败: %s", exc)

    @staticmethod
    def _current_player_visitors(room: GameRoom) -> list[Visitor]:
        if room.multiplayer.enabled:
            return [
                visitor
                for seat in room.multiplayer.seats
                if (visitor := room.visitors.get(seat.visitor_token)) is not None
                and visitor.identity_confirmed
                and visitor.qq
            ]
        player = room.player
        return (
            [player]
            if player is not None and player.identity_confirmed and player.qq
            else []
        )

    def _capture_round_participants(self, room: GameRoom, *, reset: bool = False) -> None:
        if reset:
            room.round_participant_qqs.clear()
        for visitor in self._current_player_visitors(room):
            room.round_participant_qqs.add(visitor.qq)
            room.participant_names[visitor.qq] = visitor.display_name

    def _queue_companion_round_event(
        self, room: GameRoom, payload: dict[str, Any]
    ) -> None:
        if not self.companion_afterglow_enabled:
            return
        task = self._spawn(
            self._report_companion_game_event(room, "round_finished", payload)
        )
        self._companion_round_event_tasks[room.room_id] = task

        def clear(finished: asyncio.Task) -> None:
            if self._companion_round_event_tasks.get(room.room_id) is finished:
                self._companion_round_event_tasks.pop(room.room_id, None)

        task.add_done_callback(clear)

    async def _report_companion_game_event(
        self,
        room: GameRoom,
        event_type: str,
        payload: dict[str, Any],
        *,
        visitors: list[Visitor] | None = None,
    ) -> None:
        if not self.companion_afterglow_enabled:
            return
        api = self._private_companion_api()
        recorder = getattr(api, "record_game_event", None) if api else None
        if not callable(recorder):
            logger.debug(
                "[GameCompanion] 陪伴插件未提供游戏余韵 API，已跳过联动"
            )
            return
        if visitors is None:
            by_qq = {visitor.qq: visitor for visitor in self._current_player_visitors(room)}
            for qq in room.round_participant_qqs:
                if qq not in by_qq:
                    by_qq[qq] = Visitor(
                        token="",
                        number=0,
                        qq=qq,
                        display_name=room.participant_names.get(qq, ""),
                        identity_confirmed=True,
                    )
            participants = list(by_qq.values())
        else:
            participants = [
                visitor
                for visitor in visitors
                if visitor.identity_confirmed and visitor.qq
            ]
        if not participants:
            return
        raw_result = str(payload.get("result") or "")
        bot_result = {
            "human_win": "bot_loss",
            "bot_win": "bot_win",
            "draw": "draw",
        }.get(raw_result, "completed")
        score = room.current_score
        round_number = score.completed

        async def submit(visitor: Visitor) -> None:
            event_id = (
                f"{room.room_id}:{room.game_type}:{round_number}:"
                f"{event_type}:{visitor.qq}"
            )
            event_payload = {
                "event_id": event_id,
                "event_type": event_type,
                "user_id": visitor.qq,
                "user_name": visitor.display_name,
                "game": room.game_type,
                "game_label": self._game_label(room.game_type),
                "bot_result": bot_result,
                "request_text": str(payload.get("request_text") or "")[:240],
                "recent_context": self._companion_game_recent_context(
                    room, visitor.qq
                ),
                "room_id": room.room_id,
                "session_id": room.session_id,
                "scope": room.source,
                "difficulty": room.difficulty,
                "round_number": round_number,
                "score": {
                    "completed": score.completed,
                    "human_wins": score.human_wins,
                    "bot_wins": score.bot_wins,
                    "draws": score.draws,
                },
                "occurred_at": time.time(),
                "source_plugin": PLUGIN_NAME,
            }
            try:
                result = recorder(event_payload)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                logger.debug(
                    "[GameCompanion] 为玩家 %s 上报游戏余韵失败: %s",
                    visitor.qq,
                    exc,
                )

        await asyncio.gather(*(submit(visitor) for visitor in participants))

    @staticmethod
    def _companion_game_recent_context(room: GameRoom, qq: str) -> str:
        transcript = room.chat_transcripts.get(str(qq or ""), [])
        lines: list[str] = []
        for entry in transcript[-6:]:
            content = " ".join(str(entry.get("content") or "").split())[:180]
            if not content:
                continue
            role = "用户" if entry.get("role") == "user" else "Bot"
            lines.append(f"{role}：{content}")
        return "\n".join(lines)[:900]

    def _register_companion_invite_ability(self) -> bool:
        if not self.companion_invites_enabled:
            return False
        now = time.monotonic()
        if now < self._next_companion_registration_at:
            return bool(self._companion_invite_api)
        self._next_companion_registration_at = now + 15
        api = self._private_companion_api()
        if api is None:
            return False
        if api is self._companion_invite_api:
            return True
        if self._companion_invite_api is not None:
            self._unregister_companion_invite_ability()
        registrar = getattr(api, "register_proactive_ability", None)
        if not callable(registrar):
            return False
        try:
            registered = bool(
                registrar(
                    {
                        "name": "game_companion_invite",
                        "module": "游戏伴侣",
                        "label": "邀请一起玩游戏",
                        "description": "结合近期共同游戏、当前人格和生活状态，自然邀请用户玩一局游戏。",
                        "when": "有闲暇、想陪用户玩，或对最近胜负仍有余味时",
                        "use_for": "提出低压力的游戏邀请，或自然约一次再战",
                        "avoid": "用户正在游戏、房间已满、关系或免打扰不适合时不要邀请；不要提前创建房间",
                        "share_probability": self.companion_invite_probability,
                        "min_interval_hours": self.companion_invite_cooldown_hours,
                        "default_enabled": True,
                        "availability": self._companion_invite_available,
                        "executor": self._execute_companion_invite,
                    }
                )
            )
        except Exception as exc:
            logger.debug("[GameCompanion] 注册陪伴主动邀请失败: %s", exc)
            return False
        if registered:
            self._companion_invite_api = api
            logger.info("[GameCompanion] 已向陪伴插件注册主动游戏邀请能力")
        return registered

    def _unregister_companion_invite_ability(self) -> None:
        api = self._companion_invite_api
        self._companion_invite_api = None
        if api is None:
            return
        unregister = getattr(api, "unregister_proactive_ability", None)
        if callable(unregister):
            try:
                unregister("game_companion_invite")
            except Exception as exc:
                logger.debug("[GameCompanion] 注销陪伴主动邀请失败: %s", exc)

    def _companion_invite_available(self, context: dict[str, Any]) -> bool:
        if not (
            self.companion_invites_enabled
            and self.server_enabled
            and self.private_rooms_enabled
        ):
            return False
        user = context.get("user") if isinstance(context, dict) else {}
        user = user if isinstance(user, dict) else {}
        user_id = str(user.get("user_id") or "").strip()
        if not user_id:
            return False
        for room in self.manager.rooms.values():
            if user_id in {room.creator_qq, room.player_qq}:
                return False
            if any(visitor.qq == user_id for visitor in self._current_player_visitors(room)):
                return False
        active_private = sum(
            room.source == "private" for room in self.manager.rooms.values()
        )
        if self.manager.max_private_rooms and active_private >= self.manager.max_private_rooms:
            return False
        afterglow = user.get("game_afterglow")
        if isinstance(afterglow, dict):
            expires_at = self._safe_float(afterglow.get("expires_at"))
            invite_interest = self._safe_int(afterglow.get("invite_interest"))
            if expires_at > time.time() and invite_interest < 20:
                return False
        return True

    def _execute_companion_invite(self, context: dict[str, Any]) -> dict[str, Any]:
        user = context.get("user") if isinstance(context, dict) else {}
        user = user if isinstance(user, dict) else {}
        afterglow = user.get("game_afterglow")
        afterglow = afterglow if isinstance(afterglow, dict) else {}
        active_afterglow = self._safe_float(afterglow.get("expires_at")) > time.time()
        last_game = str(afterglow.get("game_label") or "").strip()
        tone = str(afterglow.get("tone") or "").strip()[:160]
        games = "五子棋、中国象棋、井字棋、海龟汤、贪心骰子或你画我猜"
        details = (
            f"最近和该用户玩的游戏是{last_game}，当前余味是：{tone}。"
            if active_afterglow and last_game and tone
            else f"可以从{games}中按人格和用户偏好自然挑一种。"
        )
        return {
            "ok": True,
            "context": (
                "请按当前人格向该用户发出一次轻松、可拒绝的游戏邀请。"
                f"{details}只表达邀请，不创建房间、不生成链接；等用户明确接受后再由正常对话工具创建。"
            ),
            "summary": "想邀请用户一起玩游戏",
            "status": "已形成游戏邀请动机",
        }

    async def _send_to_origin(self, room: GameRoom, text: str) -> None:
        if not text:
            return
        try:
            await self.context.send_message(
                room.session_id, MessageChain([Plain(text)])
            )
        except Exception as exc:
            logger.debug("[GameCompanion] 回发游戏消息失败: %s", exc)

    async def _deliver_room_link(
        self,
        room: GameRoom,
        url: str,
        *,
        reused: bool,
        restarted: bool,
    ) -> bool:
        if restarted:
            title = "新一局已在原游戏房间开始："
        elif reused:
            title = "继续使用当前游戏房间："
        else:
            title = f"{self._game_label(room.game_type)}房间已准备好："
        lines = [title, url]
        if room.player is None and self.manager.empty_player_timeout:
            lines.append(
                f"请在 {self.manager.empty_player_timeout} 秒内进入玩家席，"
                "否则房间会自动销毁。"
            )
        try:
            delivered = await self.context.send_message(
                room.session_id,
                MessageChain([Plain("\n".join(lines))]),
            )
        except Exception as exc:
            logger.warning(
                "[GameCompanion] 独立发送房间链接失败，将交由模型回复回退: %s",
                exc,
            )
            return False
        if not delivered:
            logger.warning(
                "[GameCompanion] 未找到房间会话对应平台，将交由模型回复回退: session=%s",
                room.session_id,
            )
            return False
        logger.info(
            "[GameCompanion] 房间链接已作为独立纯文字消息发送: room=%s session=%s",
            room.room_id,
            room.session_id,
        )
        return True

    async def _watchdog(self) -> None:
        try:
            while True:
                await asyncio.sleep(2)
                await self.manager.sweep_expired()
                self._schedule_tunnel_recovery()
                self._register_companion_invite_ability()
        except asyncio.CancelledError:
            raise

    def _schedule_tunnel_recovery(self) -> None:
        if (
            self.public_base_url
            or not self.auto_quick_tunnel
            or not self.manager.rooms
            or not self.room_server.running
            or self.quick_tunnel.running
            or (
                self._tunnel_recovery_task is not None
                and not self._tunnel_recovery_task.done()
            )
        ):
            return
        now = asyncio.get_running_loop().time()
        if now < self._next_tunnel_retry_at:
            return
        self._next_tunnel_retry_at = now + 15
        self._tunnel_recovery_task = self._spawn(self._recover_quick_tunnel())

    async def _recover_quick_tunnel(self) -> None:
        try:
            self.quick_tunnel.local_url = self.room_server.local_base_url
            url = await self.quick_tunnel.start(timeout=40)
        except Exception as exc:
            logger.warning("[GameCompanion] 临时访问通道恢复失败，将稍后重试: %s", exc)
            return
        finally:
            self._tunnel_recovery_task = None
        logger.warning("[GameCompanion] 临时访问通道已恢复，新地址: %s", url)
        for room in list(self.manager.rooms.values()):
            await self._send_to_origin(
                room,
                "游戏访问通道已恢复，原临时链接已经失效。请使用新链接："
                f"{self._room_url(room)}",
            )

    def _room_link_instruction(self, room: GameRoom) -> str:
        instruction = "最终回复必须完整保留 room_url；"
        if room.admin_room:
            instruction += "这是管理员审核房间，访客需要由管理员在游戏管理台安排玩家。"
        else:
            bind_hint = (
                "在原群聊中 @Bot 发送"
                if room.source == "group"
                else "在原私聊中发送"
            )
            instruction += (
                f"提醒用户打开页面后查看一次性 QQ 绑定令牌，并{bind_hint}“绑定玩家 令牌”，"
                "绑定成功后再点击加入玩家席。"
            )
        if room.game_type == "turtle_soup":
            instruction += "说明玩家进入玩家席后由 Bot 准备题目。"
        elif room.game_type == "pig_dice":
            instruction += "说明玩家进入玩家席后直接开始，先手由系统随机决定。"
        elif room.game_type == "draw_guess":
            instruction += "说明玩家进入玩家席后在网页画布作画，并手动点击让 Bot 猜。"
        else:
            instruction += "说明玩家进入后可选择执棋方。"
        timeout = self.manager.empty_player_timeout
        if timeout:
            instruction += (
                f"明确提醒玩家在 {timeout} 秒内进入玩家席，否则房间会自动销毁。"
            )
        return instruction

    def _register_page_api(self) -> None:
        register_api = getattr(self.context, "register_web_api", None)
        if not callable(register_api):
            return
        register_api(f"{PAGE_API_PREFIX}/rooms", self.page_rooms, ["GET"], "Game rooms")
        register_api(
            f"{PAGE_API_PREFIX}/room/action",
            self.page_room_action,
            ["POST"],
            "Manage a game room",
        )
        register_api(
            f"{PAGE_API_PREFIX}/tunnel/start",
            self.page_tunnel_start,
            ["POST"],
            "Start game quick tunnel",
        )
        register_api(
            f"{PAGE_API_PREFIX}/tunnel/stop",
            self.page_tunnel_stop,
            ["POST"],
            "Stop game quick tunnel",
        )
        register_api(
            f"{PAGE_API_PREFIX}/xiangqi/install",
            self.page_xiangqi_install,
            ["POST"],
            "Install Pikafish",
        )

    async def page_rooms(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "data": {
                "rooms": [
                    room.admin_snapshot() for room in self.manager.rooms.values()
                ],
                "server": {
                    "running": self.room_server.running,
                    "port": self.room_server.port if self.room_server.running else None,
                    "public_base_url": self.public_base_url,
                },
                "tunnel": self.quick_tunnel.status(),
                "xiangqi_engine": self.xiangqi_engine.status(),
                "limits": {
                    "group": self.manager.max_group_rooms,
                    "private": self.manager.max_private_rooms,
                },
            },
        }

    async def page_room_action(self) -> dict[str, Any]:
        payload = await request.json(default={}) or {}
        room = self.manager.rooms.get(str(payload.get("room_id") or ""))
        if room is None:
            return {"status": "error", "message": "房间不存在或已经结束", "data": {}}
        action = str(payload.get("action") or "").strip().lower()
        try:
            if action == "assign":
                await self.manager.assign_player(
                    room,
                    int(payload.get("visitor_number") or 0),
                    str(payload.get("player_qq") or ""),
                )
            elif action == "demote":
                await self.manager.remove_player(
                    room, int(payload.get("visitor_number") or 0)
                )
            elif action == "kick":
                await self.manager.kick_visitor(
                    room, int(payload.get("visitor_number") or 0)
                )
            elif action == "pause":
                await self.manager.pause(room)
            elif action == "resume":
                await self.manager.resume(room)
            elif action == "switch_game":
                await self.manager.switch_game(
                    room,
                    self._game_type(payload.get("game_type")),
                    force=self._value_bool(payload.get("confirm_abandon")),
                )
            elif action == "close":
                await self.manager.destroy(room.room_id, "管理员关闭了房间")
            else:
                raise ValueError("不支持的管理操作")
        except (ValueError, RuntimeError, PermissionError) as exc:
            return {"status": "error", "message": str(exc), "data": {}}
        return {"status": "ok", "data": {"room_id": room.room_id, "action": action}}

    async def page_xiangqi_install(self) -> dict[str, Any]:
        try:
            status = await self.xiangqi_engine.install_latest()
        except (ValueError, RuntimeError, PermissionError, OSError) as exc:
            return {"status": "error", "message": str(exc), "data": {}}
        return {"status": "ok", "data": {"xiangqi_engine": status}}

    async def page_tunnel_start(self) -> dict[str, Any]:
        if self.public_base_url:
            return {"status": "error", "message": "已配置固定外部地址", "data": {}}
        try:
            if not self.room_server.running:
                await self.room_server.start()
            self.quick_tunnel.local_url = self.room_server.local_base_url
            url = await self.quick_tunnel.start(timeout=40)
        except Exception as exc:
            return {"status": "error", "message": str(exc), "data": {}}
        return {
            "status": "ok",
            "data": {"url": url, "tunnel": self.quick_tunnel.status()},
        }

    async def page_tunnel_stop(self) -> dict[str, Any]:
        if self.manager.rooms:
            return {
                "status": "error",
                "message": "仍有活动房间，不能停止访问通道",
                "data": {},
            }
        await self.quick_tunnel.stop()
        await self.room_server.stop()
        return {"status": "ok", "data": {"tunnel": self.quick_tunnel.status()}}

    def _cfg(self, dotted_key: str, default: Any = None) -> Any:
        if dotted_key in self.config:
            return self.config.get(dotted_key, default)
        current: Any = self.config
        for part in dotted_key.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current.get(part)
        return default if current is None else current

    def _cfg_str(self, dotted_key: str, default: str = "") -> str:
        return str(self._cfg(dotted_key, default) or "").strip()

    def _cfg_bool(self, dotted_key: str, default: bool) -> bool:
        value = self._cfg(dotted_key, default)
        if isinstance(value, str):
            return value.strip().lower() in {"true", "1", "yes", "on", "是", "开启"}
        return bool(value)

    def _cfg_int(
        self, dotted_key: str, default: int, *, minimum: int, maximum: int
    ) -> int:
        try:
            value = int(self._cfg(dotted_key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(value, maximum))

    def _cfg_non_negative(self, dotted_key: str, default: int) -> int:
        try:
            return max(0, int(self._cfg(dotted_key, default)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _parse_qq_ids(value: Any) -> set[str]:
        if isinstance(value, list):
            values = value
        else:
            values = re.split(r"[\s,，;；]+", str(value or ""))
        return {str(item).strip() for item in values if str(item).strip().isdigit()}

    @staticmethod
    def _difficulty(value: Any) -> Difficulty:
        normalized = str(value or "normal").strip().lower()
        return normalized if normalized in {"easy", "normal", "hard"} else "normal"  # type: ignore[return-value]

    @staticmethod
    def _turtle_soup_mode(value: Any) -> TurtleSoupMode:
        normalized = str(value or "bot_host").strip().lower()
        aliases = {
            "bot_host": "bot_host",
            "bot-host": "bot_host",
            "bot出题": "bot_host",
            "你出题": "bot_host",
            "player_host": "player_host",
            "player-host": "player_host",
            "玩家出题": "player_host",
            "我出题": "player_host",
            "bot猜": "player_host",
        }
        return aliases.get(normalized, "bot_host")  # type: ignore[return-value]

    def _room_actor_authorized(self, room: GameRoom, actor_qq: str) -> bool:
        if (
            actor_qq in {room.creator_qq, room.player_qq}
            or actor_qq in self.game_admin_ids
        ):
            return True
        return bool(
            room.multiplayer.enabled
            and room.multiplayer.seat_for_qq(actor_qq) is not None
        )

    @staticmethod
    def _game_type(value: Any) -> GameType:
        normalized = str(value or "gomoku").strip().lower()
        aliases = {
            "gomoku": "gomoku",
            "五子棋": "gomoku",
            "xiangqi": "xiangqi",
            "象棋": "xiangqi",
            "中国象棋": "xiangqi",
            "tictactoe": "tictactoe",
            "tic-tac-toe": "tictactoe",
            "tic_tac_toe": "tictactoe",
            "井字棋": "tictactoe",
            "圈叉棋": "tictactoe",
            "turtle_soup": "turtle_soup",
            "turtle-soup": "turtle_soup",
            "海龟汤": "turtle_soup",
            "pig_dice": "pig_dice",
            "pig-dice": "pig_dice",
            "pig": "pig_dice",
            "贪心骰子": "pig_dice",
            "贪心骰": "pig_dice",
            "骰子": "pig_dice",
            "draw_guess": "draw_guess",
            "draw-guess": "draw_guess",
            "你画我猜": "draw_guess",
            "画画猜词": "draw_guess",
            "画图猜词": "draw_guess",
        }
        if normalized not in aliases:
            raise ValueError("目前只支持五子棋、中国象棋、井字棋、海龟汤、贪心骰子和你画我猜")
        return aliases[normalized]  # type: ignore[return-value]

    @staticmethod
    def _game_label(game_type: GameType) -> str:
        return {
            "gomoku": "五子棋",
            "xiangqi": "中国象棋",
            "tictactoe": "井字棋",
            "turtle_soup": "海龟汤",
            "pig_dice": "贪心骰子",
            "draw_guess": "你画我猜",
        }[game_type]

    @staticmethod
    def _value_bool(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"true", "1", "yes", "on", "是", "确认"}
        return value is True

    @staticmethod
    def _validated_public_url(value: str) -> str:
        if not value:
            return ""
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.netloc:
            logger.warning("[GameCompanion] 外部访问地址必须是 HTTPS，当前配置已忽略")
            return ""
        return value.rstrip("/")

    @staticmethod
    def _json_error(message: str) -> str:
        return json.dumps({"ok": False, "error": str(message)}, ensure_ascii=False)

    def _spawn(self, operation: Any) -> asyncio.Task:
        task = asyncio.create_task(operation)
        self._background_tasks.add(task)

        def finish(finished: asyncio.Task) -> None:
            self._background_tasks.discard(finished)
            try:
                finished.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.debug("[GameCompanion] 后台任务失败: %s", exc)

        task.add_done_callback(finish)
        return task
