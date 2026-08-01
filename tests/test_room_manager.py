from __future__ import annotations

import asyncio

import pytest

from astrbot_plugin_game_companion.room_manager import RoomManager


async def create_room(
    manager: RoomManager, *, source: str = "group", creator: str = "10001"
):
    return await manager.create_room(
        source=source,
        session_id=f"aiocqhttp:{source}:session",
        platform="aiocqhttp",
        group_id="20001" if source == "group" else "",
        creator_qq=creator,
        creator_name="创建者",
        admin_room=False,
        difficulty="normal",
    )


@pytest.mark.asyncio
async def test_source_wide_quota_is_atomic() -> None:
    manager = RoomManager(max_group_rooms=1, max_private_rooms=1)

    results = await asyncio.gather(
        create_room(manager, creator="10001"),
        create_room(manager, creator="10002"),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert len(manager.rooms) == 1


@pytest.mark.asyncio
async def test_zero_quota_means_unlimited() -> None:
    manager = RoomManager(max_group_rooms=0)

    for index in range(6):
        await create_room(manager, creator=str(10000 + index))

    assert len(manager.rooms) == 6


@pytest.mark.asyncio
async def test_visitors_receive_stable_non_reused_numbers() -> None:
    manager = RoomManager()
    room = await create_room(manager)
    first = await manager.join(room)
    second = await manager.join(room)

    resumed = await manager.join(room, first.token)
    await manager.kick_visitor(room, second.number)
    third = await manager.join(room)

    assert resumed.number == 1
    assert third.number == 3


@pytest.mark.asyncio
async def test_admin_room_requires_dashboard_assignment() -> None:
    manager = RoomManager()
    room = await create_room(manager)
    room.admin_room = True
    visitor = await manager.join(room)

    with pytest.raises(ValueError, match="管理员"):
        await manager.claim_and_start(room, visitor.token, "human_black")

    await manager.assign_player(room, visitor.number, "12345678")

    assert room.player_token == visitor.token
    assert room.player_qq == "12345678"
    assert room.player_identity_confirmed
    assert room.status == "setup"


@pytest.mark.asyncio
async def test_creator_correction_swaps_seat_and_resets_game() -> None:
    manager = RoomManager()
    room = await create_room(manager, creator="10001")
    thief = await manager.join(room)
    creator = await manager.join(room)
    await manager.claim_and_start(room, thief.token, "human_black")
    assert room.game is not None

    await manager.correct_creator(room, "10001", creator.number)

    assert room.player_token == creator.token
    assert room.player_qq == "10001"
    assert room.player_identity_confirmed
    assert room.player_seat_locked
    assert room.game is None
    assert room.status == "setup"
    assert thief.token in room.visitors


@pytest.mark.asyncio
async def test_identity_confirmation_emits_only_after_real_qq_confirmation() -> None:
    events: list[str] = []

    async def callback(event: str, _room, _payload) -> None:
        events.append(event)

    manager = RoomManager(event_callback=callback)
    room = await create_room(manager, creator="10001")
    visitor = await manager.join(room)
    await manager.claim_and_start(room, visitor.token, "human_black")

    assert "player_confirmed" not in events

    await manager.confirm_creator(room, "10001")

    assert events[-1] == "player_confirmed"


@pytest.mark.asyncio
async def test_non_creator_cannot_correct_identity() -> None:
    manager = RoomManager()
    room = await create_room(manager, creator="10001")
    visitor = await manager.join(room)

    with pytest.raises(PermissionError):
        await manager.correct_creator(room, "99999", visitor.number)


@pytest.mark.asyncio
async def test_heartbeat_does_not_refresh_meaningful_activity() -> None:
    manager = RoomManager(empty_player_timeout=0, idle_timeout=10)
    room = await create_room(manager)
    visitor = await manager.join(room)
    room.last_activity_at = 100.0

    await manager.heartbeat(room, visitor.token)

    assert room.last_activity_at == 100.0


@pytest.mark.asyncio
async def test_empty_and_idle_zero_disable_expiry() -> None:
    manager = RoomManager(empty_player_timeout=0, idle_timeout=0)
    room = await create_room(manager)
    room.created_at = room.last_activity_at = 1.0
    room.player_empty_since = 1.0

    expired = await manager.sweep_expired(now=10000.0)

    assert expired == []
    assert room.room_id in manager.rooms


@pytest.mark.asyncio
async def test_empty_room_expires_and_releases_quota() -> None:
    manager = RoomManager(max_group_rooms=1, empty_player_timeout=60, idle_timeout=0)
    room = await create_room(manager)
    room.player_empty_since = 10.0

    assert await manager.sweep_expired(now=70.0) == [room.room_id]
    assert not manager.rooms
    await create_room(manager, creator="10002")


@pytest.mark.asyncio
async def test_admin_snapshot_omits_board_and_access_tokens() -> None:
    manager = RoomManager()
    room = await create_room(manager)
    visitor = await manager.join(room)
    snapshot = room.admin_snapshot()

    assert "game" not in snapshot
    assert "access_token" not in snapshot
    assert "token" not in snapshot["visitors"][0]
    assert visitor.number == snapshot["visitors"][0]["number"]
