from __future__ import annotations

import copy

import pytest

from astrbot_plugin_game_companion.room_manager import RoomManager
from astrbot_plugin_game_companion.tictactoe import (
    EMPTY,
    NOUGHT,
    X,
    TicTacToeGame,
)


def test_tictactoe_enforces_turns_and_detects_winner() -> None:
    game = TicTacToeGame(human_mark=X)

    game.place(0, 0, X)
    game.place(1, 0, NOUGHT)
    game.place(0, 1, X)
    game.place(1, 1, NOUGHT)
    game.place(0, 2, X)

    assert game.finished
    assert game.winner == X
    assert not game.draw
    with pytest.raises(ValueError, match="结束"):
        game.place(2, 2, NOUGHT)


def test_normal_bot_takes_a_win_and_blocks_a_loss() -> None:
    winning = TicTacToeGame(human_mark=NOUGHT, difficulty="normal")
    winning.board = [
        [X, X, EMPTY],
        [NOUGHT, NOUGHT, EMPTY],
        [EMPTY, EMPTY, EMPTY],
    ]
    winning.turn = X
    winning.history = [
        (0, 0, X),
        (1, 0, NOUGHT),
        (0, 1, X),
        (1, 1, NOUGHT),
    ]
    assert winning.choose_bot_move(seed=1) == (0, 2)

    blocking = TicTacToeGame(human_mark=X, difficulty="normal")
    blocking.board = [
        [X, X, EMPTY],
        [NOUGHT, EMPTY, EMPTY],
        [EMPTY, EMPTY, EMPTY],
    ]
    blocking.turn = NOUGHT
    blocking.history = [(0, 0, X), (1, 0, NOUGHT), (0, 1, X)]
    assert blocking.choose_bot_move(seed=1) == (0, 2)


@pytest.mark.parametrize("human_mark", [X, NOUGHT])
def test_hard_bot_cannot_be_forced_to_lose(human_mark: int) -> None:
    def explore(game: TicTacToeGame) -> None:
        assert game.winner != game.human_mark
        if game.finished:
            return
        if game.turn == game.bot_mark:
            move = game.choose_bot_move(seed=7)
            next_game = copy.deepcopy(game)
            next_game.place(move[0], move[1], next_game.bot_mark)
            explore(next_game)
            return
        for row, column in game._legal_moves():
            next_game = copy.deepcopy(game)
            next_game.place(row, column, next_game.human_mark)
            explore(next_game)

    explore(TicTacToeGame(human_mark=human_mark, difficulty="hard"))


def test_tictactoe_undo_removes_the_latest_round() -> None:
    game = TicTacToeGame(human_mark=X)
    game.place(0, 0, X)
    game.place(1, 1, NOUGHT)
    game.place(0, 1, X)
    game.place(0, 2, NOUGHT)

    assert game.undo_round() == 2
    assert game.history == [(0, 0, X), (1, 1, NOUGHT)]
    assert game.turn == X


@pytest.mark.asyncio
async def test_room_manager_starts_tictactoe_and_preserves_score_on_switch() -> None:
    manager = RoomManager()
    room = await manager.create_room(
        source="private",
        session_id="aiocqhttp:private:10001",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type="tictactoe",
        difficulty="hard",
    )
    visitor = await manager.join(room)
    await manager.claim_and_start(room, visitor.token, "human_x")

    assert isinstance(room.game, TicTacToeGame)
    assert room.game.human_mark == X
    room.status = "finished"
    room.completed_games = 1
    room.human_wins = 1
    await manager.switch_game(room, "gomoku")
    await manager.switch_game(room, "tictactoe")

    assert room.completed_games == 1
    assert room.human_wins == 1
