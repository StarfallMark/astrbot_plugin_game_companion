from __future__ import annotations

import time
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from astrbot_plugin_game_companion.room_manager import RoomManager
from astrbot_plugin_game_companion.server import GameRoomServer
from astrbot_plugin_game_companion.trusted_identity import TrustedIdentityStore


async def make_room(manager: RoomManager):
    return await manager.create_room(
        source="private",
        session_id="aiocqhttp:private:10001",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        difficulty="normal",
    )


def make_server(manager: RoomManager, store: TrustedIdentityStore) -> GameRoomServer:
    plugin = SimpleNamespace(
        public_base_url="https://games.example.com/game",
        quick_tunnel=SimpleNamespace(url="", running=False),
        trusted_browser_enabled=True,
        trusted_browser_ttl_days=30,
        trusted_browser_cookie_path="/game",
        trusted_identity_store=store,
    )
    return GameRoomServer(
        plugin,
        manager,
        host="127.0.0.1",
        port=0,
        web_root=Path(__file__).resolve().parents[1] / "web",
    )


@pytest.mark.asyncio
async def test_trusted_store_hashes_tokens_persists_and_expires(tmp_path: Path) -> None:
    path = tmp_path / "trusted_browsers.json"
    store = TrustedIdentityStore(path, ttl_days=1)
    current = time.time()

    token, issued = await store.issue("10001", "测试玩家", now=current)

    assert issued.qq == "10001"
    content = path.read_text(encoding="utf-8")
    assert token not in content
    assert "10001" in content

    reloaded = TrustedIdentityStore(path, ttl_days=1)
    resolved = await reloaded.resolve(token, now=current + 60)
    assert resolved is not None
    assert resolved.display_name == "测试玩家"
    assert await reloaded.resolve(token, now=current + 86401) is None


@pytest.mark.asyncio
async def test_trusted_store_revokes_every_device_for_one_qq(tmp_path: Path) -> None:
    store = TrustedIdentityStore(tmp_path / "trusted_browsers.json")
    first, _identity = await store.issue("10001", "玩家")
    second, _identity = await store.issue("10001", "玩家")
    other, _identity = await store.issue("10002", "其他玩家")

    assert await store.revoke_qq("10001") == 2
    assert await store.resolve(first) is None
    assert await store.resolve(second) is None
    assert await store.resolve(other) is not None


@pytest.mark.asyncio
async def test_trusted_cookie_recognizes_qq_in_a_new_room(tmp_path: Path) -> None:
    manager = RoomManager()
    store = TrustedIdentityStore(tmp_path / "trusted_browsers.json")
    device_token, _identity = await store.issue("10001", "测试玩家")
    server = make_server(manager, store)
    room = await make_room(manager)

    async with TestClient(TestServer(server._build_app())) as client:
        origin = str(client.make_url("/")).rstrip("/")
        response = await client.post(
            f"/api/room/{room.access_token}/join",
            json={"visitor_token": "", "remember_identity": True},
            headers={
                "Origin": origin,
                "Cookie": f"{server.TRUSTED_BROWSER_COOKIE}={device_token}",
            },
        )
        payload = await response.json()

    assert response.status == 200
    snapshot = payload["data"]["room"]
    assert snapshot["player_confirmed"] is True
    assert snapshot["visitor_display_name"] == "测试玩家"
    assert snapshot["identity_token"] == ""
    assert snapshot["trusted_browser_active"] is True


@pytest.mark.asyncio
async def test_manual_binding_can_issue_and_forget_secure_cookie(tmp_path: Path) -> None:
    manager = RoomManager()
    store = TrustedIdentityStore(tmp_path / "trusted_browsers.json")
    server = make_server(manager, store)
    room = await make_room(manager)

    async with TestClient(TestServer(server._build_app())) as client:
        origin = str(client.make_url("/")).rstrip("/")
        join_response = await client.post(
            f"/api/room/{room.access_token}/join",
            json={"visitor_token": "", "remember_identity": True},
            headers={"Origin": origin},
        )
        join_payload = await join_response.json()
        visitor_token = join_payload["data"]["visitor_token"]
        identity_token = join_payload["data"]["room"]["identity_token"]
        await manager.bind_visitor_identity(
            session_id=room.session_id,
            identity_token=identity_token,
            qq="10001",
            display_name="测试玩家",
        )

        state_response = await client.get(
            f"/api/room/{room.access_token}/state",
            params={
                "visitor_token": visitor_token,
                "remember_identity": "1",
            },
        )
        state_payload = await state_response.json()
        cookie = SimpleCookie()
        cookie.load(state_response.headers.get("Set-Cookie", ""))
        morsel = cookie[server.TRUSTED_BROWSER_COOKIE]
        device_token = morsel.value

        forget_response = await client.post(
            f"/api/room/{room.access_token}/identity/forget",
            json={"visitor_token": visitor_token},
            headers={
                "Origin": origin,
                "Cookie": f"{server.TRUSTED_BROWSER_COOKIE}={device_token}",
            },
        )
        forget_payload = await forget_response.json()
        after_forget_response = await client.get(
            f"/api/room/{room.access_token}/state",
            params={
                "visitor_token": visitor_token,
                "remember_identity": "1",
            },
        )
        after_forget_payload = await after_forget_response.json()

    assert state_payload["data"]["room"]["trusted_browser_active"] is True
    assert morsel["secure"] is True
    assert morsel["httponly"] is True
    assert morsel["samesite"] == "Lax"
    assert morsel["path"] == "/game"
    assert await store.resolve(device_token) is None
    assert forget_payload["data"]["room"]["trusted_browser_active"] is False
    assert forget_payload["data"]["room"]["player_confirmed"] is True
    assert server.TRUSTED_BROWSER_COOKIE not in after_forget_response.cookies
    assert after_forget_payload["data"]["room"]["trusted_browser_active"] is False
    assert store.count == 0
