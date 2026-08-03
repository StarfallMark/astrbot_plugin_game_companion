from __future__ import annotations

from types import SimpleNamespace

import pytest

from astrbot_plugin_game_companion.main import GameCompanionPlugin
from astrbot_plugin_game_companion.models import GameRoom
from astrbot_plugin_game_companion.pig_dice import PigDiceGame
from astrbot_plugin_game_companion.turtle_soup import SoupPuzzle, TurtleSoupGame


def make_room(*, game_type: str = "pig_dice") -> GameRoom:
    return GameRoom(
        room_id="room-1",
        access_token="access-token",
        source="private",
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
async def test_normal_chat_gets_ephemeral_scores_and_recent_fast_replies() -> None:
    room = make_room()
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
    event = SimpleNamespace(unified_msg_origin=room.session_id)
    request = SimpleNamespace(system_prompt="原始系统提示")

    await plugin.inject_game_context(event, request)

    prompt = request.system_prompt
    assert "原始系统提示" in prompt
    assert "当前请求专用的临时游戏状态" in prompt
    assert "玩家已存 21 分，Bot 已存 34 分" in prompt
    assert "Bot 已存总分领先 13 分" in prompt
    assert "我这回合还想继续冒险。" in prompt
    assert "看来我现在暂时领先。" in prompt
    assert "第一句较早的快速回复" not in prompt
    assert "另一种游戏的旧回复" not in prompt


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
    event = SimpleNamespace(unified_msg_origin=room.session_id)
    request = SimpleNamespace(system_prompt="")

    await plugin.inject_game_context(event, request)

    assert "题目《测试汤面》" in request.system_prompt
    assert "发现公开关键进度 1/2" in request.system_prompt
    assert puzzle.solution not in request.system_prompt
    assert "事实一" not in request.system_prompt
