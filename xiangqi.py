from __future__ import annotations

from dataclasses import dataclass, field

from .gomoku import Difficulty
from .pikafish import MOVE_PATTERN, PikafishService

RED = "red"
BLACK = "black"
XiangqiSide = str

INITIAL_BOARD = (
    "rnbakabnr",
    ".........",
    ".c.....c.",
    "p.p.p.p.p",
    ".........",
    ".........",
    "P.P.P.P.P",
    ".C.....C.",
    ".........",
    "RNBAKABNR",
)


@dataclass(slots=True)
class XiangqiGame:
    """Authoritative Xiangqi state validated and played by Pikafish."""

    human_side: XiangqiSide = RED
    difficulty: Difficulty = "normal"
    moves: list[str] = field(default_factory=list)
    legal_moves: list[str] = field(default_factory=list)
    winner: XiangqiSide | None = None
    draw: bool = False
    halfmove_clock: int = 0
    _position_counts: dict[str, int] = field(default_factory=dict, repr=False)

    @property
    def bot_side(self) -> XiangqiSide:
        return BLACK if self.human_side == RED else RED

    @property
    def turn(self) -> XiangqiSide:
        return RED if len(self.moves) % 2 == 0 else BLACK

    @property
    def finished(self) -> bool:
        return self.winner is not None or self.draw

    @property
    def history(self) -> list[str]:
        return self.moves

    @classmethod
    async def create(
        cls,
        engine: PikafishService,
        *,
        human_side: XiangqiSide,
        difficulty: Difficulty,
    ) -> XiangqiGame:
        game = cls(human_side=human_side, difficulty=difficulty)
        game.legal_moves = await engine.legal_moves([])
        game._record_position()
        return game

    async def place_human(
        self,
        engine: PikafishService,
        from_row: int,
        from_column: int,
        to_row: int,
        to_column: int,
    ) -> str:
        if self.finished or self.turn != self.human_side:
            raise ValueError("当前不是玩家的象棋回合")
        move = self.coordinates_to_iccs(
            from_row, from_column, to_row, to_column
        )
        if move not in self.legal_moves:
            raise ValueError("这枚棋子不能走到该位置")
        await self._apply(engine, move)
        return move

    async def place_bot(self, engine: PikafishService) -> str:
        if self.finished or self.turn != self.bot_side:
            raise ValueError("当前不是 Bot 的象棋回合")
        move = await engine.choose_move(self.moves, self.difficulty)
        if move not in self.legal_moves:
            raise RuntimeError("Pikafish 返回了当前局面的非法着法")
        await self._apply(engine, move)
        return move

    async def undo_round(self, engine: PikafishService) -> int:
        if not self.moves:
            raise ValueError("没有可以撤销的玩家着法")
        human_parity = 0 if self.human_side == RED else 1
        human_index = next(
            (
                index
                for index in range(len(self.moves) - 1, -1, -1)
                if index % 2 == human_parity
            ),
            -1,
        )
        if human_index < 0:
            raise ValueError("没有可以撤销的玩家着法")
        removed = len(self.moves) - human_index
        del self.moves[human_index:]
        self.winner = None
        self.draw = False
        self._rebuild_position_counts()
        self._rebuild_halfmove_clock()
        self.legal_moves = await engine.legal_moves(self.moves)
        return removed

    def snapshot(self) -> dict[str, object]:
        return {
            "kind": "xiangqi",
            "board": self.board(),
            "turn": self.turn,
            "human_side": self.human_side,
            "bot_side": self.bot_side,
            "winner": self.winner,
            "draw": self.draw,
            "move_count": len(self.moves),
            "last_move": self.iccs_to_coordinates(self.moves[-1]) if self.moves else None,
            "legal_moves": [
                self.iccs_to_coordinates(move)
                for move in self.legal_moves
                if self.turn == self.human_side and not self.finished
            ],
        }

    def board(self) -> list[list[str]]:
        board = [list(row) for row in INITIAL_BOARD]
        for move in self.moves:
            from_row, from_column, to_row, to_column = self.iccs_to_coordinates(move)
            board[to_row][to_column] = board[from_row][from_column]
            board[from_row][from_column] = "."
        return board

    def tactical_state(self, _side: object = None) -> str:
        if self.winner is not None:
            return "win"
        if not self.moves:
            return ""
        board_before = self._board_before_last_move()
        _from_row, _from_column, to_row, to_column = self.iccs_to_coordinates(
            self.moves[-1]
        )
        captured = board_before[to_row][to_column]
        return "major_capture" if captured.lower() in {"r", "c", "n"} else ""

    async def _apply(self, engine: PikafishService, move: str) -> None:
        board_before = self.board()
        _from_row, _from_column, to_row, to_column = self.iccs_to_coordinates(move)
        captured = board_before[to_row][to_column] != "."
        self.moves.append(move)
        try:
            legal = await engine.legal_moves(self.moves)
        except Exception:
            self.moves.pop()
            raise
        self.legal_moves = legal
        self.halfmove_clock = 0 if captured else self.halfmove_clock + 1
        if not legal:
            self.winner = BLACK if self.turn == RED else RED
            return
        position_count = self._record_position()
        if position_count >= 3 or self.halfmove_clock >= 120:
            self.draw = True

    def _record_position(self) -> int:
        key = "".join("".join(row) for row in self.board()) + ":" + self.turn
        count = self._position_counts.get(key, 0) + 1
        self._position_counts[key] = count
        return count

    def _rebuild_position_counts(self) -> None:
        moves = list(self.moves)
        self._position_counts.clear()
        self.moves.clear()
        self._record_position()
        for move in moves:
            self.moves.append(move)
            self._record_position()

    def _rebuild_halfmove_clock(self) -> None:
        board = [list(row) for row in INITIAL_BOARD]
        clock = 0
        for move in self.moves:
            from_row, from_column, to_row, to_column = self.iccs_to_coordinates(move)
            captured = board[to_row][to_column] != "."
            board[to_row][to_column] = board[from_row][from_column]
            board[from_row][from_column] = "."
            clock = 0 if captured else clock + 1
        self.halfmove_clock = clock

    def _board_before_last_move(self) -> list[list[str]]:
        if not self.moves:
            return [list(row) for row in INITIAL_BOARD]
        move = self.moves.pop()
        try:
            return self.board()
        finally:
            self.moves.append(move)

    @staticmethod
    def coordinates_to_iccs(
        from_row: int,
        from_column: int,
        to_row: int,
        to_column: int,
    ) -> str:
        coordinates = (from_row, from_column, to_row, to_column)
        if any(not isinstance(value, int) for value in coordinates):
            raise ValueError("象棋坐标必须是整数")
        if not (0 <= from_row < 10 and 0 <= to_row < 10):
            raise ValueError("象棋纵坐标超出棋盘")
        if not (0 <= from_column < 9 and 0 <= to_column < 9):
            raise ValueError("象棋横坐标超出棋盘")
        return (
            f"{chr(97 + from_column)}{9 - from_row}"
            f"{chr(97 + to_column)}{9 - to_row}"
        )

    @staticmethod
    def iccs_to_coordinates(move: str) -> list[int]:
        if not MOVE_PATTERN.fullmatch(move):
            raise ValueError("无效的 ICCS 象棋着法")
        return [9 - int(move[1]), ord(move[0]) - 97, 9 - int(move[3]), ord(move[2]) - 97]
