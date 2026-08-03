from __future__ import annotations

import asyncio
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

from .gomoku import Difficulty
from .models import GameRoom, GameType
from .pikafish import PikafishService
from .room_manager import RoomManager
from .server import GameRoomServer
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
    public_judge_history,
    question_judge_prompt,
    validation_passed,
    validation_prompt,
)

PLUGIN_NAME = "astrbot_plugin_game_companion"
PLUGIN_VERSION = "0.1.4"
PAGE_API_PREFIX = f"/{PLUGIN_NAME}/page"


@register(
    PLUGIN_NAME,
    "AstrBot Community",
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
        self.commentary_cooldown = self._cfg_int(
            "game.commentary_cooldown_seconds", 45, minimum=10, maximum=600
        )
        self.turtle_soup_max_hints = self._cfg_int(
            "turtle_soup.max_hints", 3, minimum=0, maximum=8
        )
        self.turtle_soup_content_level = normalize_content_level(
            self._cfg("turtle_soup.content_level", "normal")
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
        self._register_page_api()

    async def initialize(self) -> None:
        """Start only the in-memory watchdog; the port opens lazily on demand."""
        self._watchdog_task = asyncio.create_task(self._watchdog())
        logger.info(
            "[GameCompanion] 游戏伴侣已加载；房间服务将在首次创建房间时按需启动"
        )

    async def terminate(self) -> None:
        """Invalidate every room and stop only plugin-owned resources."""
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
        turtle_soup（海龟汤）和 pig_dice（贪心骰子）。
        不要因为普通聊天中偶然提到游戏名称就调用本工具。
        当前 QQ 会话已有房间时绝不能先关闭再创建；用户说“再来一局”必须调用
        game_companion_control_room 的 rematch 动作，在原房间直接开始下一局。
        若已有另一种游戏正在进行，必须先得到用户明确同意放弃当前局，再传 confirm_abandon=true。

        Args:
            game_type(string): 游戏类型，只能是 gomoku、xiangqi、tictactoe、turtle_soup 或 pig_dice。
            difficulty(string): 你决定使用的难度，只能是 easy、normal、hard；贪心骰子中分别表示稳健、均衡和大胆。
            confirm_abandon(boolean): 切换游戏且当前局未结束时，用户是否已明确同意放弃本局。
        """
        try:
            game_type = self._game_type(kwargs.get("game_type"))
        except ValueError as exc:
            return self._json_error(str(exc))
        difficulty = self._difficulty(kwargs.get("difficulty"))
        try:
            room, reused, restarted = await self._create_or_reuse_room_from_event(
                event,
                difficulty,
                game_type,
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
        """处理用户在真实 QQ 会话中提出的游戏房间操作。

        身份确认、抢占纠正、悔棋、暂停、继续、认输、再来一局、切换游戏和结束房间必须使用本工具，不能仅口头答应。
        悔棋是否同意由你结合人格决定，并通过 allow 表达决定。
        用户说“再来一局”时使用 rematch，保留原房间和链接，不得先 close 再创建房间。

        Args:
            action(string): status、confirm_player、correct_player、undo、pause、resume、resign、rematch、switch_game、close。
            room_id(string): 可选房间编号；当前会话只有一个房间时可以留空。
            visitor_number(number): correct_player 时创建者声明的浏览器序号。
            allow(boolean): undo 时你是否同意悔棋。
            difficulty(string): rematch 时你根据人格决定的新棋力，只能是 easy、normal、hard。
            game_type(string): switch_game 时切换到 gomoku、xiangqi、tictactoe、turtle_soup 或 pig_dice。
            confirm_abandon(boolean): 当前局未结束时，用户是否已明确同意放弃本局。
        """
        action = str(kwargs.get("action") or "status").strip().lower()
        actor_qq = str(event.get_sender_id() or "").strip()
        try:
            room = self._resolve_event_room(event, str(kwargs.get("room_id") or ""))
            if action == "status":
                return json.dumps(
                    {"ok": True, "room": room.public_snapshot()}, ensure_ascii=False
                )
            authorized = (
                actor_qq in {room.creator_qq, room.player_qq}
                or actor_qq in self.game_admin_ids
            )
            if action == "confirm_player":
                await self.manager.confirm_creator(room, actor_qq)
            elif action == "correct_player":
                await self.manager.correct_creator(
                    room, actor_qq, int(kwargs.get("visitor_number") or 0)
                )
            elif action == "undo":
                if not authorized:
                    raise PermissionError("只有房主、当前玩家或游戏管理员能提出悔棋")
                if not self._value_bool(kwargs.get("allow")):
                    room.add_message("bot", "这次不悔棋，继续下吧。")
                    return json.dumps(
                        {"ok": True, "accepted": False}, ensure_ascii=False
                    )
                removed = await self.manager.undo(room)
                return json.dumps(
                    {"ok": True, "accepted": True, "removed_stones": removed},
                    ensure_ascii=False,
                )
            elif action == "pause":
                if not authorized:
                    raise PermissionError("没有暂停该房间的权限")
                await self.manager.pause(room)
            elif action == "resume":
                if not authorized:
                    raise PermissionError("没有继续该房间的权限")
                await self.manager.resume(room)
            elif action == "resign":
                if not authorized:
                    raise PermissionError("只有房主或当前玩家能认输")
                await self.manager.resign(room)
            elif action == "rematch":
                if not authorized:
                    raise PermissionError("只有房主、当前玩家或游戏管理员能开始下一局")
                await self.manager.restart_finished_game(
                    room,
                    difficulty=self._difficulty(
                        kwargs.get("difficulty") or room.difficulty
                    ),
                )
                return json.dumps(
                    {
                        "ok": True,
                        "action": action,
                        "room_id": room.room_id,
                        "room_url": self._room_url(room),
                        "difficulty": room.difficulty,
                        "instruction": "下一局已在原房间直接开始；不得关闭房间或创建新链接。",
                    },
                    ensure_ascii=False,
                )
            elif action == "switch_game":
                if not authorized:
                    raise PermissionError("没有切换该房间游戏的权限")
                target = self._game_type(kwargs.get("game_type"))
                switched = await self.manager.switch_game(
                    room,
                    target,
                    force=self._value_bool(kwargs.get("confirm_abandon")),
                )
                return json.dumps(
                    {
                        "ok": True,
                        "action": action,
                        "room_id": room.room_id,
                        "room_url": self._room_url(room),
                        "game_type": room.game_type,
                        "switched": switched,
                        "instruction": "已保留原房间、玩家和链接，只切换了游戏。",
                    },
                    ensure_ascii=False,
                )
            elif action == "close":
                if not authorized:
                    raise PermissionError("没有关闭该房间的权限")
                await self.manager.destroy(room.room_id, "用户主动结束了房间")
            else:
                raise ValueError("不支持的房间操作")
            return json.dumps(
                {"ok": True, "action": action, "room_id": room.room_id},
                ensure_ascii=False,
            )
        except (ValueError, RuntimeError, PermissionError, OSError) as exc:
            return self._json_error(str(exc))

    @filter.llm_tool(name="game_companion_turtle_soup")
    async def turtle_soup_tool(self, event: AstrMessageEvent, **kwargs: Any) -> str:
        """处理当前玩家在 QQ 中进行的海龟汤问答。

        当前房间为海龟汤时，玩家提出可用“是/否/无关/部分正确”回答的问题、
        提交完整推理或申请提示，必须调用本工具。普通聊天不要调用。
        玩家放弃时调用 game_companion_control_room 的 resign，重新出题时调用 rematch。

        Args:
            action(string): ask、answer 或 hint。
            text(string): ask 时为单个问题，answer 时为完整推理；hint 时可留空。
            room_id(string): 可选房间编号；当前会话只有一个房间时可以留空。
        """
        action = str(kwargs.get("action") or "ask").strip().lower()
        actor_qq = str(event.get_sender_id() or "").strip()
        try:
            room = self._resolve_event_room(event, str(kwargs.get("room_id") or ""))
            if action == "ask":
                result = await self.submit_turtle_soup_question(
                    room,
                    str(kwargs.get("text") or ""),
                    source="qq",
                    actor_qq=actor_qq,
                )
                return json.dumps(
                    {
                        "ok": True,
                        **result,
                        "instruction": (
                            "最终回复必须准确保留 reply 中的是非裁决，不得补充、猜测或泄露汤底；"
                            "可以按当前人格增加一句不含新线索的简短反应。"
                        ),
                    },
                    ensure_ascii=False,
                )
            if action == "answer":
                result = await self.submit_turtle_soup_answer(
                    room,
                    str(kwargs.get("text") or ""),
                    source="qq",
                    actor_qq=actor_qq,
                )
                return json.dumps(
                    {
                        "ok": True,
                        **result,
                        "instruction": (
                            "按 solved 和 reply 准确回应；solved=false 时不得透露缺失事实，"
                            "solved=true 时可以自然揭晓 solution。"
                        ),
                    },
                    ensure_ascii=False,
                )
            if action == "hint":
                hint = await self.manager.request_turtle_soup_hint(
                    room, source="qq", actor_qq=actor_qq
                )
                return json.dumps(
                    {
                        "ok": True,
                        "hint": hint,
                        "instruction": "准确转述 hint，不得额外补充隐藏线索。",
                    },
                    ensure_ascii=False,
                )
            raise ValueError("海龟汤操作只能是 ask、answer 或 hint")
        except (ValueError, RuntimeError, PermissionError, OSError) as exc:
            return self._json_error(str(exc))

    @filter.command("游戏伴侣")
    async def game_companion_status(self, event: AstrMessageEvent):
        """Return a small fallback status without taking over ordinary chat."""
        rooms = self.manager.for_session(event.unified_msg_origin)
        if not rooms:
            yield event.plain_result(
                "当前会话没有活动游戏房间。直接告诉我想玩五子棋、象棋、井字棋、海龟汤或贪心骰子即可。"
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
            "",
            "房间容量",
            f"群聊：{capacity(group_count, self.manager.max_group_rooms, self.group_rooms_enabled)}",
            f"私聊：{capacity(private_count, self.manager.max_private_rooms, self.private_rooms_enabled)}",
            "",
            "直接用自然语言告诉 Bot 想玩哪个游戏即可。",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.on_llm_request(priority=-10)
    async def inject_game_context(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """Append concise room state while leaving every normal plugin active."""
        rooms = self.manager.for_session(event.unified_msg_origin)
        if not rooms:
            return
        lines = ["<game_companion_context>"]
        for room in rooms:
            player = room.player
            game = room.game
            progress = len(game.history) if game else 0
            progress_label = {
                "turtle_soup": "问答数",
                "pig_dice": "掷骰记录",
            }.get(room.game_type, "手数")
            lines.append(
                "房间 {room_id}：游戏={game_type}，状态={status}，创建者QQ={creator}，玩家序号={number}，"
                "已确认身份={confirmed}，难度={difficulty}，{progress_label}={progress}。".format(
                    room_id=room.room_id,
                    game_type=self._game_label(room.game_type),
                    status=room.status,
                    creator=room.creator_qq,
                    number=player.number if player else "无",
                    confirmed=room.player_identity_confirmed,
                    difficulty=room.difficulty,
                    progress_label=progress_label,
                    progress=progress,
                )
            )
        lines.append(
            "涉及身份、悔棋、暂停、认输、再来一局、切换游戏或结束时调用 game_companion_control_room；"
            "海龟汤中的是非提问、完整猜测和提示请求必须调用 game_companion_turtle_soup；"
            "贪心骰子的掷骰和收手只在 WebUI 操作，不要通过 QQ 工具伪造点数；"
            "再来一局必须使用 rematch 并保留原房间，绝不能 close 后调用创建工具；普通闲聊照常回答。"
        )
        lines.append("</game_companion_context>")
        req.system_prompt = (
            str(req.system_prompt or "") + "\n\n" + "\n".join(lines)
        ).strip()

    async def _create_room_from_event(
        self,
        event: AstrMessageEvent,
        difficulty: Difficulty,
        game_type: GameType,
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
        )
        return room

    async def _create_or_reuse_room_from_event(
        self,
        event: AstrMessageEvent,
        difficulty: Difficulty,
        game_type: GameType,
        *,
        confirm_abandon: bool,
    ) -> tuple[GameRoom, bool, bool]:
        rooms = self.manager.for_session(event.unified_msg_origin)
        if len(rooms) > 1:
            raise ValueError("当前会话已有多个活动房间，请先说明要使用的房间编号")
        if not rooms:
            if game_type == "xiangqi":
                await self.xiangqi_engine.ensure_ready()
            room = await self._create_room_from_event(event, difficulty, game_type)
            return room, False, False
        room = rooms[0]
        restarted = False
        actor_qq = str(event.get_sender_id() or "").strip()
        authorized = (
            actor_qq in {room.creator_qq, room.player_qq}
            or actor_qq in self.game_admin_ids
        )
        if room.game_type != game_type:
            if not authorized:
                raise PermissionError("没有切换当前房间游戏的权限")
            await self.manager.switch_game(room, game_type, force=confirm_abandon)
        elif room.status in {"finished", "rematch_pending"} and authorized:
            await self.manager.restart_finished_game(room, difficulty=difficulty)
            restarted = True
        await self._ensure_public_access()
        return room, True, restarted

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
            if room.player_identity_confirmed:
                self._notify_companion_activity(room, "updated")
            opening = (
                "新的一局海龟汤已经出题完成，请用当前人格简短邀请玩家开始提问。"
                if room.game_type == "turtle_soup"
                else f"新的一局{game_label}刚刚开始，请用当前人格简短说一句开场话。"
            )
            self._spawn(
                self._comment(
                    room,
                    opening,
                    history_event=f"[游戏事件] 新的一局{game_label}开始。",
                )
            )
            return
        if event_name == "player_confirmed":
            self._notify_companion_activity(room, "started")
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
                        history_event=f"[游戏事件] {tactical_prompt}。",
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
                        history_event="[游戏事件] 玩家在海龟汤中发现了关键线索。",
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
                        history_event="[游戏事件] 玩家提交了接近但不完整的海龟汤推理。",
                    )
                )
            return
        if event_name == "soup_hint_revealed":
            if payload.get("source") == "web":
                hint = str(payload.get("hint") or "")
                self._spawn(self._announce_turtle_soup_hint(room, hint))
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
                        history_event=f"[游戏事件] 贪心骰子：{key_event}。",
                    )
                )
            return
        if event_name == "game_finished":
            if room.game_type == "turtle_soup" and isinstance(
                room.game, TurtleSoupGame
            ):
                result = (
                    "玩家成功解开汤底" if room.game.solved else "玩家放弃，汤底已揭晓"
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
                    history_event=f"[游戏事件] {game_label}本局结果：{result}。",
                )
            )
            return
        if event_name == "rematch_requested":
            self._spawn(self._decide_rematch(room))
            return
        if event_name == "game_switched":
            self._notify_companion_activity(room, "updated")
            return
        if event_name == "room_destroyed":
            self._notify_companion_activity(room, "ended")
            await self._record_room_memory(room)

    async def _comment(
        self, room: GameRoom, prompt: str, *, history_event: str
    ) -> None:
        text = await self._generate_persona_text(room, prompt)
        if not text or room.status == "closed":
            return
        room.add_message("bot", text)
        await self._sync_conversation_pair(room, history_event, text)
        await self._send_to_origin(room, text)

    async def _decide_rematch(self, room: GameRoom) -> None:
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
        await self._sync_conversation_pair(room, "[游戏事件] 玩家申请再来一局。", reply)
        await self._send_to_origin(room, reply)
        if (
            accept
            and room.game_type == "turtle_soup"
            and room.status == "setup"
            and room.player_token
        ):
            await self.manager.start_game(room, room.player_token, "")

    async def _prepare_turtle_soup(self, room: GameRoom, game: TurtleSoupGame) -> None:
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

    async def _announce_turtle_soup_hint(self, room: GameRoom, hint: str) -> None:
        if not hint or room.status == "closed":
            return
        intro = await self._generate_persona_text(
            room,
            "玩家刚申请了一次海龟汤提示。请用当前人格说一句很短的引子，"
            "不要猜测或补充任何线索。",
        )
        text = f"{intro}\n提示：{hint}" if intro else f"提示：{hint}"
        room.add_message("bot", text)
        await self._sync_conversation_pair(
            room, "[游戏事件] 玩家在海龟汤中申请了一次提示。", text
        )
        await self._send_to_origin(room, text)

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
        if not room.player_identity_confirmed:
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
        if (
            not self.record_shared_experience
            or not room.player_identity_confirmed
            or total_completed < 1
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
                        f"海龟汤 {score.completed} 题（成功解开 {score.human_wins} 题，"
                        f"放弃 {score.bot_wins} 题，共提问 {room.turtle_soup_stats.questions} 次，"
                        f"使用提示 {room.turtle_soup_stats.hints} 次）"
                    )
                else:
                    summaries.append(
                        f"{self._game_label(game_type)} {score.completed} 局（用户胜 {score.human_wins} 局，"
                        f"Bot 胜 {score.bot_wins} 局，平局 {score.draws} 局）"
                    )
        summary = "Bot 与用户完成了游戏：" + "；".join(summaries) + "。"
        try:
            await recorder(
                content=summary,
                experience_type="game",
                user_id=room.player_qq,
                user_name=room.creator_name
                if room.player_qq == room.creator_qq
                else "",
                scope=room.source,
                session_id=room.session_id,
                platform=room.platform,
                source_plugin=PLUGIN_NAME,
                memory_id=f"game-companion-{room.room_id}",
                confidence=0.95,
                importance=0.66,
                metadata={
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
                },
            )
        except Exception as exc:
            logger.debug("[GameCompanion] 写入共同游戏经历失败: %s", exc)

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
        if not room.player_identity_confirmed:
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

    async def _sync_conversation_pair(
        self, room: GameRoom, game_event: str, bot_text: str
    ) -> bool:
        manager = getattr(self.context, "conversation_manager", None)
        get_current = (
            getattr(manager, "get_curr_conversation_id", None) if manager else None
        )
        add_pair = getattr(manager, "add_message_pair", None) if manager else None
        if not callable(get_current) or not callable(add_pair):
            return False
        async with room.conversation_lock:
            try:
                conversation_id = await get_current(room.session_id)
                if not conversation_id:
                    return False
                await add_pair(
                    conversation_id,
                    {"role": "user", "content": str(game_event or "")[:500]},
                    {"role": "assistant", "content": str(bot_text or "")[:800]},
                )
                return True
            except Exception as exc:
                logger.debug("[GameCompanion] 同步当前 AstrBot 对话记录失败: %s", exc)
                return False

    async def _watchdog(self) -> None:
        try:
            while True:
                await asyncio.sleep(2)
                await self.manager.sweep_expired()
                self._schedule_tunnel_recovery()
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
        if room.game_type == "turtle_soup":
            instruction += "说明玩家进入玩家席后由 Bot 准备题目。"
        elif room.game_type == "pig_dice":
            instruction += "说明玩家进入玩家席后直接开始，先手由系统随机决定。"
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
                await self.manager.remove_player(room)
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
        }
        if normalized not in aliases:
            raise ValueError("目前只支持五子棋、中国象棋、井字棋、海龟汤和贪心骰子")
        return aliases[normalized]  # type: ignore[return-value]

    @staticmethod
    def _game_label(game_type: GameType) -> str:
        return {
            "gomoku": "五子棋",
            "xiangqi": "中国象棋",
            "tictactoe": "井字棋",
            "turtle_soup": "海龟汤",
            "pig_dice": "贪心骰子",
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
