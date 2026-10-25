from __future__ import annotations

import pytest
from astrbot_plugin_game_companion.othello import (
    BLACK,
    EMPTY,
    WHITE,
    OthelloGame,
)
from astrbot_plugin_game_companion.room_manager import RoomManager


def test_initial_board_and_legal_moves_follow_standard_rules() -> None:
    game = OthelloGame()

    assert game.black_count == 2
    assert game.white_count == 2
    assert game.turn == BLACK
    assert game.legal_moves() == [(2, 3), (3, 2), (4, 5), (5, 4)]
    assert game.legal_moves(WHITE) == [(2, 4), (3, 5), (4, 2), (5, 3)]
    assert game.snapshot()["legal_moves"] == [[2, 3], [3, 2], [4, 5], [5, 4]]


def test_move_flips_every_bracketed_opponent_disk_and_rejects_illegal_moves() -> None:
    game = OthelloGame()

    with pytest.raises(ValueError, match="不能夹住"):
        game.place(0, 0, BLACK)
    with pytest.raises(ValueError, match="轮到"):
        game.place(2, 3, WHITE)

    game.place(2, 3, BLACK)

    assert game.board[2][3] == BLACK
    assert game.board[3][3] == BLACK
    assert (game.black_count, game.white_count) == (4, 1)
    assert game.turn == WHITE


def test_no_legal_move_is_skipped_and_game_ends_after_both_sides_pass() -> None:
    board = [
        [0, 2, 2, 1, 1, 1, 1, 1],
        [2, 2, 2, 1, 1, 2, 2, 0],
        [1, 1, 1, 2, 1, 2, 2, 2],
        [1, 1, 2, 2, 1, 2, 2, 2],
        [1, 1, 2, 2, 1, 2, 2, 2],
        [1, 1, 2, 2, 2, 1, 2, 2],
        [1, 1, 1, 2, 2, 2, 2, 1],
        [1, 1, 1, 0, 2, 2, 2, 1],
    ]
    game = OthelloGame(board=board, turn=BLACK, human_color=WHITE)

    assert not game.legal_moves(WHITE)
    assert game.legal_moves(BLACK)
    game.place(0, 0, BLACK)

    assert game.last_passed == WHITE
    assert game.turn == BLACK
    assert game.legal_moves(BLACK)

    board = [[BLACK for _ in range(8)] for _ in range(8)]
    board[7][6] = WHITE
    board[7][7] = EMPTY
    final = OthelloGame(board=board, turn=BLACK)
    final.place(7, 7, BLACK)

    assert final.finished
    assert final.winner == BLACK
    assert not final.draw
    assert final.black_count == 64
    assert final.white_count == 0


def test_undo_round_restores_flipped_disks_and_preserves_prior_rounds() -> None:
    game = OthelloGame(human_color=BLACK)
    game.place(2, 3, BLACK)
    bot_move = game.legal_moves(WHITE)[0]
    game.place(*bot_move, WHITE)
    board_before_human_move = [row[:] for row in game.board]
    human_move = game.legal_moves(BLACK)[0]
    game.place(*human_move, BLACK)

    game.undo_round()

    assert len(game.history) == 2
    assert game.turn == BLACK
    assert game.board == board_before_human_move
    assert game.winner == EMPTY
    assert not game.finished


@pytest.mark.parametrize("difficulty", ["easy", "normal", "hard"])
def test_bot_selects_a_legal_move_at_each_difficulty(difficulty: str) -> None:
    game = OthelloGame(human_color=WHITE, difficulty=difficulty)  # type: ignore[arg-type]

    move = game.choose_bot_move(seed=7, time_limit=0.12)

    assert move in game.legal_moves(BLACK)


@pytest.mark.asyncio
async def test_room_manager_starts_bot_when_human_chooses_white() -> None:
    manager = RoomManager()
    room = await manager.create_room(
        source="private",
        session_id="aiocqhttp:private:10001",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type="othello",
        difficulty="easy",
    )
    visitor = await manager.join(room)

    await manager.claim_and_start(room, visitor.token, "bot_black")

    assert isinstance(room.game, OthelloGame)
    assert room.game.human_color == WHITE
    assert len(room.game.history) == 1
    assert room.game.turn == WHITE
    assert room.status == "active"
