from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_config_defaults_match_product_contract() -> None:
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    rooms = schema["rooms"]["items"]

    assert rooms["max_group_rooms"]["default"] == 1
    assert rooms["max_private_rooms"]["default"] == 1
    assert rooms["empty_player_timeout_seconds"]["default"] == 60
    assert rooms["idle_timeout_seconds"]["default"] == 300
    assert rooms["allow_non_admin_group_creation"]["default"] is False
    assert schema["xiangqi"]["items"]["allow_engine_download"]["default"] is True
    assert schema["xiangqi"]["items"]["auto_download_engine"]["default"] is False


def test_metadata_registers_default_management_page() -> None:
    metadata = yaml.safe_load((ROOT / "metadata.yaml").read_text(encoding="utf-8"))

    assert metadata["astrbot_version"] == ">=4.26.8"
    assert metadata["repo"] == (
        "https://github.com/StarfallMark/astrbot_plugin_game_companion"
    )
    assert metadata["pages"] == [{"name": "游戏管理台", "title": "游戏管理台"}]
    assert metadata["version"] == "0.1.1"


def test_frontends_do_not_use_external_cdn_or_inline_scripts() -> None:
    for page in (
        ROOT / "web" / "index.html",
        ROOT / "pages" / "游戏管理台" / "index.html",
    ):
        content = page.read_text(encoding="utf-8")
        assert "https://" not in content
        assert "<script>" not in content


def test_player_move_is_rendered_before_waiting_for_bot_response() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    optimistic_render = script.index(
        'pendingMove = { kind: "gomoku", row, column, color: room.game.human_color };'
    )
    move_request = script.index(
        'await request("POST", "move", { visitor_token: visitorToken, row, column })'
    )

    assert optimistic_render < move_request
    assert "pendingMove = null;" in script[move_request:]


def test_xiangqi_webui_uses_server_legal_moves_without_game_switcher() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    assert "room.game.legal_moves" in script
    assert 'room.game_type === "xiangqi"' in script
    assert "from_row" in script and "to_column" in script
    assert "switch_game" not in script
    assert "切换游戏" not in page


def test_management_page_can_switch_games_and_install_engine() -> None:
    script = (ROOT / "pages" / "游戏管理台" / "manager.js").read_text(
        encoding="utf-8"
    )
    page = (ROOT / "pages" / "游戏管理台" / "index.html").read_text(
        encoding="utf-8"
    )

    assert 'action: "switch_game"' in script
    assert "confirm_abandon: active" in script
    assert 'endpoint("POST", "xiangqi/install")' in script
    assert "window.confirm" not in script
    assert 'id="confirmDialog"' in page
    assert "confirmAction" in script


def test_room_expiry_does_not_stop_the_shared_access_channel() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")

    assert "_stop_idle_access" not in source
    assert "self._schedule_tunnel_recovery()" in source


def test_natural_language_rematch_is_routed_to_the_existing_room() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")

    assert 'elif action == "rematch"' in source
    assert "restart_finished_game" in source
    assert "绝不能 close 后调用创建工具" in source


def test_browser_reports_departure_with_heartbeat_fallback() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    manager = (ROOT / "room_manager.py").read_text(encoding="utf-8")

    assert 'window.addEventListener("pagehide"' in script
    assert "window.navigator.sendBeacon" in script
    assert 'endpoint("leave")' in script
    assert "FINISHED_PLAYER_LEAVE_GRACE_SECONDS = 8" in manager
    assert "FINISHED_PLAYER_HEARTBEAT_TIMEOUT_SECONDS = 60" in manager
