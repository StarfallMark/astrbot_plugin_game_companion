from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field

from .gomoku import Difficulty

BOARD_SIZE = 8
EMPTY = 0
BLACK = 1
WHITE = 2
DIRECTIONS = (
    (-1, -1),
    (-1, 0),
    (-1, 1),
    (0, -1),
    (0, 1),
    (1, -1),
    (1, 0),
    (1, 1),
)
POSITION_WEIGHTS = (
    (120, -25, 20, 5, 5, 20, -25, 120),
    (-25, -45, -5, -5, -5, -5, -45, -25),
    (20, -5, 15, 3, 3, 15, -5, 20),
    (5, -5, 3, 3, 3, 3, -5, 5),
    (5, -5, 3, 3, 3, 3, -5, 5),
    (20, -5, 15, 3, 3, 15, -5, 20),
    (-25, -45, -5, -5, -5, -5, -45, -25),
    (120, -25, 20, 5, 5, 20, -25, 120),
)


def _initial_board() -> list[list[int]]:
    board = [[EMPTY for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)]
    board[3][3] = WHITE
    board[3][4] = BLACK
    board[4][3] = BLACK
    board[4][4] = WHITE
    return board


@dataclass(slots=True)
class OthelloGame:
    """Authoritative 8x8 Othello game with bounded three-level Bot play."""

    human_color: int = BLACK
    difficulty: Difficulty = "normal"
    board: list[list[int]] = field(default_factory=_initial_board)
    turn: int = BLACK
    winner: int = EMPTY
    draw: bool = False
    history: list[tuple[int, int, int]] = field(default_factory=list)
    last_passed: int = EMPTY
    last_flipped_count: int = 0

    def __post_init__(self) -> None:
        if len(self.board) != BOARD_SIZE or any(
            len(row) != BOARD_SIZE for row in self.board
        ):
            raise ValueError("黑白棋棋盘必须是 8×8")

    @property
    def bot_color(self) -> int:
        return WHITE if self.human_color == BLACK else BLACK

    @property
    def finished(self) -> bool:
        return self.winner != EMPTY or self.draw

    @property
    def black_count(self) -> int:
        return sum(cell == BLACK for row in self.board for cell in row)

    @property
    def white_count(self) -> int:
        return sum(cell == WHITE for row in self.board for cell in row)

    def legal_moves(self, color: int | None = None) -> list[tuple[int, int]]:
        side = self.turn if color is None else color
        return self._legal_moves_on(self.board, side)

    def place(self, row: int, column: int, color: int) -> None:
        if self.finished:
            raise ValueError("对局已经结束")
        if color not in {BLACK, WHITE} or color != self.turn:
            raise ValueError("现在还没有轮到这一方")
        if not (0 <= row < BOARD_SIZE and 0 <= column < BOARD_SIZE):
            raise ValueError("落子位置超出棋盘")
        flips = self._flips_for(self.board, row, column, color)
        if not flips:
            raise ValueError("这个位置不能夹住对方棋子")

        self.board[row][column] = color
        for flip_row, flip_column in flips:
            self.board[flip_row][flip_column] = color
        self.history.append((row, column, color))
        self.last_passed = EMPTY
        self.last_flipped_count = len(flips)

        opponent = WHITE if color == BLACK else BLACK
        if self._legal_moves_on(self.board, opponent):
            self.turn = opponent
        elif self._legal_moves_on(self.board, color):
            # The opponent passes; the mover immediately gets another turn.
            self.turn = color
            self.last_passed = opponent
        else:
            self._settle()

    def choose_bot_move(
        self, *, seed: int | None = None, time_limit: float = 0.4
    ) -> tuple[int, int]:
        if self.finished or self.turn != self.bot_color:
            raise ValueError("现在不是 Bot 的回合")
        legal = self.legal_moves()
        if not legal:
            raise ValueError("当前没有合法落子")
        rng = random.Random(seed)
        if self.difficulty == "easy":
            return rng.choice(legal)

        if self.difficulty == "normal":
            ranked = sorted(
                legal,
                key=lambda move: self._move_heuristic(self.board, move, self.bot_color),
                reverse=True,
            )
            best_value = self._move_heuristic(self.board, ranked[0], self.bot_color)
            best_moves = [
                move
                for move in ranked
                if self._move_heuristic(self.board, move, self.bot_color) == best_value
            ]
            return rng.choice(best_moves)

        deadline = time.monotonic() + max(0.05, min(float(time_limit), 1.0))
        best_move = max(
            legal,
            key=lambda move: self._move_heuristic(self.board, move, self.bot_color),
        )
        # Iterative deepening keeps a legal fallback if the time budget expires.
        for depth in range(2, 7):
            try:
                ranked = sorted(
                    legal,
                    key=lambda move: self._move_heuristic(
                        self.board, move, self.bot_color
                    ),
                    reverse=True,
                )
                alpha = -math.inf
                beta = math.inf
                depth_best = best_move
                depth_value = -math.inf
                for move in ranked[:14]:
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    next_board = self._play_on(
                        self.board, move[0], move[1], self.bot_color
                    )
                    value = self._search(
                        next_board,
                        WHITE if self.bot_color == BLACK else BLACK,
                        self.bot_color,
                        depth - 1,
                        alpha,
                        beta,
                        deadline,
                    )
                    if value > depth_value:
                        depth_value = value
                        depth_best = move
                    alpha = max(alpha, depth_value)
                best_move = depth_best
            except TimeoutError:
                break
        return best_move

    def undo_round(self) -> int:
        human_index = next(
            (
                index
                for index in range(len(self.history) - 1, -1, -1)
                if self.history[index][2] == self.human_color
            ),
            -1,
        )
        if human_index < 0:
            raise ValueError("还没有可以悔掉的玩家落子")
        removed = self.history[human_index:]
        remaining = self.history[:human_index]
        self.board = [[EMPTY for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)]
        self.board[3][3] = WHITE
        self.board[3][4] = BLACK
        self.board[4][3] = BLACK
        self.board[4][4] = WHITE
        self.history = []
        self.turn = BLACK
        self.winner = EMPTY
        self.draw = False
        self.last_passed = EMPTY
        self.last_flipped_count = 0
        for row, column, color in remaining:
            self.place(row, column, color)
        return len(removed)

    def snapshot(self) -> dict[str, object]:
        return {
            "board": self.board,
            "turn": self.turn,
            "winner": self.winner,
            "draw": self.draw,
            "human_color": self.human_color,
            "bot_color": self.bot_color,
            "difficulty": self.difficulty,
            "move_count": len(self.history),
            "last_move": list(self.history[-1][:2]) if self.history else None,
            "legal_moves": [list(move) for move in self.legal_moves()],
            "last_passed": self.last_passed,
            "last_flipped_count": self.last_flipped_count,
            "black_count": self.black_count,
            "white_count": self.white_count,
        }

    def _settle(self) -> None:
        black = self.black_count
        white = self.white_count
        if black == white:
            self.winner = EMPTY
            self.draw = True
        else:
            self.winner = BLACK if black > white else WHITE
            self.draw = False

    def tactical_state(self, color: int | None) -> str:
        if self.finished and self.winner == color:
            return "win"
        if (
            self.history
            and self.history[-1][2] == color
            and self.last_flipped_count >= 4
        ):
            return "major_capture"
        return ""

    @classmethod
    def _legal_moves_on(
        cls, board: list[list[int]], color: int
    ) -> list[tuple[int, int]]:
        return [
            (row, column)
            for row in range(BOARD_SIZE)
            for column in range(BOARD_SIZE)
            if board[row][column] == EMPTY and cls._flips_for(board, row, column, color)
        ]

    @staticmethod
    def _flips_for(
        board: list[list[int]], row: int, column: int, color: int
    ) -> list[tuple[int, int]]:
        if board[row][column] != EMPTY:
            return []
        opponent = WHITE if color == BLACK else BLACK
        flips: list[tuple[int, int]] = []
        for delta_row, delta_column in DIRECTIONS:
            scan_row = row + delta_row
            scan_column = column + delta_column
            line: list[tuple[int, int]] = []
            while (
                0 <= scan_row < BOARD_SIZE
                and 0 <= scan_column < BOARD_SIZE
                and board[scan_row][scan_column] == opponent
            ):
                line.append((scan_row, scan_column))
                scan_row += delta_row
                scan_column += delta_column
            if (
                line
                and 0 <= scan_row < BOARD_SIZE
                and 0 <= scan_column < BOARD_SIZE
                and board[scan_row][scan_column] == color
            ):
                flips.extend(line)
        return flips

    @classmethod
    def _play_on(
        cls, board: list[list[int]], row: int, column: int, color: int
    ) -> list[list[int]]:
        next_board = [line[:] for line in board]
        next_board[row][column] = color
        for flip_row, flip_column in cls._flips_for(board, row, column, color):
            next_board[flip_row][flip_column] = color
        return next_board

    @classmethod
    def _move_heuristic(
        cls,
        board: list[list[int]],
        move: tuple[int, int],
        color: int,
    ) -> float:
        row, column = move
        flips = cls._flips_for(board, row, column, color)
        next_board = cls._play_on(board, row, column, color)
        opponent = WHITE if color == BLACK else BLACK
        mobility_delta = len(cls._legal_moves_on(next_board, color)) - len(
            cls._legal_moves_on(next_board, opponent)
        )
        return POSITION_WEIGHTS[row][column] - len(flips) * 0.2 + mobility_delta * 2

    @classmethod
    def _search(
        cls,
        board: list[list[int]],
        turn: int,
        perspective: int,
        depth: int,
        alpha: float,
        beta: float,
        deadline: float,
    ) -> float:
        if time.monotonic() >= deadline:
            raise TimeoutError
        moves = cls._legal_moves_on(board, turn)
        opponent = WHITE if turn == BLACK else BLACK
        if not moves:
            other_moves = cls._legal_moves_on(board, opponent)
            if not other_moves:
                return cls._evaluate(board, perspective)
            if depth <= 0:
                return cls._evaluate(board, perspective)
            return cls._search(
                board, opponent, perspective, depth, alpha, beta, deadline
            )
        if depth <= 0:
            return cls._evaluate(board, perspective)

        maximizing = turn == perspective
        moves = sorted(
            moves,
            key=lambda move: cls._move_heuristic(board, move, turn),
            reverse=True,
        )[:10]
        if maximizing:
            value = -math.inf
            for row, column in moves:
                next_board = cls._play_on(board, row, column, turn)
                value = max(
                    value,
                    cls._search(
                        next_board,
                        opponent,
                        perspective,
                        depth - 1,
                        alpha,
                        beta,
                        deadline,
                    ),
                )
                alpha = max(alpha, value)
                if beta <= alpha:
                    break
            return value
        value = math.inf
        for row, column in moves:
            next_board = cls._play_on(board, row, column, turn)
            value = min(
                value,
                cls._search(
                    next_board,
                    opponent,
                    perspective,
                    depth - 1,
                    alpha,
                    beta,
                    deadline,
                ),
            )
            beta = min(beta, value)
            if beta <= alpha:
                break
        return value

    @classmethod
    def _evaluate(cls, board: list[list[int]], perspective: int) -> float:
        opponent = WHITE if perspective == BLACK else BLACK
        own_disks = sum(cell == perspective for row in board for cell in row)
        other_disks = sum(cell == opponent for row in board for cell in row)
        occupied = own_disks + other_disks
        if not cls._legal_moves_on(board, perspective) and not cls._legal_moves_on(
            board, opponent
        ):
            difference = own_disks - other_disks
            return difference * 1000 + (
                100000 if difference > 0 else -100000 if difference < 0 else 0
            )

        disk_weight = 1 if occupied < 24 else 4 if occupied < 48 else 12
        score = (own_disks - other_disks) * disk_weight
        score += sum(
            POSITION_WEIGHTS[row][column]
            for row in range(BOARD_SIZE)
            for column in range(BOARD_SIZE)
            if board[row][column] == perspective
        )
        score -= sum(
            POSITION_WEIGHTS[row][column]
            for row in range(BOARD_SIZE)
            for column in range(BOARD_SIZE)
            if board[row][column] == opponent
        )
        score += 8 * (
            len(cls._legal_moves_on(board, perspective))
            - len(cls._legal_moves_on(board, opponent))
        )
        return score
