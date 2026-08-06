from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from astrbot_plugin_game_companion.gomoku import BLACK, GomokuGame
from astrbot_plugin_game_companion.main import GameCompanionPlugin
from astrbot_plugin_game_companion.models import GameRoom, PlayerSeat
from astrbot_plugin_game_companion.pig_dice import PigDiceGame
from astrbot_plugin_game_companion.turtle_soup import SoupPuzzle, TurtleSoupGame


def make_room(*, game_type: str = "pig_dice", source: str = "private") -> GameRoom:
    return GameRoom(
        room_id="room-1",
        access_token="access-token",
        source=source,  # type: ignore[arg-type]
        session_id="aiocqhttp:private:10001",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type=game_type,  # type: ignore[arg-type]
        difficulty="normal",
    )


@pytest.mark.asyncio
async def test_group_qq_chat_isolated_from_active_webui_room() -> None:
    room = make_room(source="group")
    room.game = PigDiceGame(
        turn="bot", human_score=21, bot_score=34, turn_total=8, last_roll=5
    )
    room.add_message("bot", "第一句较早的快速回复")
    room.add_message("bot", "我这回合还想继续冒险。")
    room.add_message("bot", "看来我现在暂时领先。")
    room.messages.append(
        {
            "role": "bot",
            "content": "另一种游戏的旧回复",
            "at": 0,
            "game_type": "gomoku",
        }
    )
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.manager = SimpleNamespace(for_session=lambda _session: [room])
    plugin.private_qq_game_context_enabled = True
    plugin._recent_private_game_results = {}
    event = SimpleNamespace(
        unified_msg_origin=room.session_id, get_sender_id=lambda: "10001"
    )
    request = SimpleNamespace(system_prompt="原始系统提示")

    await plugin.inject_game_context(event, request)

    assert request.system_prompt == "原始系统提示"


@pytest.mark.asyncio
async def test_private_qq_receives_read_only_game_facts_without_webui_dialogue() -> None:
    room = make_room()
    room.status = "active"
    room.player_token = "visitor-token"
    room.player_qq = "10001"
    room.player_identity_confirmed = True
    room.game = PigDiceGame(
        turn="bot", human_score=21, bot_score=34, turn_total=8, last_roll=5
    )
    room.add_message("user", "不应进入 QQ 上下文的 WebUI 发言")
    room.add_message("bot", "不应进入 QQ 上下文的快速回复")
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.manager = SimpleNamespace(for_session=lambda _session: [room])
    plugin.private_qq_game_context_enabled = True
    plugin._recent_private_game_results = {}
    event = SimpleNamespace(
        unified_msg_origin=room.session_id, get_sender_id=lambda: "10001"
    )
    request = SimpleNamespace(system_prompt="原始系统提示")

    await plugin.inject_game_context(event, request)

    assert "<game_companion_private_context>" in request.system_prompt
    assert "游戏=贪心骰子，状态=进行中" in request.system_prompt
    assert "玩家 21 分，Bot 34 分" in request.system_prompt
    assert "当前轮到Bot" in request.system_prompt
    assert "所有游戏操作仍只能在 WebUI 完成" in request.system_prompt
    assert "不应进入 QQ 上下文" not in request.system_prompt


@pytest.mark.asyncio
async def test_private_qq_context_rejects_a_different_bound_player() -> None:
    room = make_room()
    room.player_token = "visitor-token"
    room.player_qq = "10002"
    room.player_identity_confirmed = True
    room.game = PigDiceGame(turn="human")
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.manager = SimpleNamespace(for_session=lambda _session: [room])
    plugin.private_qq_game_context_enabled = True
    plugin._recent_private_game_results = {}
    event = SimpleNamespace(
        unified_msg_origin=room.session_id, get_sender_id=lambda: "10001"
    )
    request = SimpleNamespace(system_prompt="原始系统提示")

    await plugin.inject_game_context(event, request)

    assert request.system_prompt == "原始系统提示"


@pytest.mark.asyncio
async def test_private_qq_context_does_not_expose_multiplayer_room() -> None:
    room = make_room(game_type="turtle_soup")
    room.multiplayer.enabled = True
    room.multiplayer.seats = [
        PlayerSeat(
            visitor_token="first", qq="10001", display_name="创建者", identity_confirmed=True
        ),
        PlayerSeat(
            visitor_token="second", qq="10002", display_name="其他玩家", identity_confirmed=True
        ),
    ]
    room.game = TurtleSoupGame(
        difficulty="normal", max_hints=3, content_level="normal"
    )
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.manager = SimpleNamespace(for_session=lambda _session: [room])
    plugin.private_qq_game_context_enabled = True
    plugin._recent_private_game_results = {}
    event = SimpleNamespace(
        unified_msg_origin=room.session_id, get_sender_id=lambda: "10001"
    )
    request = SimpleNamespace(system_prompt="原始系统提示")

    await plugin.inject_game_context(event, request)

    assert request.system_prompt == "原始系统提示"


@pytest.mark.asyncio
async def test_turtle_soup_live_context_never_exposes_hidden_solution() -> None:
    room = make_room(game_type="turtle_soup")
    puzzle = SoupPuzzle(
        title="测试汤面",
        surface="一个人看见灯灭了，松了一口气。",
        solution="这是绝不能进入普通聊天上下文的隐藏汤底。",
        key_facts=("事实一", "事实二"),
        acceptable_variants=(),
        hints=("提示一",),
        content_level="normal",
        theme="测试",
        trick="测试诡计",
    )
    game = TurtleSoupGame(difficulty="normal", max_hints=3, content_level="normal")
    game.set_puzzle(puzzle)
    game.discovered_facts.add(0)
    room.game = game
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.manager = SimpleNamespace(for_session=lambda _session: [room])
    plugin.private_qq_game_context_enabled = True
    plugin._recent_private_game_results = {}
    event = SimpleNamespace(
        unified_msg_origin=room.session_id, get_sender_id=lambda: "10001"
    )
    request = SimpleNamespace(system_prompt="")

    await plugin.inject_game_context(event, request)

    assert "游戏=海龟汤" in request.system_prompt
    assert "玩法=Bot 出题、玩家猜" in request.system_prompt
    assert "这是绝不能进入普通聊天上下文的隐藏汤底" not in request.system_prompt
    assert "事实一" not in request.system_prompt
    assert "事实二" not in request.system_prompt
    assert "一个人看见灯灭了" not in request.system_prompt


@pytest.mark.asyncio
async def test_recent_private_result_survives_room_close_then_expires() -> None:
    room = make_room(game_type="gomoku")
    room.player_token = "visitor-token"
    room.player_qq = "10001"
    room.player_identity_confirmed = True
    room.game = GomokuGame(human_color=BLACK)
    room.human_wins = 1
    room.completed_games = 1
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.private_qq_game_context_enabled = True
    plugin.recent_game_result_ttl_seconds = 1800
    plugin._recent_private_game_results = {}
    plugin.manager = SimpleNamespace(for_session=lambda _session: [])
    event = SimpleNamespace(
        unified_msg_origin=room.session_id, get_sender_id=lambda: "10001"
    )

    plugin._remember_private_game_result(room, {"result": "human_win"})
    with patch("astrbot_plugin_game_companion.main.time.time", return_value=100.0):
        plugin._finalize_private_game_result(room)
    active_request = SimpleNamespace(system_prompt="")
    with patch("astrbot_plugin_game_companion.main.time.time", return_value=1899.0):
        await plugin.inject_game_context(event, active_request)

    assert "最近一局结果：五子棋，玩家获胜" in active_request.system_prompt
    assert "玩家执黑、Bot 执白" in active_request.system_prompt

    expired_request = SimpleNamespace(system_prompt="")
    with patch("astrbot_plugin_game_companion.main.time.time", return_value=1900.0):
        await plugin.inject_game_context(event, expired_request)

    assert expired_request.system_prompt == ""
    assert room.session_id not in plugin._recent_private_game_results
