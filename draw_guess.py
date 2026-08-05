from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .gomoku import Difficulty


@dataclass(frozen=True, slots=True)
class DrawWord:
    answer: str
    aliases: tuple[str, ...] = ()


WORD_BANK: dict[Difficulty, tuple[DrawWord, ...]] = {
    "easy": (
        DrawWord("苹果", ("apple",)),
        DrawWord("太阳", ("太阳公公",)),
        DrawWord("雨伞", ("伞",)),
        DrawWord("猫", ("小猫",)),
        DrawWord("自行车", ("单车",)),
        DrawWord("蛋糕"),
        DrawWord("房子", ("房屋",)),
        DrawWord("飞机"),
    ),
    "normal": (
        DrawWord("摩天轮"),
        DrawWord("潜水艇"),
        DrawWord("机器人"),
        DrawWord("露营", ("野营",)),
        DrawWord("望远镜"),
        DrawWord("火山"),
        DrawWord("魔法帽"),
        DrawWord("热气球"),
        DrawWord("迷宫"),
        DrawWord("音乐会"),
    ),
    "hard": (
        DrawWord("时间机器", ("时光机",)),
        DrawWord("海市蜃楼"),
        DrawWord("回形针"),
        DrawWord("纸上谈兵"),
        DrawWord("蝴蝶效应"),
        DrawWord("守株待兔"),
        DrawWord("引力波"),
        DrawWord("南柯一梦"),
        DrawWord("量子纠缠"),
        DrawWord("破镜重圆"),
    ),
}


def _normalize_answer(value: Any) -> str:
    return re.sub(r"[\s\W_]+", "", str(value or "").strip().lower())


@dataclass(slots=True)
class DrawGuessGame:
    """Server-authoritative cooperative drawing game.

    The answer never leaves this object until ``snapshot(reveal_answer=True)`` is
    requested for the bound drawer or a finished room.
    """

    difficulty: Difficulty = "normal"
    max_guesses: int = 5
    duration_seconds: int = 120
    target: DrawWord | None = None
    started_at: float = field(default_factory=time.time)
    strokes: list[dict[str, Any]] = field(default_factory=list)
    guesses: list[dict[str, Any]] = field(default_factory=list)
    processing: bool = False
    finished: bool = False
    solved: bool = False
    timed_out: bool = False
    revision: int = 0
    paused_at: float = 0.0

    def __post_init__(self) -> None:
        self.max_guesses = max(1, min(int(self.max_guesses), 10))
        self.duration_seconds = max(10, min(int(self.duration_seconds), 600))
        if self.target is None:
            choices = WORD_BANK.get(self.difficulty, WORD_BANK["normal"])
            self.target = random.choice(choices)

    @property
    def answer(self) -> str:
        return self.target.answer if self.target else ""

    @property
    def deadline(self) -> float:
        return self.started_at + self.duration_seconds

    @property
    def remaining_seconds(self) -> int:
        current = self.paused_at or time.time()
        return max(0, int(self.deadline - current + 0.999))

    def is_expired(self, now: float | None = None) -> bool:
        current = time.time() if now is None else float(now)
        return current >= self.deadline if not self.paused_at else False

    def pause(self) -> None:
        if not self.finished and not self.paused_at:
            self.paused_at = time.time()

    def resume(self) -> None:
        if self.paused_at:
            self.started_at += time.time() - self.paused_at
            self.paused_at = 0.0

    def replace_strokes(self, strokes: list[dict[str, Any]]) -> None:
        if self.finished:
            raise ValueError("本局已经结束，不能修改画布")
        self.strokes = strokes
        self.revision += 1

    def matches(self, guess: str) -> bool:
        normalized = _normalize_answer(guess)
        if not normalized or self.target is None:
            return False
        return normalized in {
            _normalize_answer(self.target.answer),
            *(_normalize_answer(item) for item in self.target.aliases),
        }

    def record_guess(self, guess: str, *, correct: bool) -> dict[str, Any]:
        if self.finished:
            raise ValueError("本局已经结束")
        cleaned = re.sub(r"\s+", " ", str(guess or "").strip())[:80]
        if not cleaned:
            raise ValueError("Bot 没有给出有效猜测")
        item = {
            "guess": cleaned,
            "correct": bool(correct),
            "number": len(self.guesses) + 1,
            "at": time.time(),
        }
        self.guesses.append(item)
        if correct:
            self.solved = True
            self.finished = True
        elif len(self.guesses) >= self.max_guesses:
            self.finished = True
        return item

    def timeout(self) -> None:
        if not self.finished:
            self.timed_out = True
            self.finished = True

    def snapshot(self, *, reveal_answer: bool = False) -> dict[str, object]:
        result: dict[str, object] = {
            "type": "draw_guess",
            "difficulty": self.difficulty,
            "strokes": self.strokes,
            "revision": self.revision,
            "guesses": list(self.guesses),
            "guess_count": len(self.guesses),
            "max_guesses": self.max_guesses,
            "processing": self.processing,
            "finished": self.finished,
            "solved": self.solved,
            "timed_out": self.timed_out,
            "started_at": self.started_at,
            "deadline": self.deadline,
            "remaining_seconds": self.remaining_seconds,
            "paused": bool(self.paused_at),
        }
        if reveal_answer or self.finished:
            result["answer"] = self.answer
        return result
