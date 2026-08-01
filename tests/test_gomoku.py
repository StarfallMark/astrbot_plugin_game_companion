from __future__ import annotations

import pytest

from astrbot_plugin_game_companion.gomoku import BLACK, EMPTY, WHITE, GomokuGame


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
