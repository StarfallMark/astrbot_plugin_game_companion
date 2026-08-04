from __future__ import annotations

import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from astrbot_plugin_game_companion.room_manager import RoomManager
from astrbot_plugin_game_companion.pig_dice import PigDiceGame
from astrbot_plugin_game_companion.server import GameRoomServer


class EndingXiangqiEngine:
    async def ensure_ready(self) -> None:
        return None

    async def legal_moves(self, moves: list[str]) -> list[str]:
        return ["a3a4"] if not moves else []


def make_server(port: int = 6331) -> GameRoomServer:
    plugin = SimpleNamespace(
        public_base_url="",
        quick_tunnel=SimpleNamespace(url="", running=False),
    )
    return GameRoomServer(
        plugin,
        RoomManager(),
        host="127.0.0.1",
        port=port,
        web_root=Path(__file__).resolve().parents[1] / "web",
    )


def test_origin_requires_exact_local_or_public_origin() -> None:
    server = make_server()
    same = SimpleNamespace(
        headers={"Origin": "http://127.0.0.1:6331"},
        scheme="http",
        host="127.0.0.1:6331",
    )
    foreign = SimpleNamespace(
        headers={"Origin": "https://attacker.example"},
        scheme="http",
        host="127.0.0.1:6331",
    )

    assert server._origin_allowed(same)
    assert not server._origin_allowed(foreign)


def test_public_https_origin_is_exact() -> None:
    server = make_server()
    server.plugin.public_base_url = "https://games.example.com/path"
    allowed = SimpleNamespace(
        headers={"Origin": "https://games.example.com"},
        scheme="http",
        host="127.0.0.1:6331",
    )
    wrong_port = SimpleNamespace(
        headers={"Origin": "https://games.example.com:8443"},
        scheme="http",
        host="127.0.0.1:6331",
    )

    assert server._origin_allowed(allowed)
    assert not server._origin_allowed(wrong_port)


@pytest.mark.asyncio
async def test_server_skips_an_occupied_port_without_touching_listener() -> None:
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    occupied_port = blocker.getsockname()[1]
    if occupied_port >= 65535:
        blocker.close()
        pytest.skip("No next TCP port is available")
    server = make_server(occupied_port)
    try:
        await server.start()
        assert server.port > occupied_port
        assert blocker.getsockname()[1] == occupied_port
    finally:
        await server.stop()
        blocker.close()


def test_security_policy_blocks_external_assets_and_framing() -> None:
    headers = make_server()._headers("text/html")

    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["Referrer-Policy"] == "no-referrer"


@pytest.mark.asyncio
async def test_expired_room_api_returns_structured_close_reason() -> None:
    server = make_server(0)
    room = await server.manager.create_room(
        source="private",
        session_id="aiocqhttp:private:10001",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        difficulty="normal",
    )
    access_token = room.access_token
    await server.manager.destroy(room.room_id, "房间测试关闭")

    app = server._build_app()
    async with TestClient(TestServer(app)) as client:
        response = await client.get(f"/api/room/{access_token}/state")
        payload = await response.json()

    assert response.status == 410
    assert payload == {"status": "error", "message": "房间测试关闭"}


@pytest.mark.asyncio
async def test_same_origin_leave_beacon_records_browser_departure() -> None:
    server = make_server(0)
    room = await server.manager.create_room(
        source="private",
        session_id="aiocqhttp:private:10001",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        difficulty="normal",
    )
    visitor = await server.manager.join(room)

    app = server._build_app()
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            f"/api/room/{room.access_token}/leave",
            json={"visitor_token": visitor.token},
            headers={"Origin": str(client.make_url("/")).rstrip("/")},
        )
        payload = await response.json()

    assert response.status == 200
    assert payload == {"status": "ok", "data": {"left": True}}
    assert not visitor.connected
    assert visitor.left_at is not None


@pytest.mark.asyncio
async def test_browser_cannot_claim_player_seat_before_qq_binding() -> None:
    server = make_server(0)
    room = await server.manager.create_room(
        source="private",
        session_id="aiocqhttp:private:10001",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        difficulty="normal",
    )
    visitor = await server.manager.join(room)
    app = server._build_app()
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            f"/api/room/{room.access_token}/claim",
            json={"visitor_token": visitor.token, "side": "human_black"},
            headers={"Origin": str(client.make_url("/")).rstrip("/")},
        )
        payload = await response.json()

        identity_token = room.public_snapshot(visitor.token)["identity_token"]
        await server.manager.bind_visitor_identity(
            session_id=room.session_id,
            identity_token=str(identity_token),
            qq="10002",
            display_name="测试玩家",
        )
        bound_response = await client.post(
            f"/api/room/{room.access_token}/claim",
            json={"visitor_token": visitor.token, "side": "human_black"},
            headers={"Origin": str(client.make_url("/")).rstrip("/")},
        )

    assert response.status == 403
    assert payload["status"] == "error"
    assert "绑定页面令牌" in payload["message"]
    assert bound_response.status == 200
    assert room.player_qq == "10002"


@pytest.mark.asyncio
async def test_xiangqi_move_endpoint_accepts_start_and_end_coordinates() -> None:
    server = make_server(0)
    server.manager.xiangqi_engine = EndingXiangqiEngine()  # type: ignore[assignment]
    room = await server.manager.create_room(
        source="private",
        session_id="aiocqhttp:private:10001",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type="xiangqi",
        difficulty="normal",
    )
    visitor = await server.manager.join(room)
    await server.manager.claim_and_start(room, visitor.token, "human_red")

    app = server._build_app()
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            f"/api/room/{room.access_token}/move",
            json={
                "visitor_token": visitor.token,
                "from_row": 6,
                "from_column": 0,
                "to_row": 5,
                "to_column": 0,
            },
            headers={"Origin": str(client.make_url("/")).rstrip("/")},
        )
        payload = await response.json()

    assert response.status == 200
    assert payload["data"]["room"]["game"]["last_move"] == [6, 0, 5, 0]
    assert payload["data"]["room"]["status"] == "finished"


@pytest.mark.asyncio
async def test_tictactoe_move_endpoint_accepts_cell_coordinates() -> None:
    server = make_server(0)
    room = await server.manager.create_room(
        source="private",
        session_id="aiocqhttp:private:10001",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type="tictactoe",
        difficulty="easy",
    )
    visitor = await server.manager.join(room)
    await server.manager.claim_and_start(room, visitor.token, "human_x")

    app = server._build_app()
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            f"/api/room/{room.access_token}/move",
            json={"visitor_token": visitor.token, "row": 1, "column": 1},
            headers={"Origin": str(client.make_url("/")).rstrip("/")},
        )
        payload = await response.json()

    assert response.status == 200
    game = payload["data"]["room"]["game"]
    assert game["board"][1][1] == 1
    assert game["move_count"] == 2


@pytest.mark.asyncio
async def test_pig_dice_endpoint_never_accepts_a_client_supplied_roll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "astrbot_plugin_game_companion.pig_dice.secrets.randbelow", lambda _n: 4
    )
    server = make_server(0)
    room = await server.manager.create_room(
        source="private",
        session_id="aiocqhttp:private:10001",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type="pig_dice",
        difficulty="normal",
    )
    visitor = await server.manager.join(room)
    room.player_token = visitor.token
    room.status = "active"
    room.game = PigDiceGame(turn="human")

    app = server._build_app()
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            f"/api/room/{room.access_token}/dice/action",
            json={"visitor_token": visitor.token, "action": "roll", "value": 6},
            headers={"Origin": str(client.make_url("/")).rstrip("/")},
        )
        payload = await response.json()

    assert response.status == 200
    game = payload["data"]["room"]["game"]
    assert game["last_roll"] == 5
    assert game["turn_total"] == 5
