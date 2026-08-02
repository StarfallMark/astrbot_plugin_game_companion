from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_game_companion.main import GameCompanionPlugin


def make_plugin(*, delivered: bool) -> GameCompanionPlugin:
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.context = SimpleNamespace(send_message=AsyncMock(return_value=delivered))
    plugin.manager = SimpleNamespace(empty_player_timeout=60)
    return plugin


def make_room() -> SimpleNamespace:
    return SimpleNamespace(
        room_id="room-1",
        session_id="aiocqhttp:FriendMessage:10001",
        game_type="xiangqi",
        player=None,
    )


@pytest.mark.asyncio
async def test_room_link_is_sent_as_an_independent_plain_message() -> None:
    plugin = make_plugin(delivered=True)
    room = make_room()
    url = "https://example.test/room/access-token"

    delivered = await plugin._deliver_room_link(
        room,
        url,
        reused=False,
        restarted=False,
    )

    assert delivered is True
    session_id, chain = plugin.context.send_message.await_args.args
    assert session_id == room.session_id
    assert len(chain.chain) == 1
    assert chain.chain[0].text == (
        "中国象棋房间已准备好：\n"
        f"{url}\n"
        "请在 60 秒内进入玩家席，否则房间会自动销毁。"
    )


@pytest.mark.asyncio
async def test_room_link_delivery_reports_platform_failure_for_model_fallback() -> None:
    plugin = make_plugin(delivered=False)

    delivered = await plugin._deliver_room_link(
        make_room(),
        "https://example.test/room/access-token",
        reused=True,
        restarted=False,
    )

    assert delivered is False
