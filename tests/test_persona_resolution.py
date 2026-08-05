from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot_plugin_game_companion.main import GameCompanionPlugin
from astrbot_plugin_game_companion.models import GameRoom


def make_room() -> GameRoom:
    return GameRoom(
        room_id="room-1",
        access_token="access-token",
        source="private",
        session_id="aiocqhttp:private:10001",
        platform="adapter-instance-id",
        group_id="",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type="gomoku",
        difficulty="normal",
    )


@pytest.mark.asyncio
async def test_persona_uses_same_session_resolution_inputs_as_normal_chat() -> None:
    room = make_room()
    resolver = AsyncMock(
        return_value=(
            "conversation-persona",
            {"prompt": "当前会话人格"},
            None,
            False,
        )
    )
    default_getter = Mock(return_value={"prompt": "错误的全局默认人格"})
    config_getter = Mock(
        return_value={"provider_settings": {"default_personality": "routed-default"}}
    )
    persona_manager = SimpleNamespace(
        resolve_selected_persona=resolver,
        get_default_persona_v3=default_getter,
        acm=SimpleNamespace(get_conf=config_getter),
    )
    conversation_manager = SimpleNamespace(
        get_curr_conversation_id=AsyncMock(return_value="conversation-id"),
        get_conversation=AsyncMock(
            return_value=SimpleNamespace(persona_id="conversation-persona")
        ),
    )
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.context = SimpleNamespace(
        persona_manager=persona_manager,
        conversation_manager=conversation_manager,
    )

    prompt = await plugin._persona_prompt(room)

    assert prompt == "当前会话人格"
    resolver.assert_awaited_once_with(
        umo=room.session_id,
        conversation_persona_id="conversation-persona",
        platform_name="aiocqhttp",
        provider_settings={"default_personality": "routed-default"},
    )
    config_getter.assert_called_once_with(room.session_id)
    default_getter.assert_not_called()


@pytest.mark.asyncio
async def test_persona_resolver_respects_explicit_none_without_default_fallback() -> None:
    room = make_room()
    resolver = AsyncMock(return_value=("[%None]", None, None, False))
    default_getter = Mock(return_value={"prompt": "不应使用的人格"})
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.context = SimpleNamespace(
        persona_manager=SimpleNamespace(
            resolve_selected_persona=resolver,
            get_default_persona_v3=default_getter,
            acm=SimpleNamespace(get_conf=lambda _umo: {}),
        ),
        conversation_manager=SimpleNamespace(
            get_curr_conversation_id=lambda _umo: "conversation-id",
            get_conversation=lambda _umo, _cid: {"persona_id": "[%None]"},
        ),
    )

    assert await plugin._persona_prompt(room) == ""
    default_getter.assert_not_called()


@pytest.mark.asyncio
async def test_persona_fallback_passes_umo_to_older_astrbot_getter() -> None:
    room = make_room()
    default_getter = AsyncMock(return_value={"prompt": "会话路由默认人格"})
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.context = SimpleNamespace(
        persona_manager=SimpleNamespace(get_default_persona_v3=default_getter),
        conversation_manager=None,
    )

    assert await plugin._persona_prompt(room) == "会话路由默认人格"
    default_getter.assert_awaited_once_with(room.session_id)


@pytest.mark.asyncio
async def test_persona_fallback_supports_legacy_no_argument_getter() -> None:
    room = make_room()
    calls = 0

    def legacy_getter() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"system_prompt": "旧版默认人格"}

    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.context = SimpleNamespace(
        persona_manager=SimpleNamespace(get_default_persona_v3=legacy_getter),
        conversation_manager=None,
    )

    assert await plugin._persona_prompt(room) == "旧版默认人格"
    assert calls == 1
