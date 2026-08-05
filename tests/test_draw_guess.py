from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from astrbot_plugin_game_companion.draw_guess import DrawGuessGame, DrawWord
from astrbot_plugin_game_companion.main import GameCompanionPlugin
from astrbot_plugin_game_companion.room_manager import RoomManager
from astrbot_plugin_game_companion.server import GameRoomServer


async def make_draw_room(*, target: str = "苹果", max_guesses: int = 5):
    manager = RoomManager(
        draw_guess_max_guesses=max_guesses,
        draw_guess_duration_seconds=120,
    )
    room = await manager.create_room(
        source="private",
        session_id="aiocqhttp:private:10001",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type="draw_guess",
        difficulty="normal",
    )
    player = await manager.join(room)
    spectator = await manager.join(room)
    room.player_token = player.token
    room.player_qq = "10001"
    room.player_identity_confirmed = True
    player.qq = "10001"
    player.display_name = "玩家"
    player.identity_confirmed = True
    room.status = "active"
    room.game = DrawGuessGame(
        difficulty="normal",
        max_guesses=max_guesses,
        duration_seconds=120,
        target=DrawWord(target, ("apple",) if target == "苹果" else ()),
    )
    return manager, room, player, spectator


def sample_strokes():
    return [
        {
            "color": "#202522",
            "width": 5,
            "points": [[0.1, 0.2], [0.5, 0.6], [0.9, 0.8]],
        }
    ]


def sample_image_data_url() -> str:
    content = b"\x89PNG\r\n\x1a\n" + b"0" * 300
    return "data:image/png;base64," + base64.b64encode(content).decode("ascii")


@pytest.mark.asyncio
async def test_hidden_answer_is_visible_only_to_drawer_until_finish() -> None:
    _manager, room, player, spectator = await make_draw_room()

    player_game = room.public_snapshot(player.token)["game"]
    spectator_game = room.public_snapshot(spectator.token)["game"]

    assert player_game["answer"] == "苹果"
    assert "answer" not in spectator_game
    room.game.record_guess("苹果", correct=True)
    room.status = "finished"
    assert room.public_snapshot(spectator.token)["game"]["answer"] == "苹果"


@pytest.mark.asyncio
async def test_strokes_are_validated_and_wrong_client_cannot_draw() -> None:
    manager, room, player, spectator = await make_draw_room()

    await manager.update_drawing(room, player.token, sample_strokes())
    assert room.game.revision == 1
    assert room.game.strokes[0]["points"][1] == [0.5, 0.6]

    with pytest.raises(PermissionError, match="玩家席"):
        await manager.update_drawing(room, spectator.token, sample_strokes())
    with pytest.raises(ValueError, match="坐标"):
        await manager.update_drawing(
            room,
            player.token,
            [{"color": "#202522", "width": 5, "points": [[1.2, 0.3]]}],
        )


@pytest.mark.asyncio
async def test_visual_guess_finishes_cooperative_round_without_answer_in_prompt() -> None:
    manager, room, player, _spectator = await make_draw_room()
    await manager.update_drawing(room, player.token, sample_strokes())
    provider = SimpleNamespace(
        text_chat=AsyncMock(return_value=SimpleNamespace(completion_text="我猜是：苹果"))
    )
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.manager = manager
    plugin.context = SimpleNamespace(get_using_provider=lambda _session_id: provider)
    plugin.draw_guess_vision_provider_id = ""

    result = await plugin.submit_draw_guess(
        room,
        visitor_token=player.token,
        image_data_url=sample_image_data_url(),
    )

    assert result == {"guess": "苹果", "correct": True, "number": 1}
    assert room.status == "finished"
    assert room.human_wins == 1
    assert room.bot_wins == 0
    prompt = provider.text_chat.await_args.kwargs["prompt"]
    assert "苹果" not in prompt
    assert provider.text_chat.await_args.kwargs["image_urls"] == [
        sample_image_data_url()
    ]


@pytest.mark.asyncio
async def test_visual_provider_failure_releases_processing_state() -> None:
    manager, room, player, _spectator = await make_draw_room()
    await manager.update_drawing(room, player.token, sample_strokes())
    provider = SimpleNamespace(text_chat=AsyncMock(side_effect=RuntimeError("no vision")))
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.manager = manager
    plugin.context = SimpleNamespace(get_using_provider=lambda _session_id: provider)
    plugin.draw_guess_vision_provider_id = ""

    with pytest.raises(RuntimeError, match="视觉模型"):
        await plugin.submit_draw_guess(
            room,
            visitor_token=player.token,
            image_data_url=sample_image_data_url(),
        )

    assert room.game.processing is False
    assert room.game.guesses == []


@pytest.mark.asyncio
async def test_drawing_http_routes_sync_canvas_and_submit_visual_guess() -> None:
    manager, room, player, _spectator = await make_draw_room()
    provider = SimpleNamespace(
        text_chat=AsyncMock(return_value=SimpleNamespace(completion_text="苹果"))
    )
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.manager = manager
    plugin.context = SimpleNamespace(get_using_provider=lambda _session_id: provider)
    plugin.draw_guess_vision_provider_id = ""
    plugin.public_base_url = ""
    plugin.quick_tunnel = SimpleNamespace(url="", running=False)
    server = GameRoomServer(
        plugin,
        manager,
        host="127.0.0.1",
        port=0,
        web_root=Path(__file__).resolve().parents[1] / "web",
    )

    async with TestClient(TestServer(server._build_app())) as client:
        origin = str(client.make_url("/")).rstrip("/")
        strokes_response = await client.post(
            f"/api/room/{room.access_token}/draw/strokes",
            json={"visitor_token": player.token, "strokes": sample_strokes()},
            headers={"Origin": origin},
        )
        guess_response = await client.post(
            f"/api/room/{room.access_token}/draw/guess",
            json={
                "visitor_token": player.token,
                "image_data_url": sample_image_data_url(),
            },
            headers={"Origin": origin},
        )
        strokes_payload = await strokes_response.json()
        guess_payload = await guess_response.json()

    assert strokes_response.status == 200
    assert strokes_payload["data"]["room"]["game"]["revision"] == 1
    assert guess_response.status == 200
    assert guess_payload["data"]["correct"] is True
    assert guess_payload["data"]["room"]["status"] == "finished"


@pytest.mark.asyncio
async def test_draw_round_times_out_during_room_sweep() -> None:
    manager, room, _player, _spectator = await make_draw_room()
    room.game.started_at = 100

    await manager.sweep_expired(now=221)

    assert room.status == "finished"
    assert room.game.timed_out is True
    assert room.completed_games == 1
    assert room.bot_wins == 1


def test_guess_output_cleaning_and_image_validation() -> None:
    assert GameCompanionPlugin._clean_draw_guess("我猜是：苹果\n因为它是圆的") == "苹果"
    assert GameCompanionPlugin._clean_draw_guess("猫、狗、兔子") == "猫"
    assert GameCompanionPlugin._validated_drawing_image(sample_image_data_url()).startswith(
        "data:image/png;base64,"
    )
    with pytest.raises(ValueError, match="PNG 或 WebP"):
        GameCompanionPlugin._validated_drawing_image("data:image/svg+xml;base64,AAAA")
