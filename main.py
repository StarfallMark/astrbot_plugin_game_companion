from __future__ import annotations

import asyncio
import inspect
import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api.web import request

from .gomoku import Difficulty
from .models import GameRoom
from .room_manager import RoomManager
from .server import GameRoomServer
from .tunnel import QuickTunnel

PLUGIN_NAME = "astrbot_plugin_game_companion"
PLUGIN_VERSION = "0.1.0"
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

        self.manager = RoomManager(
            max_group_rooms=self._cfg_non_negative("rooms.max_group_rooms", 1),
            max_private_rooms=self._cfg_non_negative("rooms.max_private_rooms", 1),
            empty_player_timeout=self._cfg_non_negative(
                "rooms.empty_player_timeout_seconds", 60
            ),
            idle_timeout=self._cfg_non_negative("rooms.idle_timeout_seconds", 300),
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
        logger.info("[GameCompanion] 所有运行态房间均已销毁")

    @filter.llm_tool(name="game_companion_create_room")
    async def create_room_tool(self, event: AstrMessageEvent, **kwargs: Any) -> str:
        """仅在用户明确想和 Bot 玩游戏时创建可视化游戏房间。

        棋力必须由你结合当前人格、关系和用户请求自行决定，不能把棋力选择交给网页用户。
        当前仅支持 gomoku。不要因为普通聊天中偶然提到游戏名称就调用本工具。

        Args:
            game_type(string): 游戏类型，当前固定为 gomoku。
            difficulty(string): 你决定使用的棋力，只能是 easy、normal、hard。
        """
        game_type = str(kwargs.get("game_type") or "gomoku").strip().lower()
        if game_type not in {"gomoku", "五子棋"}:
            return self._json_error("目前只支持五子棋")
        difficulty = self._difficulty(kwargs.get("difficulty"))
        try:
            room = await self._create_room_from_event(event, difficulty)
            url = self._room_url(room)
        except (ValueError, RuntimeError, PermissionError) as exc:
            return self._json_error(str(exc))
        return json.dumps(
            {
                "ok": True,
                "room_id": room.room_id,
                "room_url": url,
                "difficulty": room.difficulty,
                "admin_room": room.admin_room,
                "instruction": "最终回复必须完整保留 room_url；说明玩家进入后可选择先后手。",
            },
            ensure_ascii=False,
        )

    @filter.llm_tool(name="game_companion_control_room")
    async def control_room_tool(self, event: AstrMessageEvent, **kwargs: Any) -> str:
        """处理用户在真实 QQ 会话中提出的游戏房间操作。

        身份确认、抢占纠正、悔棋、暂停、继续、认输和结束房间必须使用本工具，不能仅口头答应。
        悔棋是否同意由你结合人格决定，并通过 allow 表达决定。

        Args:
            action(string): status、confirm_player、correct_player、undo、pause、resume、resign、close。
            room_id(string): 可选房间编号；当前会话只有一个房间时可以留空。
            visitor_number(number): correct_player 时创建者声明的浏览器序号。
            allow(boolean): undo 时你是否同意悔棋。
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
                if not bool(kwargs.get("allow")):
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
        except (ValueError, PermissionError) as exc:
            return self._json_error(str(exc))

    @filter.command("游戏伴侣")
    async def game_companion_status(self, event: AstrMessageEvent):
        """Return a small fallback status without taking over ordinary chat."""
        rooms = self.manager.for_session(event.unified_msg_origin)
        if not rooms:
            yield event.plain_result(
                "当前会话没有活动游戏房间。直接告诉我想玩五子棋即可。"
            )
            return
        labels = [f"{room.room_id}：{room.status}" for room in rooms]
        yield event.plain_result("当前游戏房间：\n" + "\n".join(labels))

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
            lines.append(
                "房间 {room_id}：状态={status}，创建者QQ={creator}，玩家序号={number}，"
                "已确认身份={confirmed}，棋力={difficulty}，手数={moves}。".format(
                    room_id=room.room_id,
                    status=room.status,
                    creator=room.creator_qq,
                    number=player.number if player else "无",
                    confirmed=room.player_identity_confirmed,
                    difficulty=room.difficulty,
                    moves=len(game.history) if game else 0,
                )
            )
        lines.append(
            "涉及身份、悔棋、暂停、认输或结束时调用 game_companion_control_room；普通闲聊照常回答。"
        )
        lines.append("</game_companion_context>")
        req.system_prompt = (
            str(req.system_prompt or "") + "\n\n" + "\n".join(lines)
        ).strip()

    async def _create_room_from_event(
        self, event: AstrMessageEvent, difficulty: Difficulty
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
            difficulty=difficulty,
        )
        return room

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
        if event_name == "game_started":
            if room.player_identity_confirmed:
                self._notify_companion_activity(room, "updated")
            self._spawn(
                self._comment(
                    room,
                    "新的一局五子棋刚刚开始，请用当前人格简短说一句开场话。",
                    history_event="[游戏事件] 新的一局五子棋开始。",
                )
            )
            return
        if event_name == "player_confirmed":
            self._notify_companion_activity(room, "started")
            return
        if event_name == "board_changed" and room.game is not None:
            tactical = room.game.tactical_state(
                room.game.human_color
                if payload.get("actor") == "human"
                else room.game.bot_color
            )
            if (
                tactical in {"four"}
                and time.time() - room.last_commentary_at >= self.commentary_cooldown
            ):
                room.last_commentary_at = time.time()
                self._spawn(
                    self._comment(
                        room,
                        "棋盘刚出现明显的四子威胁，请结合当前人格简短自然地回应。",
                        history_event="[游戏事件] 棋盘上出现了明显的四子威胁。",
                    )
                )
            return
        if event_name == "game_finished":
            result = {
                "human_win": "玩家获胜",
                "bot_win": "Bot 获胜",
                "draw": "平局",
            }.get(str(payload.get("result")), "对局结束")
            self._spawn(
                self._comment(
                    room,
                    f"五子棋本局结果是：{result}。请用当前人格简短回应。",
                    history_event=f"[游戏事件] 五子棋本局结果：{result}。",
                )
            )
            return
        if event_name == "rematch_requested":
            self._spawn(self._decide_rematch(room))
            return
        if event_name == "room_destroyed":
            self._notify_companion_activity(room, "ended")
            await self._record_room_memory(room)
            if not self.manager.rooms:
                self._spawn(self._stop_idle_access())

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
            "如果接受，可以根据人格和此前胜负重新选择本局棋力。",
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
        room.difficulty = difficulty
        await self.manager.resolve_rematch(room, accepted=accept, message=reply)
        await self._sync_conversation_pair(room, "[游戏事件] 玩家申请再来一局。", reply)
        await self._send_to_origin(room, reply)

    async def _generate_persona_text(self, room: GameRoom, prompt: str) -> str:
        provider = self.context.get_using_provider(room.session_id)
        if provider is None or not callable(getattr(provider, "text_chat", None)):
            return ""
        persona = await self._persona_prompt()
        memory = await self._memory_context(room, prompt)
        companion_scene = self._companion_scene_prompt(room)
        system_prompt = (
            f"{persona}\n\n{companion_scene}\n\n{memory}\n\n"
            "你正在与用户通过游戏伴侣 WebUI 下五子棋。保持原有人格和关系语气，"
            "只回应当前游戏事件，不输出坐标、规则说明或格式标签。"
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
        if (
            not self.record_shared_experience
            or not room.player_identity_confirmed
            or room.completed_games < 1
        ):
            return
        bridge = self._memory_bridge()
        recorder = getattr(bridge, "record_shared_experience", None) if bridge else None
        if not callable(recorder):
            return
        summary = (
            f"Bot 与用户完成了 {room.completed_games} 局五子棋："
            f"用户胜 {room.human_wins} 局，Bot 胜 {room.bot_wins} 局，平局 {room.draws} 局。"
        )
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
                    "game": "gomoku",
                    "room_id": room.room_id,
                    "difficulty": room.difficulty,
                    "completed_games": room.completed_games,
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
                    label="正在和用户下五子棋",
                    source_plugin=PLUGIN_NAME,
                    ttl_seconds=max(60, self.manager.idle_timeout or 300),
                    metadata={"room_id": room.room_id, "game": "gomoku"},
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
        except asyncio.CancelledError:
            raise

    async def _stop_idle_access(self) -> None:
        await asyncio.sleep(0)
        if self.manager.rooms:
            return
        await self.quick_tunnel.stop()
        await self.room_server.stop()

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
            elif action == "close":
                await self.manager.destroy(room.room_id, "管理员关闭了房间")
            else:
                raise ValueError("不支持的管理操作")
        except (ValueError, PermissionError) as exc:
            return {"status": "error", "message": str(exc), "data": {}}
        return {"status": "ok", "data": {"room_id": room.room_id, "action": action}}

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

    def _spawn(self, operation: Any) -> None:
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
