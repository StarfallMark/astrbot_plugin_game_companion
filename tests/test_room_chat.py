from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_game_companion.main import GameCompanionPlugin
from astrbot_plugin_game_companion.room_manager import RoomManager


async def make_room(manager: RoomManager):
    return await manager.create_room(
        source="group",
        session_id="aiocqhttp:group:20001",
        platform="aiocqhttp",
        group_id="20001",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type="gomoku",
        difficulty="normal",
    )


async def bind_visitor(
    manager: RoomManager, room, visitor, *, qq: str, name: str
) -> None:
    identity_token = room.public_snapshot(visitor.token)["identity_token"]
    await manager.bind_visitor_identity(
        session_id=room.session_id,
        identity_token=str(identity_token),
        qq=qq,
        display_name=name,
    )


@pytest.mark.asyncio
async def test_room_chat_distinguishes_anonymous_and_bound_spectators() -> None:
    manager = RoomManager()
    room = await make_room(manager)
    anonymous = await manager.join(room)
    bound = await manager.join(room)
    await bind_visitor(manager, room, bound, qq="10002", name="小明")

    _, _, anonymous_player, _ = await manager.begin_room_chat(
        room, anonymous.token, "匿名消息"
    )
    _, _, bound_player, _ = await manager.begin_room_chat(
        room, bound.token, "绑定消息"
    )

    assert not anonymous_player
    assert not bound_player
    assert room.messages[-2]["sender_name"] == "匿名观众"
    assert room.messages[-2]["sender_number"] == anonymous.number
    assert room.messages[-1]["sender_name"] == "小明"
    assert room.messages[-1]["sender_number"] == bound.number
    assert "10001" not in room.chat_transcripts
    assert room.chat_transcripts["10002"] == [
        {"role": "user", "content": "绑定消息"}
    ]
    public = json.dumps(room.public_snapshot(anonymous.token), ensure_ascii=False)
    assert "10002" not in public
    assert "chat_transcripts" not in public


@pytest.mark.asyncio
async def test_spectator_game_command_is_denied_and_never_changes_room() -> None:
    manager = RoomManager()
    room = await make_room(manager)
    spectator = await manager.join(room)
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.manager = manager

    result = await plugin.submit_room_chat(
        room, "我投降", visitor_token=spectator.token
    )

    assert result["action"] == "denied"
    assert "观众席" in result["reply"]
    assert room.status == "waiting"
    assert room.messages[-1]["message_type"] == "permission"

    bound = await manager.join(room)
    await bind_visitor(manager, room, bound, qq="10002", name="小明")
    bound_result = await plugin.submit_room_chat(
        room, "换成井字棋", visitor_token=bound.token
    )

    assert bound_result["action"] == "denied"
    assert room.game_type == "gomoku"
    assert [entry["role"] for entry in room.chat_transcripts["10002"]] == [
        "user",
        "bot",
    ]


@pytest.mark.asyncio
async def test_anonymous_spectator_can_receive_normal_room_chat_reply() -> None:
    manager = RoomManager()
    room = await make_room(manager)
    spectator = await manager.join(room)
    provider = SimpleNamespace(
        text_chat=AsyncMock(
            return_value=SimpleNamespace(completion_text="我在房间里听着呢。")
        )
    )
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.manager = manager
    plugin.context = SimpleNamespace(
        persona_manager=None,
        get_using_provider=lambda _session_id: provider,
    )

    result = await plugin.submit_room_chat(
        room, "你在做什么？", visitor_token=spectator.token
    )

    assert result == {"action": "chat", "reply": "我在房间里听着呢。"}
    prompt = provider.text_chat.await_args.kwargs["prompt"]
    assert "身份=匿名观众" in prompt
    assert "是否当前回合玩家=否" in prompt
    assert room.messages[-1]["role"] == "bot"
    assert room.chat_transcripts == {}


@pytest.mark.asyncio
async def test_bound_spectator_chat_is_written_only_to_its_own_memory() -> None:
    manager = RoomManager()
    room = await make_room(manager)
    first = await manager.join(room)
    second = await manager.join(room)
    anonymous = await manager.join(room)
    await bind_visitor(manager, room, first, qq="10002", name="小明")
    await bind_visitor(manager, room, second, qq="10003", name="小红")
    await manager.begin_room_chat(room, first.token, "只属于小明的内容")
    await manager.add_room_chat_reply(room, first, "给小明的回复")
    await manager.begin_room_chat(room, second.token, "只属于小红的内容")
    await manager.add_room_chat_reply(room, second, "给小红的回复")
    await manager.begin_room_chat(room, anonymous.token, "匿名内容")

    recorder = AsyncMock()
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.record_shared_experience = True
    plugin._memory_bridge = lambda: SimpleNamespace(  # type: ignore[method-assign]
        record_shared_experience=recorder
    )

    await plugin._record_room_memory(room)

    assert recorder.await_count == 2
    records = {
        call.kwargs["user_id"]: call.kwargs["content"]
        for call in recorder.await_args_list
    }
    assert "只属于小明的内容" in records["10002"]
    assert "只属于小红的内容" not in records["10002"]
    assert "只属于小红的内容" in records["10003"]
    assert "匿名内容" not in records["10002"]
    assert "匿名内容" not in records["10003"]


@pytest.mark.asyncio
async def test_player_can_switch_game_from_room_chat() -> None:
    manager = RoomManager()
    room = await make_room(manager)
    player = await manager.join(room)
    await bind_visitor(manager, room, player, qq="10002", name="玩家")
    await manager.claim_and_start(room, player.token, "human_black")
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.manager = manager

    result = await plugin.submit_room_chat(
        room, "换成井字棋吧", visitor_token=player.token
    )

    assert result["action"] == "switch_game"
    assert room.game_type == "tictactoe"
    assert room.status == "setup"
    assert "井字棋" in result["reply"]
