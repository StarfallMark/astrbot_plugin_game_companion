from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Literal

from .gomoku import Difficulty

DiceActor = Literal["human", "bot"]


@dataclass(slots=True)
class PigDiceGame:
    """Authoritative one-die Pig game with a score-aware Bot policy."""

    difficulty: Difficulty = "normal"
    target_score: int = 50
    turn: DiceActor = field(default_factory=lambda: secrets.choice(("human", "bot")))
    human_score: int = 0
    bot_score: int = 0
    turn_total: int = 0
    turn_rolls: int = 0
    last_roll: int | None = None
    winner: DiceActor | None = None
    action_count: int = 0
    history: list[dict[str, object]] = field(default_factory=list)

    @property
    def finished(self) -> bool:
        return self.winner is not None

    @property
    def bot_risk_style(self) -> str:
        return {"easy": "cautious", "normal": "balanced", "hard": "bold"}[
            self.difficulty
        ]

    def roll(self, actor: DiceActor, *, value: int | None = None) -> int:
        self._require_turn(actor)
        rolled = secrets.randbelow(6) + 1 if value is None else int(value)
        if not 1 <= rolled <= 6:
            raise ValueError("骰子点数必须在 1 到 6 之间")
        before = self.turn_total
        self.last_roll = rolled
        if rolled == 1:
            self.turn_total = 0
            self.turn_rolls = 0
            self._append_event(actor, "bust", value=rolled, lost=before)
            self.turn = "bot" if actor == "human" else "human"
            return rolled
        self.turn_total += rolled
        self.turn_rolls += 1
        self._append_event(actor, "roll", value=rolled, lost=0)
        return rolled

    def hold(self, actor: DiceActor) -> int:
        self._require_turn(actor)
        if self.turn_total <= 0:
            raise ValueError("本回合还没有可以存下的分数")
        banked = self.turn_total
        if actor == "human":
            self.human_score += banked
        else:
            self.bot_score += banked
        self.turn_total = 0
        self.turn_rolls = 0
        self.last_roll = None
        if self._score(actor) >= self.target_score:
            self.winner = actor
            self._append_event(actor, "win", value=0, banked=banked)
        else:
            self._append_event(actor, "hold", value=0, banked=banked)
            self.turn = "bot" if actor == "human" else "human"
        return banked

    def resign_human(self) -> None:
        if self.finished:
            raise ValueError("本局已经结束")
        self.turn_total = 0
        self.turn_rolls = 0
        self.winner = "bot"
        self._append_event("human", "resign", value=0)

    def bot_hold_threshold(self) -> int:
        """Return a dynamic target informed by personality style and score pressure."""
        threshold = {"easy": 10, "normal": 15, "hard": 20}[self.difficulty]
        score_gap = self.human_score - self.bot_score
        if score_gap >= 20:
            threshold += 5
        elif score_gap >= 10:
            threshold += 3
        elif score_gap <= -20:
            threshold -= 4
        elif score_gap <= -10:
            threshold -= 2

        human_remaining = self.target_score - self.human_score
        bot_remaining = self.target_score - self.bot_score
        if human_remaining <= 10 and bot_remaining > human_remaining:
            threshold += 3
        if bot_remaining <= 12:
            threshold = min(threshold, bot_remaining)
        return max(7, min(24, threshold))

    def bot_should_hold(self) -> bool:
        if self.finished or self.turn != "bot":
            raise ValueError("现在不是 Bot 的回合")
        if self.bot_score + self.turn_total >= self.target_score:
            return True
        return self.turn_total > 0 and self.turn_total >= self.bot_hold_threshold()

    def snapshot(self) -> dict[str, object]:
        return {
            "kind": "pig_dice",
            "target_score": self.target_score,
            "turn": self.turn,
            "human_score": self.human_score,
            "bot_score": self.bot_score,
            "turn_total": self.turn_total,
            "turn_rolls": self.turn_rolls,
            "last_roll": self.last_roll,
            "winner": self.winner,
            "finished": self.finished,
            "risk_style": self.bot_risk_style,
            "bot_hold_threshold": self.bot_hold_threshold(),
            "action_count": self.action_count,
            "history": self.history[-24:],
        }

    def _require_turn(self, actor: DiceActor) -> None:
        if self.finished:
            raise ValueError("本局已经结束")
        if actor != self.turn:
            raise ValueError("现在还没有轮到这一方")

    def _score(self, actor: DiceActor) -> int:
        return self.human_score if actor == "human" else self.bot_score

    def _append_event(
        self,
        actor: DiceActor,
        action: str,
        *,
        value: int,
        lost: int = 0,
        banked: int = 0,
    ) -> None:
        self.action_count += 1
        self.history.append(
            {
                "sequence": self.action_count,
                "actor": actor,
                "action": action,
                "value": value,
                "lost": lost,
                "banked": banked,
                "turn_total": self.turn_total,
                "turn_rolls": self.turn_rolls,
                "human_score": self.human_score,
                "bot_score": self.bot_score,
            }
        )
        del self.history[:-60]
