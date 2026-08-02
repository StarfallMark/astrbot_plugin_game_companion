from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from .gomoku import Difficulty

EMPTY = 0
X = 1
NOUGHT = 2
BOARD_SIZE = 3
WIN_LINES = (
    ((0, 0), (0, 1), (0, 2)),
    ((1, 0), (1, 1), (1, 2)),
    ((2, 0), (2, 1), (2, 2)),
    ((0, 0), (1, 0), (2, 0)),
    ((0, 1), (1, 1), (2, 1)),
    ((0, 2), (1, 2), (2, 2)),
    ((0, 0), (1, 1), (2, 2)),
    ((0, 2), (1, 1), (2, 0)),
)


@dataclass(slots=True)
class TicTacToeGame:
    """Authoritative 3x3 Tic-tac-toe state with three Bot skill levels."""

    human_mark: int = X
    difficulty: Difficulty = "normal"
    board: list[list[int]] = field(
        default_factory=lambda: [
            [EMPTY for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)
        ]
    )
    turn: int = X
    winner: int = EMPTY
    draw: bool = False
    history: list[tuple[int, int, int]] = field(default_factory=list)

    @property
    def bot_mark(self) -> int:
        return NOUGHT if self.human_mark == X else X

    @property
    def finished(self) -> bool:
        return self.winner != EMPTY or self.draw

    def place(self, row: int, column: int, mark: int) -> None:
        if self.finished:
            raise ValueError("对局已经结束")
        if mark not in {X, NOUGHT} or mark != self.turn:
            raise ValueError("现在还没有轮到这一方")
        if not (0 <= row < BOARD_SIZE and 0 <= column < BOARD_SIZE):
            raise ValueError("落子位置超出棋盘")
        if self.board[row][column] != EMPTY:
            raise ValueError("这个位置已经有棋子")
        self.board[row][column] = mark
        self.history.append((row, column, mark))
        if self._winner_for_board() == mark:
            self.winner = mark
            return
        if len(self.history) == BOARD_SIZE * BOARD_SIZE:
            self.draw = True
            return
        self.turn = NOUGHT if mark == X else X

    def choose_bot_move(self, *, seed: int | None = None) -> tuple[int, int]:
        if self.finished or self.turn != self.bot_mark:
            raise ValueError("现在不是 Bot 的回合")
        legal = self._legal_moves()
        if not legal:
            raise ValueError("棋盘上没有可落子位置")
        rng = random.Random(seed)
        winning = self._immediate_move(self.bot_mark, legal)
        blocking = self._immediate_move(self.human_mark, legal)

        if self.difficulty == "easy":
            if winning is not None and rng.random() < 0.58:
                return winning
            if blocking is not None and rng.random() < 0.34:
                return blocking
            return rng.choice(legal)

        if winning is not None:
            return winning
        if blocking is not None:
            return blocking
        if self.difficulty == "normal":
            if self.board[1][1] == EMPTY:
                return 1, 1
            corners = [move for move in legal if move in {(0, 0), (0, 2), (2, 0), (2, 2)}]
            return rng.choice(corners or legal)

        ranked: list[tuple[int, tuple[int, int]]] = []
        for row, column in legal:
            self.board[row][column] = self.bot_mark
            score = self._minimax(self.human_mark, depth=1, alpha=-math.inf, beta=math.inf)
            self.board[row][column] = EMPTY
            ranked.append((score, (row, column)))
        best_score = max(score for score, _move in ranked)
        best_moves = [move for score, move in ranked if score == best_score]
        return rng.choice(best_moves)

    def undo_round(self) -> int:
        human_index = next(
            (
                index
                for index in range(len(self.history) - 1, -1, -1)
                if self.history[index][2] == self.human_mark
            ),
            -1,
        )
        if human_index < 0:
            raise ValueError("还没有可以悔掉的玩家落子")
        removed = self.history[human_index:]
        for row, column, _mark in removed:
            self.board[row][column] = EMPTY
        del self.history[human_index:]
        self.winner = EMPTY
        self.draw = False
        self.turn = self.human_mark
        return len(removed)

    def tactical_state(self, mark: int | None) -> str:
        if mark not in {X, NOUGHT}:
            return ""
        if self.winner == mark:
            return "win"
        threats = sum(
            self._would_win(row, column, mark) for row, column in self._legal_moves()
        )
        return "fork" if threats >= 2 else ""

    def snapshot(self) -> dict[str, object]:
        return {
            "board": self.board,
            "turn": self.turn,
            "winner": self.winner,
            "draw": self.draw,
            "human_mark": self.human_mark,
            "bot_mark": self.bot_mark,
            "difficulty": self.difficulty,
            "move_count": len(self.history),
            "last_move": list(self.history[-1][:2]) if self.history else None,
        }

    def _legal_moves(self) -> list[tuple[int, int]]:
        preferred = (
            (1, 1),
            (0, 0),
            (0, 2),
            (2, 0),
            (2, 2),
            (0, 1),
            (1, 0),
            (1, 2),
            (2, 1),
        )
        return [move for move in preferred if self.board[move[0]][move[1]] == EMPTY]

    def _winner_for_board(self) -> int:
        for line in WIN_LINES:
            marks = [self.board[row][column] for row, column in line]
            if marks[0] != EMPTY and marks.count(marks[0]) == 3:
                return marks[0]
        return EMPTY

    def _would_win(self, row: int, column: int, mark: int) -> bool:
        self.board[row][column] = mark
        winner = self._winner_for_board()
        self.board[row][column] = EMPTY
        return winner == mark

    def _immediate_move(
        self, mark: int, legal: list[tuple[int, int]]
    ) -> tuple[int, int] | None:
        return next(
            (
                (row, column)
                for row, column in legal
                if self._would_win(row, column, mark)
            ),
            None,
        )

    def _minimax(self, mark: int, *, depth: int, alpha: float, beta: float) -> int:
        winner = self._winner_for_board()
        if winner == self.bot_mark:
            return 10 - depth
        if winner == self.human_mark:
            return depth - 10
        legal = self._legal_moves()
        if not legal:
            return 0
        if mark == self.bot_mark:
            value = -math.inf
            for row, column in legal:
                self.board[row][column] = mark
                value = max(
                    value,
                    self._minimax(
                        self.human_mark, depth=depth + 1, alpha=alpha, beta=beta
                    ),
                )
                self.board[row][column] = EMPTY
                alpha = max(alpha, value)
                if beta <= alpha:
                    break
            return int(value)
        value = math.inf
        for row, column in legal:
            self.board[row][column] = mark
            value = min(
                value,
                self._minimax(
                    self.bot_mark, depth=depth + 1, alpha=alpha, beta=beta
                ),
            )
            self.board[row][column] = EMPTY
            beta = min(beta, value)
            if beta <= alpha:
                break
        return int(value)
