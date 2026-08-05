from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_game_companion.main import GameCompanionPlugin
from astrbot_plugin_game_companion.room_manager import RoomManager


async def make_bound_room():
    manager = RoomManager(max_private_rooms=2)
    room = await manager.create_room(
        source="private",
        session_id="aiocqhttp:private:10001",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type="gomoku",
        difficulty="normal",
    )
    player = await manager.join(room)
    spectator = await manager.join(room)
    for visitor, qq, name in (
        (player, "10001", "玩家"),
        (spectator, "10002", "观众"),
    ):
        token = room.public_snapshot(visitor.token)["identity_token"]
        await manager.bind_visitor_identity(
            session_id=room.session_id,
            identity_token=str(token),
            qq=qq,
            display_name=name,
        )
    await manager.claim_and_start(room, player.token, "human_black")
    return manager, room, player, spectator


@pytest.mark.asyncio
async def test_game_result_is_reported_only_for_actual_bound_players() -> None:
    manager, room, player, _spectator = await make_bound_room()
    room.current_score.completed = 1
    room.current_score.human_wins = 1
    recorder = AsyncMock(return_value={"ok": True})
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.manager = manager
    plugin.companion_afterglow_enabled = True
    plugin._private_companion_api = lambda: SimpleNamespace(  # type: ignore[method-assign]
        record_game_event=recorder
    )
    plugin._capture_round_participants(room, reset=True)
    room.chat_transcripts[player.qq] = [
        {"role": "user", "content": "刚才我有点走神"},
        {"role": "bot", "content": "那也算这一局。"},
    ]

    await plugin._report_companion_game_event(
        room,
        "round_finished",
        {"result": "human_win"},
    )

    recorder.assert_awaited_once()
    payload = recorder.await_args.args[0]
    assert payload["user_id"] == player.qq
    assert payload["bot_result"] == "bot_loss"
    assert payload["round_number"] == 1
    assert payload["event_id"].endswith(":round_finished:10001")
    assert "刚才我有点走神" in payload["recent_context"]
    assert "10002" not in payload["recent_context"]


def test_proactive_invite_respects_room_capacity_and_current_players() -> None:
    manager = RoomManager(max_private_rooms=1)
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.manager = manager
    plugin.companion_invites_enabled = True
    plugin.server_enabled = True
    plugin.private_rooms_enabled = True

    assert plugin._companion_invite_available({"user": {"user_id": "10001"}})

    manager.rooms["occupied"] = SimpleNamespace(
        source="private",
        creator_qq="20001",
        player_qq="20001",
        multiplayer=SimpleNamespace(enabled=False),
        player=None,
    )
    assert not plugin._companion_invite_available({"user": {"user_id": "10001"}})


def test_proactive_invite_tolerates_malformed_afterglow_numbers() -> None:
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.manager = RoomManager(max_private_rooms=1)
    plugin.companion_invites_enabled = True
    plugin.server_enabled = True
    plugin.private_rooms_enabled = True
    context = {
        "user": {
            "user_id": "10001",
            "game_afterglow": {
                "expires_at": "not-a-number",
                "invite_interest": "also-not-a-number",
                "game_label": "五子棋",
                "tone": "还想再赢回来",
            },
        }
    }

    assert plugin._companion_invite_available(context)
    result = plugin._execute_companion_invite(context)
    assert result["ok"] is True
    assert "五子棋、中国象棋" in result["context"]


def test_proactive_invite_registration_uses_companion_ability_api() -> None:
    captured = {}
    api = SimpleNamespace(
        register_proactive_ability=lambda spec: captured.update(spec) or True,
        unregister_proactive_ability=lambda _name: True,
    )
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.companion_invites_enabled = True
    plugin.companion_invite_probability = 0.18
    plugin.companion_invite_cooldown_hours = 24
    plugin._companion_invite_api = None
    plugin._next_companion_registration_at = 0.0
    plugin._private_companion_api = lambda: api  # type: ignore[method-assign]

    assert plugin._register_companion_invite_ability()
    assert captured["name"] == "game_companion_invite"
    assert captured["availability"] == plugin._companion_invite_available
    assert captured["executor"] == plugin._execute_companion_invite
    assert captured["default_enabled"] is True
