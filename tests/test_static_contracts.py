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
    assert schema["turtle_soup"]["items"]["max_hints"]["default"] == 3
    assert schema["turtle_soup"]["items"]["content_level"]["default"] == "normal"
    assert schema["turtle_soup"]["items"]["max_players"]["default"] == 6
    assert schema["multiplayer"]["items"]["turn_timeout_seconds"]["default"] == 60
    assert (
        schema["multiplayer"]["items"]["swap_request_cooldown_seconds"]["default"] == 30
    )
    assert (
        schema["multiplayer"]["items"]["swap_request_expiry_seconds"]["default"] == 20
    )


def test_metadata_registers_default_management_page() -> None:
    metadata = yaml.safe_load((ROOT / "metadata.yaml").read_text(encoding="utf-8"))

    assert metadata["astrbot_version"] == ">=4.24.2"
    assert metadata["repo"] == (
        "https://github.com/StarfallMark/astrbot_plugin_game_companion"
    )
    assert metadata["pages"] == [{"name": "游戏管理台", "title": "游戏管理台"}]
    assert metadata["version"] == "0.1.6"


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
        'await request("POST", "move", { visitor_token: visitorToken, row, column })',
        optimistic_render,
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


def test_tictactoe_webui_has_three_by_three_board_and_optimistic_move() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    style = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

    assert 'room?.game_type === "tictactoe"' in script
    assert '[["human_x", "我执 X"], ["human_o", "我执 O"]' in script
    optimistic = script.index(
        'pendingMove = { kind: "tictactoe", row, column, mark: room.game.human_mark };'
    )
    request = script.index(
        'await request("POST", "move", { visitor_token: visitorToken, row, column })',
        optimistic,
    )
    assert optimistic < request
    assert "function drawTicTacToe()" in script
    assert ".board-stage.tictactoe" in style


def test_management_page_can_switch_games_and_install_engine() -> None:
    script = (ROOT / "pages" / "游戏管理台" / "manager.js").read_text(encoding="utf-8")
    page = (ROOT / "pages" / "游戏管理台" / "index.html").read_text(encoding="utf-8")

    assert 'action: "switch_game"' in script
    assert '["tictactoe", "井字棋"]' in script
    assert '["turtle_soup", "海龟汤"]' in script
    assert "confirm_abandon: active" in script
    assert 'endpoint("POST", "xiangqi/install")' in script
    assert "window.confirm" not in script
    assert 'id="confirmDialog"' in page
    assert "confirmAction" in script


def test_turtle_soup_webui_has_no_game_switcher_and_keeps_failed_input() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

    assert 'id="soupStage"' in page
    assert 'id="soupInput"' in page
    assert 'id="soupSolution"' in page
    assert 'request("POST", action, { visitor_token: visitorToken, text })' in script
    assert 'request("POST", "soup/hint"' in script
    assert '["turtle_soup", "pig_dice"].includes(room.game_type)' in script
    assert ".side-choice[hidden] { display: none; }" in styles
    assert 'soupInput.value = "";' in script
    failure_branch = script.index('showToast(error?.message || "Bot 暂时无法判断")')
    success_clear = script.index('soupInput.value = "";')
    assert success_clear < failure_branch
    assert "switch_game" not in script
    assert "切换游戏" not in page
    assert '"soup/reverse"' in script
    assert 'request("POST", "seat/swap/request"' in script
    assert 'id="soupCorrectAction"' in page


def test_room_expiry_does_not_stop_the_shared_access_channel() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")

    assert "_stop_idle_access" not in source
    assert "self._schedule_tunnel_recovery()" in source


def test_natural_language_rematch_is_routed_to_the_existing_room() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")

    assert 'elif action == "rematch"' in source
    assert "restart_finished_game" in source
    assert "绝不能 close 后调用创建工具" in source


def test_pig_dice_webui_and_qq_menu_are_registered() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    manager = (ROOT / "pages" / "游戏管理台" / "manager.js").read_text(encoding="utf-8")

    assert '@filter.command_group("game")' in source
    assert '@game_commands.command("游戏菜单"' in source
    assert '"贪心骰子": "pig_dice"' in source
    assert 'id="diceStage"' in page
    assert 'request("POST", "dice/action"' in script
    assert '["pig_dice", "贪心骰子"]' in manager


def test_fast_game_replies_do_not_pollute_normal_conversation_history() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")

    assert "_sync_conversation_pair" not in source
    assert "add_message_pair" not in source
    assert "record_shared_experience" in source
    assert "当前请求专用的临时游戏状态" in source
    assert "最近游戏回复只用于理解用户的承接和指代" in source


def test_tictactoe_is_available_to_natural_language_tools() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")

    assert '"井字棋": "tictactoe"' in source
    assert '"圈叉棋": "tictactoe"' in source
    assert '"tictactoe": "井字棋"' in source


def test_room_link_is_delivered_outside_model_rewrite_with_fallback() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")

    assert "link_delivered = await self._deliver_room_link" in source
    assert '"room_url": "" if link_delivered else url' in source
    assert 'MessageChain([Plain("\\n".join(lines))])' in source
    assert "将交由模型回复回退" in source


def test_browser_reports_departure_with_heartbeat_fallback() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    manager = (ROOT / "room_manager.py").read_text(encoding="utf-8")

    assert 'window.addEventListener("pagehide"' in script
    assert "window.navigator.sendBeacon" in script
    assert 'endpoint("leave")' in script
    assert "FINISHED_PLAYER_LEAVE_GRACE_SECONDS = 8" in manager
    assert "FINISHED_PLAYER_HEARTBEAT_TIMEOUT_SECONDS = 60" in manager
