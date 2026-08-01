from __future__ import annotations

import shutil

import pytest

from astrbot_plugin_game_companion.tunnel import QuickTunnel


@pytest.mark.asyncio
async def test_tunnel_records_an_exit_after_publishing_its_url(
    tmp_path, monkeypatch
) -> None:
    binary = tmp_path / "cloudflared"
    binary.write_text(
        "#!/bin/sh\n"
        "echo 'https://temporary-game.trycloudflare.com'\n"
        "sleep 0.2\n"
        "exit 7\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    monkeypatch.setattr(shutil, "which", lambda _command: None)
    tunnel = QuickTunnel("http://127.0.0.1:42000", search_paths=[tmp_path])

    url = await tunnel.start(timeout=2)
    assert url == "https://temporary-game.trycloudflare.com"

    reader_task = tunnel._reader_task
    assert reader_task is not None
    await reader_task

    assert not tunnel.running
    assert not tunnel.ready
    assert "代码 7" in tunnel.error
