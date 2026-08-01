from __future__ import annotations

import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_game_companion.room_manager import RoomManager
from astrbot_plugin_game_companion.server import GameRoomServer


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
