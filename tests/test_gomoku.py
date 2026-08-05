from __future__ import annotations

import pytest
from astrbot_plugin_game_companion.gomoku import BLACK, EMPTY, WHITE, GomokuGame
from astrbot_plugin_game_companion.main import GameCompanionPlugin
from astrbot_plugin_game_companion.models import GameRoom


def make_room(game: GomokuGame) -> GameRoom:
    room = GameRoom(
        room_id="room-1",
        access_token="access-token",
        source="private",
        session_id="aiocqhttp:private:10001",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type="gomoku",
        difficulty="normal",
    )
    room.game = game
    return room


def test_freestyle_five_or_more_wins() -> None:
    game = GomokuGame(human_color=BLACK)
    for column in range(4):
        game.board[7][column] = BLACK
        game.history.append((7, column, BLACK))
    game.turn = BLACK

    game.place(7, 4, BLACK)

    assert game.winner == BLACK
    assert game.finished


def test_move_rejects_wrong_turn_and_occupied_cell() -> None:
    game = GomokuGame(human_color=BLACK)
    with pytest.raises(ValueError, match="轮到"):
        game.place(7, 7, WHITE)

    game.place(7, 7, BLACK)
    with pytest.raises(ValueError, match="有棋子"):
        game.place(7, 7, WHITE)


@pytest.mark.parametrize("difficulty", ["normal", "hard"])
def test_ai_blocks_immediate_human_win(difficulty: str) -> None:
    game = GomokuGame(human_color=BLACK, difficulty=difficulty)
    for column in range(4, 8):
        game.board[8][column] = BLACK
        game.history.append((8, column, BLACK))
    game.turn = WHITE

    row, column = game.choose_bot_move(seed=2, time_limit=0.1)

    assert row == 8
    assert column in {3, 8}


def test_ai_takes_immediate_win_before_blocking() -> None:
    game = GomokuGame(human_color=BLACK, difficulty="normal")
    for column in range(4):
        game.board[6][column] = WHITE
        game.history.append((6, column, WHITE))
        game.board[9][column] = BLACK
        game.history.append((9, column, BLACK))
    game.turn = WHITE

    assert game.choose_bot_move(seed=1) == (6, 4)


def test_undo_round_removes_player_and_following_bot_stone() -> None:
    game = GomokuGame(human_color=BLACK)
    game.place(7, 7, BLACK)
    game.place(7, 8, WHITE)
    game.place(6, 7, BLACK)
    game.place(6, 8, WHITE)

    removed = game.undo_round()

    assert removed == 2
    assert game.board[6][7] == EMPTY
    assert game.board[6][8] == EMPTY
    assert game.turn == BLACK
    assert len(game.history) == 2


def test_snapshot_does_not_expose_mutation_helpers() -> None:
    snapshot = GomokuGame().snapshot()

    assert snapshot["move_count"] == 0
    assert "history" not in snapshot


def test_last_move_threat_finds_two_immediate_winning_points() -> None:
    game = GomokuGame(human_color=BLACK)
    for column in range(4, 8):
        game.board[7][column] = BLACK

    threat = game.move_threat(7, 7, BLACK)

    assert threat.kind == "multiple"
    assert threat.winning_points == ((7, 3), (7, 8))


def test_last_move_threat_distinguishes_one_blockable_point() -> None:
    game = GomokuGame(human_color=BLACK)
    game.board[7][3] = WHITE
    for column in range(4, 8):
        game.board[7][column] = BLACK

    threat = game.move_threat(7, 7, BLACK)

    assert threat.kind == "single"
    assert threat.winning_points == ((7, 8),)


def test_last_move_threat_finds_internal_winning_gap() -> None:
    game = GomokuGame(human_color=BLACK)
    for column in (4, 5, 7, 8):
        game.board[7][column] = BLACK

    threat = game.move_threat(7, 8, BLACK)

    assert threat.kind == "single"
    assert threat.winning_points == ((7, 6),)


def test_last_move_does_not_report_an_unrelated_existing_threat() -> None:
    game = GomokuGame(human_color=BLACK)
    for column in range(4, 8):
        game.board[7][column] = BLACK
    game.board[10][10] = BLACK

    threat = game.move_threat(10, 10, BLACK)

    assert threat.kind == ""
    assert threat.winning_points == ()


def test_gomoku_commentary_names_actor_color_coordinate_and_consequence() -> None:
    game = GomokuGame(human_color=BLACK)
    for column in range(4, 8):
        game.board[7][column] = BLACK
    game.turn = WHITE

    prompt = GameCompanionPlugin._gomoku_commentary_prompt(
        make_room(game),
        {"actor": "human", "row": 7, "column": 7, "color": BLACK},
    )

    assert "触发者是玩家，执黑" in prompt
    assert "对手是Bot，执白" in prompt
    assert "第 8 行第 8 列" in prompt
    assert "玩家有 4 颗棋子" in prompt
    assert "当前轮到 Bot" in prompt
    assert "2 个下一手即可连成五子的空位" in prompt
    assert "Bot下一手无法全部封住" in prompt
    assert all(term not in prompt for term in ("活四", "冲四", "死四"))


def test_gomoku_commentary_explains_single_bot_threat_without_jargon() -> None:
    game = GomokuGame(human_color=BLACK)
    game.board[6][3] = BLACK
    for column in range(4, 8):
        game.board[6][column] = WHITE
    game.turn = BLACK

    prompt = GameCompanionPlugin._gomoku_commentary_prompt(
        make_room(game),
        {"actor": "bot", "row": 6, "column": 7, "color": WHITE},
    )

    assert "触发者是Bot，执白" in prompt
    assert "对手是玩家，执黑" in prompt
    assert "第 7 行第 8 列" in prompt
    assert "1 个下一手即可连成五子的空位" in prompt
    assert "玩家下一手仍可封住" in prompt
    assert "专业棋型名称" in prompt
