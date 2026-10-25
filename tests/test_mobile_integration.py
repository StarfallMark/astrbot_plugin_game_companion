from __future__ import annotations

import types

import pytest
from astrbot_plugin_game_companion.main import GameCompanionPlugin
from astrbot_plugin_game_companion.room_manager import RoomManager


@pytest.mark.asyncio
async def test_mobile_room_uses_lan_server_and_prebinds_phone_user() -> None:
    plugin = object.__new__(GameCompanionPlugin)
    plugin.server_enabled = True
    plugin.private_rooms_enabled = True
    plugin.server_host = "0.0.0.0"
    plugin.auto_quick_tunnel = False
    plugin.public_base_url = ""
    plugin.manager = RoomManager(max_private_rooms=1)
    plugin.room_server = types.SimpleNamespace(
        running=True,
        local_base_url="http://127.0.0.1:6331",
    )
    plugin.quick_tunnel = types.SimpleNamespace(running=False, ready=False, url="")

    result = await plugin.mobile_create_room("mobile-owner", "gomoku")

    assert result["game_type"] == "gomoku"
    assert result["url"].startswith("http://")
    assert "127.0.0.1:6331/room/" not in result["url"]
    assert "visitor_token=" in result["url"]
    room = plugin.manager.rooms[result["room_id"]]
    assert room.player_qq == "mobile-owner"
    assert room.player_identity_confirmed is True
    assert room.status == "active"


@pytest.mark.asyncio
async def test_mobile_room_switches_the_phone_users_room_to_the_selected_game() -> None:
    plugin = object.__new__(GameCompanionPlugin)
    plugin.server_enabled = True
    plugin.private_rooms_enabled = True
    plugin.server_host = "0.0.0.0"
    plugin.auto_quick_tunnel = False
    plugin.public_base_url = "https://games.example.com"
    plugin.manager = RoomManager(max_private_rooms=1)
    plugin.room_server = types.SimpleNamespace(running=True, local_base_url="http://127.0.0.1:6331")
    plugin.quick_tunnel = types.SimpleNamespace(running=False, ready=False, url="")

    first = await plugin.mobile_create_room("mobile-owner", "tictactoe")
    second = await plugin.mobile_create_room("mobile-owner", "gomoku")

    assert second["room_id"] == first["room_id"]
    assert second["game_type"] == "gomoku"
    assert second["reused_room"] is True
    assert second["switched_game"] is True
    room = plugin.manager.rooms[second["room_id"]]
    assert room.status == "active"


@pytest.mark.asyncio
async def test_mobile_room_reuses_the_same_selected_game_without_resetting_it() -> None:
    plugin = object.__new__(GameCompanionPlugin)
    plugin.server_enabled = True
    plugin.private_rooms_enabled = True
    plugin.server_host = "0.0.0.0"
    plugin.auto_quick_tunnel = False
    plugin.public_base_url = "https://games.example.com"
    plugin.manager = RoomManager(max_private_rooms=1)
    plugin.room_server = types.SimpleNamespace(running=True, local_base_url="http://127.0.0.1:6331")
    plugin.quick_tunnel = types.SimpleNamespace(running=False, url="")

    first = await plugin.mobile_create_room("mobile-owner", "gomoku")
    room = plugin.manager.rooms[first["room_id"]]
    game_before = room.game
    second = await plugin.mobile_create_room("mobile-owner", "gomoku")

    assert second["room_id"] == first["room_id"]
    assert second["game_type"] == "gomoku"
    assert second["reused_room"] is True
    assert second["switched_game"] is False
    assert room.game is game_before


@pytest.mark.asyncio
async def test_mobile_gateway_room_does_not_require_public_tunnel() -> None:
    plugin = object.__new__(GameCompanionPlugin)
    plugin.server_enabled = True
    plugin.private_rooms_enabled = True
    plugin.server_host = "127.0.0.1"
    plugin.auto_quick_tunnel = False
    plugin.public_base_url = ""
    plugin.manager = RoomManager(max_private_rooms=1)
    plugin.room_server = types.SimpleNamespace(
        running=True,
        local_base_url="http://127.0.0.1:6331",
    )
    plugin.quick_tunnel = types.SimpleNamespace(running=False, url="")

    result = await plugin.mobile_create_room(
        "mobile-owner",
        "gomoku",
        via_mobile_gateway=True,
    )

    assert result["url"].startswith("http://127.0.0.1:6331/room/")


def test_room_url_rejects_a_running_but_unhealthy_tunnel() -> None:
    plugin = object.__new__(GameCompanionPlugin)
    plugin.public_base_url = ""
    plugin.quick_tunnel = types.SimpleNamespace(
        running=True,
        ready=False,
        url="https://stale.trycloudflare.com",
    )
    room = types.SimpleNamespace(access_token="room-token")

    with pytest.raises(RuntimeError, match="尚未就绪"):
        plugin._room_url(room)


def test_custom_external_base_url_bypasses_cloudflare() -> None:
    plugin = object.__new__(GameCompanionPlugin)
    plugin.public_base_url = ""
    plugin.external_base_url = "https://frp.example.com/games"
    plugin.quick_tunnel = types.SimpleNamespace(ready=False, url="")
    room = types.SimpleNamespace(access_token="room-token")

    assert plugin._room_url(room) == "https://frp.example.com/games/room/room-token"


def test_mobile_status_allows_a_non_loopback_listener_without_cloudflare() -> None:
    plugin = object.__new__(GameCompanionPlugin)
    plugin.server_enabled = True
    plugin.private_rooms_enabled = True
    plugin.server_host = "0.0.0.0"
    plugin.access_host = "192.168.1.20"
    plugin.public_base_url = ""
    plugin.external_base_url = ""
    plugin.auto_quick_tunnel = False
    plugin.room_server = types.SimpleNamespace(running=False)
    plugin.quick_tunnel = types.SimpleNamespace(ready=False)
    plugin.enabled_games = {
        game: True
        for game in (
            "gomoku",
            "xiangqi",
            "tictactoe",
            "othello",
            "turtle_soup",
            "pig_dice",
            "draw_guess",
            "blackjack",
        )
    }

    status = plugin.mobile_status()

    assert status["ready"] is True
    assert status["blockers"] == []
