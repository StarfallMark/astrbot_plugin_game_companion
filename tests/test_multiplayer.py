from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_game_companion.main import GameCompanionPlugin
from astrbot_plugin_game_companion.room_manager import RoomManager
from astrbot_plugin_game_companion.turtle_soup import (
    TurtleSoupGame,
    puzzle_from_mapping,
)


def make_puzzle():
    return puzzle_from_mapping(
        {
            "title": "测试题",
            "surface": "一个人每天打开同一扇门，却从来不从这扇门进入房间，这是为什么？",
            "solution": "这是温室的通风门。他负责照料植物，真正供人出入的门位于温室另一侧。",
            "key_facts": ["门用于通风", "房间是温室", "入口位于另一侧"],
            "acceptable_variants": [],
            "hints": ["门不只用于通行", "与环境有关", "房内有植物"],
            "theme": "温室",
            "trick": "把通风门当成入口",
        },
        content_level="all_ages",
    )


async def make_room(
    manager: RoomManager,
    *,
    admin_room: bool = False,
    mode: str = "bot_host",
):
    return await manager.create_room(
        source="group",
        session_id="aiocqhttp:group:20001",
        platform="aiocqhttp",
        group_id="20001",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=admin_room,
        game_type="turtle_soup",
        difficulty="normal",
        turtle_soup_mode=mode,
    )


@pytest.mark.asyncio
async def test_turtle_soup_uses_reusable_multiplayer_seats_and_capacity() -> None:
    manager = RoomManager(turtle_soup_max_players=2)
    room = await make_room(manager)
    first = await manager.join(room)
    second = await manager.join(room)
    third = await manager.join(room)

    await manager.claim_and_start(room, first.token, "")
    assert isinstance(room.game, TurtleSoupGame)
    await manager.complete_turtle_soup_generation(room, room.game, make_puzzle())
    await manager.claim_and_start(room, second.token, "")

    assert room.public_snapshot(first.token)["player_numbers"] == [1, 2]
    with pytest.raises(ValueError, match="坐满"):
        await manager.claim_and_start(room, third.token, "")


@pytest.mark.asyncio
async def test_browser_identity_token_binds_qq_and_nickname_before_seating() -> None:
    manager = RoomManager(turtle_soup_max_players=2)
    room = await make_room(manager)
    visitor = await manager.join(room)
    snapshot = room.public_snapshot(visitor.token)
    identity_token = snapshot["identity_token"]
    assert identity_token

    bound_room, bound = await manager.bind_visitor_identity(
        session_id=room.session_id,
        identity_token=str(identity_token),
        qq="10002",
        display_name="群名片小明",
    )
    assert bound_room is room
    assert bound.qq == "10002"
    assert bound.display_name == "群名片小明"
    assert room.public_snapshot(visitor.token)["identity_token"] == ""

    await manager.claim_and_start(room, visitor.token, "")
    public = room.public_snapshot(visitor.token)
    assert public["player_labels"] == ["群名片小明（1号）"]
    assert public["current_player_name"] == "群名片小明"
    assert room.player_qq == "10002"
    assert room.participant_names == {"10002": "群名片小明"}

    with pytest.raises(ValueError, match="无效|使用"):
        await manager.bind_visitor_identity(
            session_id=room.session_id,
            identity_token=str(identity_token),
            qq="10003",
            display_name="另一个人",
        )


@pytest.mark.asyncio
async def test_same_qq_cannot_bind_two_browser_visitors_in_one_room() -> None:
    manager = RoomManager()
    room = await make_room(manager)
    first = await manager.join(room)
    second = await manager.join(room)
    first_token = room.public_snapshot(first.token)["identity_token"]
    second_token = room.public_snapshot(second.token)["identity_token"]
    await manager.bind_visitor_identity(
        session_id=room.session_id,
        identity_token=str(first_token),
        qq="10002",
        display_name="小明",
    )
    with pytest.raises(ValueError, match="其他访客"):
        await manager.bind_visitor_identity(
            session_id=room.session_id,
            identity_token=str(second_token),
            qq="10002",
            display_name="小明",
        )


@pytest.mark.asyncio
async def test_valid_action_rotates_turn_and_timeout_skips_offline_player() -> None:
    manager = RoomManager(turtle_soup_max_players=3, multiplayer_turn_timeout=60)
    room = await make_room(manager)
    players = [await manager.join(room) for _index in range(3)]
    await manager.claim_and_start(room, players[0].token, "")
    assert isinstance(room.game, TurtleSoupGame)
    await manager.complete_turtle_soup_generation(room, room.game, make_puzzle())
    await manager.claim_and_start(room, players[1].token, "")
    await manager.claim_and_start(room, players[2].token, "")

    game, question = await manager.begin_turtle_soup_interaction(
        room,
        "这是温室吗？",
        source="web",
        visitor_token=players[0].token,
        limit=200,
    )
    await manager.resolve_turtle_soup_question(
        room, game, question, "yes", source="web", matched_facts={1}
    )
    assert room.multiplayer.current_token == players[1].token

    players[2].connected = False
    room.multiplayer.turn_deadline = 100
    await manager.sweep_expired(now=101)
    assert room.multiplayer.current_token == players[0].token


@pytest.mark.asyncio
async def test_model_processing_pauses_turn_deadline_and_failure_restores_it() -> None:
    manager = RoomManager(multiplayer_turn_timeout=60, idle_timeout=0)
    room = await make_room(manager)
    player = await manager.join(room)
    await manager.claim_and_start(room, player.token, "")
    assert isinstance(room.game, TurtleSoupGame)
    await manager.complete_turtle_soup_generation(room, room.game, make_puzzle())

    game, _text = await manager.begin_turtle_soup_interaction(
        room,
        "测试问题",
        source="web",
        visitor_token=player.token,
        limit=200,
    )
    assert room.multiplayer.turn_deadline == 0

    await manager.sweep_expired(now=time.time() + 1000)
    assert room.multiplayer.current_token == player.token
    await manager.cancel_turtle_soup_interaction(room, game, "模型失败")
    assert room.multiplayer.turn_deadline > time.time()


@pytest.mark.asyncio
async def test_swap_is_rate_limited_and_never_transfers_bound_qq() -> None:
    manager = RoomManager(swap_request_cooldown=30, swap_request_expiry=20)
    room = await make_room(manager, admin_room=True)
    player = await manager.join(room)
    spectator = await manager.join(room)
    await manager.assign_player(room, player.number, "123456")
    await manager.start_game(room, player.token, "")
    assert isinstance(room.game, TurtleSoupGame)
    await manager.complete_turtle_soup_generation(room, room.game, make_puzzle())

    request_id = await manager.request_seat_swap(
        room, spectator.token, player.number, now=100
    )
    accepted = await manager.resolve_seat_swap(
        room, player.token, request_id, accepted=True, now=105
    )

    assert accepted
    seat = room.multiplayer.seat_for_token(spectator.token)
    assert seat is not None
    assert seat.qq == ""
    assert not seat.identity_confirmed
    assert room.player_qq == ""
    assert room.multiplayer.current_token == spectator.token
    assert room.multiplayer.turn_deadline == 165

    await manager.remove_player(room, spectator.number)
    await manager.assign_player(room, player.number, "123456")
    with pytest.raises(ValueError, match="等待 25 秒"):
        await manager.request_seat_swap(room, spectator.token, player.number, now=105)


@pytest.mark.asyncio
async def test_admin_room_can_assign_multiple_players_but_not_self_claim() -> None:
    manager = RoomManager(turtle_soup_max_players=6)
    room = await make_room(manager, admin_room=True)
    first = await manager.join(room)
    second = await manager.join(room)

    with pytest.raises(ValueError, match="管理员"):
        await manager.claim_and_start(room, first.token, "")
    await manager.assign_player(room, first.number, "10001")
    await manager.assign_player(room, second.number, "10002")

    assert room.public_snapshot(first.token)["player_numbers"] == [1, 2]
    assert room.status == "setup"


@pytest.mark.asyncio
async def test_player_host_mode_uses_public_clues_and_bot_guess_can_finish() -> None:
    provider = SimpleNamespace(
        text_chat=AsyncMock(
            return_value=SimpleNamespace(
                completion_text=json.dumps(
                    {"kind": "guess", "text": "这是温室的通风门。"},
                    ensure_ascii=False,
                )
            )
        )
    )
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.context = SimpleNamespace(
        get_using_provider=lambda _session: provider,
        persona_manager=None,
    )
    plugin.manager = RoomManager()
    room = await make_room(plugin.manager, mode="player_host")
    player = await plugin.manager.join(room)
    await plugin.manager.claim_and_start(room, player.token, "")

    assert isinstance(room.game, TurtleSoupGame)
    assert room.game.phase == "ready"
    assert room.game.puzzle is None
    result = await plugin.submit_reverse_turtle_soup_turn(
        room,
        "有一扇每天都会打开但不供人通行的门。",
        source="web",
        visitor_token=player.token,
    )

    assert result["bot_action"] == "guess"
    prompt = provider.text_chat.await_args.kwargs["prompt"]
    assert "隐藏汤底" not in prompt
    assert "不供人通行" in prompt

    await plugin.manager.confirm_reverse_turtle_soup_guess(
        room, source="web", visitor_token=player.token
    )
    assert room.status == "finished"
    assert room.game.bot_solved
    assert room.scores["turtle_soup"].bot_wins == 1


@pytest.mark.asyncio
async def test_switching_to_single_player_game_keeps_only_primary_seat() -> None:
    manager = RoomManager()
    room = await make_room(manager)
    first = await manager.join(room)
    second = await manager.join(room)
    await manager.claim_and_start(room, first.token, "")
    await manager.claim_and_start(room, second.token, "")

    await manager.switch_game(room, "gomoku", force=True)

    assert not room.multiplayer.enabled
    assert room.player_token == first.token
    assert second.token in room.visitors
    assert room.public_snapshot(second.token)["is_player"] is False


@pytest.mark.asyncio
async def test_finished_multiplayer_room_waits_until_all_players_leave() -> None:
    manager = RoomManager(idle_timeout=0)
    room = await make_room(manager)
    first = await manager.join(room)
    second = await manager.join(room)
    await manager.claim_and_start(room, first.token, "")
    assert isinstance(room.game, TurtleSoupGame)
    await manager.complete_turtle_soup_generation(room, room.game, make_puzzle())
    await manager.claim_and_start(room, second.token, "")
    room.game.give_up()
    await manager._finish_game(room)

    await manager.leave(room, first.token)
    future = time.time() + manager.FINISHED_PLAYER_LEAVE_GRACE_SECONDS + 1
    assert await manager.sweep_expired(now=future) == []

    await manager.leave(room, second.token)
    assert await manager.sweep_expired(now=future) == [room.room_id]
