from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from .gomoku import Difficulty

SoupContentLevel = Literal["all_ages", "normal", "unrestricted"]
SoupVerdict = Literal["yes", "no", "irrelevant", "partial", "compound"]
SoupMode = Literal["bot_host", "player_host"]
SoupEntryKind = Literal["question", "answer", "hint", "reverse"]
SoupBotAction = Literal["question", "guess"]


VERDICT_LABELS: dict[SoupVerdict, str] = {
    "yes": "是",
    "no": "否",
    "irrelevant": "无关",
    "partial": "部分正确",
    "compound": "请一次只问一个问题",
}


@dataclass(slots=True, frozen=True)
class SoupPuzzle:
    title: str
    surface: str
    solution: str
    key_facts: tuple[str, ...]
    acceptable_variants: tuple[str, ...]
    hints: tuple[str, ...]
    content_level: SoupContentLevel
    theme: str
    trick: str

    @property
    def signature(self) -> str:
        return f"{self.theme.strip().lower()}|{self.trick.strip().lower()}"[:180]

    def public_snapshot(self, *, reveal_solution: bool) -> dict[str, object]:
        data: dict[str, object] = {
            "title": self.title,
            "surface": self.surface,
            "content_level": self.content_level,
        }
        if reveal_solution:
            data["solution"] = self.solution
            data["key_facts"] = list(self.key_facts)
        return data


@dataclass(slots=True)
class SoupEntry:
    kind: SoupEntryKind
    prompt: str
    response: str
    source: Literal["web", "qq", "system"]
    verdict: SoupVerdict | None = None
    player_number: int | None = None
    bot_action: SoupBotAction | None = None
    at: float = field(default_factory=time.time)

    def snapshot(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "prompt": self.prompt,
            "response": self.response,
            "source": self.source,
            "verdict": self.verdict,
            "player_number": self.player_number,
            "bot_action": self.bot_action,
            "at": self.at,
        }


@dataclass(slots=True)
class TurtleSoupGame:
    difficulty: Difficulty
    max_hints: int
    content_level: SoupContentLevel
    mode: SoupMode = "bot_host"
    puzzle: SoupPuzzle | None = None
    phase: Literal["preparing", "ready", "finished"] = "preparing"
    processing: bool = False
    processing_player_number: int | None = None
    entries: list[SoupEntry] = field(default_factory=list)
    discovered_facts: set[int] = field(default_factory=set)
    question_count: int = 0
    answer_attempts: int = 0
    hints_used: int = 0
    solved: bool = False
    gave_up: bool = False
    bot_solved: bool = False
    turn_count: int = 0
    last_bot_action: SoupBotAction | None = None
    last_bot_text: str = ""
    failure_reason: str = ""

    def __post_init__(self) -> None:
        if self.mode == "player_host" and self.phase == "preparing":
            self.phase = "ready"

    @property
    def finished(self) -> bool:
        return self.phase == "finished"

    @property
    def draw(self) -> bool:
        return False

    @property
    def winner(self) -> str:
        if not self.finished:
            return ""
        if self.mode == "player_host":
            return "bot"
        return "human" if self.solved else "bot"

    @property
    def history(self) -> list[SoupEntry]:
        return self.entries

    def set_puzzle(self, puzzle: SoupPuzzle) -> None:
        if self.mode != "bot_host":
            raise ValueError("玩家出题模式不使用 Bot 题库")
        if self.phase != "preparing":
            raise ValueError("当前海龟汤已经完成出题")
        self.puzzle = puzzle
        self.phase = "ready"
        self.failure_reason = ""

    def begin_processing(self) -> None:
        if self.phase == "preparing":
            raise ValueError("Bot 还在准备题目")
        if self.phase == "finished":
            raise ValueError("本题已经结束")
        if self.processing:
            raise ValueError("Bot 正在判断上一条内容")
        if self.mode == "bot_host" and self.puzzle is None:
            raise ValueError("题目尚未准备完成")
        self.processing = True

    def cancel_processing(self, reason: str = "") -> None:
        self.processing = False
        self.processing_player_number = None
        self.failure_reason = str(reason or "")[:160]

    def record_question(
        self,
        question: str,
        verdict: SoupVerdict,
        *,
        source: Literal["web", "qq"],
        matched_facts: set[int] | None = None,
        player_number: int | None = None,
    ) -> set[int]:
        if self.phase != "ready":
            raise ValueError("当前不能继续提问")
        newly_discovered = set(matched_facts or ()) - self.discovered_facts
        self.discovered_facts.update(matched_facts or ())
        self.question_count += 1
        self.entries.append(
            SoupEntry(
                kind="question",
                prompt=question,
                response=VERDICT_LABELS[verdict],
                source=source,
                verdict=verdict,
                player_number=player_number,
            )
        )
        del self.entries[:-80]
        self.processing = False
        self.processing_player_number = None
        self.failure_reason = ""
        return newly_discovered

    def record_answer(
        self,
        answer: str,
        *,
        solved: bool,
        source: Literal["web", "qq"],
        matched_facts: set[int] | None = None,
        player_number: int | None = None,
    ) -> set[int]:
        if self.phase != "ready":
            raise ValueError("当前不能提交答案")
        newly_discovered = set(matched_facts or ()) - self.discovered_facts
        self.discovered_facts.update(matched_facts or ())
        self.answer_attempts += 1
        response = (
            "推理正确，汤底已经揭晓。" if solved else "已经接近了，但还缺少关键环节。"
        )
        self.entries.append(
            SoupEntry(
                kind="answer",
                prompt=answer,
                response=response,
                source=source,
                player_number=player_number,
            )
        )
        del self.entries[:-80]
        self.processing = False
        self.processing_player_number = None
        self.failure_reason = ""
        if solved:
            self.solved = True
            self.phase = "finished"
        return newly_discovered

    def reveal_hint(
        self, *, source: Literal["web", "qq"], player_number: int | None = None
    ) -> str:
        if self.phase != "ready" or self.puzzle is None:
            raise ValueError("当前不能申请提示")
        allowed = len(self.puzzle.hints)
        if self.max_hints:
            allowed = min(allowed, self.max_hints)
        if self.hints_used >= allowed:
            raise ValueError("本题已经没有更多提示")
        hint = self.puzzle.hints[self.hints_used]
        self.hints_used += 1
        self.entries.append(
            SoupEntry(
                kind="hint",
                prompt="申请提示",
                response=hint,
                source=source,
                player_number=player_number,
            )
        )
        del self.entries[:-80]
        return hint

    def give_up(self, *, source: Literal["web", "qq"] = "qq") -> None:
        if self.phase != "ready" or (self.mode == "bot_host" and self.puzzle is None):
            raise ValueError("当前没有可以放弃的海龟汤")
        self.gave_up = True
        self.phase = "finished"
        self.processing = False
        self.processing_player_number = None
        self.entries.append(
            SoupEntry(
                kind="answer",
                prompt=(
                    "结束玩家出题" if self.mode == "player_host" else "放弃并查看汤底"
                ),
                response=(
                    "本题已结束。"
                    if self.mode == "player_host"
                    else "本题已结束，汤底已经揭晓。"
                ),
                source=source,
            )
        )
        del self.entries[:-80]

    def record_reverse_turn(
        self,
        player_text: str,
        *,
        bot_action: SoupBotAction,
        bot_text: str,
        source: Literal["web", "qq"],
        player_number: int | None = None,
    ) -> None:
        if self.mode != "player_host" or self.phase != "ready":
            raise ValueError("当前不是玩家出题回合")
        self.turn_count += 1
        if bot_action == "question":
            self.question_count += 1
        else:
            self.answer_attempts += 1
        self.last_bot_action = bot_action
        self.last_bot_text = bot_text
        self.entries.append(
            SoupEntry(
                kind="reverse",
                prompt=player_text,
                response=bot_text,
                source=source,
                player_number=player_number,
                bot_action=bot_action,
            )
        )
        del self.entries[:-80]
        self.processing = False
        self.processing_player_number = None
        self.failure_reason = ""

    def confirm_bot_guess(self, *, correct: bool) -> None:
        if self.mode != "player_host" or self.phase != "ready":
            raise ValueError("当前不能判定 Bot 的猜测")
        if self.last_bot_action != "guess":
            raise ValueError("Bot 目前还没有提交可判定的猜测")
        if correct:
            self.bot_solved = True
            self.solved = False
            self.phase = "finished"
        self.processing = False
        self.processing_player_number = None

    def snapshot(self) -> dict[str, object]:
        reveal_solution = self.finished and self.puzzle is not None
        available_hints = len(self.puzzle.hints) if self.puzzle else 0
        hint_limit = (
            min(self.max_hints, available_hints) if self.max_hints else available_hints
        )
        return {
            "kind": "turtle_soup",
            "mode": self.mode,
            "phase": self.phase,
            "preparing": self.phase == "preparing",
            "processing": self.processing,
            "failure_reason": self.failure_reason,
            "puzzle": self.puzzle.public_snapshot(reveal_solution=reveal_solution)
            if self.puzzle
            else None,
            "entries": [entry.snapshot() for entry in self.entries[-60:]],
            "question_count": self.question_count,
            "answer_attempts": self.answer_attempts,
            "hints_used": self.hints_used,
            "hint_limit": hint_limit,
            "discovered_fact_count": len(self.discovered_facts),
            "key_fact_count": len(self.puzzle.key_facts) if self.puzzle else 0,
            "solved": self.solved,
            "gave_up": self.gave_up,
            "bot_solved": self.bot_solved,
            "turn_count": self.turn_count,
            "last_bot_action": self.last_bot_action,
            "last_bot_text": self.last_bot_text,
            "finished": self.finished,
        }


def normalize_content_level(value: Any) -> SoupContentLevel:
    normalized = str(value or "normal").strip().lower()
    aliases = {
        "all_ages": "all_ages",
        "all-ages": "all_ages",
        "全年龄": "all_ages",
        "normal": "normal",
        "普通": "normal",
        "unrestricted": "unrestricted",
        "不限制": "unrestricted",
    }
    return aliases.get(normalized, "normal")  # type: ignore[return-value]


def puzzle_from_mapping(
    data: dict[str, Any], *, content_level: SoupContentLevel
) -> SoupPuzzle:
    title = _text(data.get("title"), 80)
    surface = _text(data.get("surface"), 800)
    solution = _text(data.get("solution"), 1600)
    key_facts = _text_items(data.get("key_facts"), maximum=8, item_limit=260)
    variants = _text_items(data.get("acceptable_variants"), maximum=8, item_limit=260)
    hints = _text_items(data.get("hints"), maximum=8, item_limit=260)
    theme = _text(data.get("theme"), 80)
    trick = _text(data.get("trick"), 160)
    if len(title) < 2:
        raise ValueError("题目标题过短")
    if len(surface) < 20:
        raise ValueError("汤面信息不足")
    if len(solution) < 30:
        raise ValueError("汤底信息不足")
    if len(key_facts) < 3:
        raise ValueError("关键事实不足")
    if len(hints) < 3:
        raise ValueError("渐进提示不足")
    if not theme or not trick:
        raise ValueError("题目缺少主题或核心诡计标记")
    return SoupPuzzle(
        title=title,
        surface=surface,
        solution=solution,
        key_facts=tuple(key_facts),
        acceptable_variants=tuple(variants),
        hints=tuple(hints),
        content_level=content_level,
        theme=theme,
        trick=trick,
    )


def fallback_puzzle(
    *,
    content_level: SoupContentLevel,
    excluded_signatures: set[str] | None = None,
) -> SoupPuzzle:
    excluded = excluded_signatures or set()
    candidates = [
        puzzle_from_mapping(
            item,
            content_level=normalize_content_level(item.get("content_level")),
        )
        for item in _FALLBACK_PUZZLES
        if item["content_level"] == "all_ages" or content_level != "all_ages"
    ]
    available = [item for item in candidates if item.signature not in excluded]
    return secrets.choice(available or candidates)


def clean_player_text(value: Any, *, limit: int) -> str:
    text = " ".join(str(value or "").strip().split())
    if not text:
        raise ValueError("内容不能为空")
    if len(text) > limit:
        raise ValueError(f"内容不能超过 {limit} 个字符")
    return text


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").strip().split())[:limit]


def _text_items(value: Any, *, maximum: int, item_limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = _text(item, item_limit)
        if text and text not in result:
            result.append(text)
        if len(result) >= maximum:
            break
    return result


_FALLBACK_PUZZLES: tuple[dict[str, Any], ...] = (
    {
        "content_level": "all_ages",
        "title": "没有寄出的明信片",
        "surface": "小林旅行回来后，把一张写好的明信片放进抽屉。他没有寄出它，却因此确认朋友已经收到了自己的祝福。为什么？",
        "solution": "小林和朋友约定，各自在旅行地写一张明信片，但不邮寄，而是在回家后视频展示。朋友在视频中读出了小林明信片背面的内容，所以小林确认祝福已经通过视频传达；明信片本身仍留在抽屉里。",
        "key_facts": [
            "祝福并非依靠邮寄传达",
            "两人事先约定通过视频展示",
            "朋友已经在视频中读到明信片内容",
        ],
        "acceptable_variants": ["通过照片、直播或视频通话看到了明信片内容也可视为等价"],
        "hints": [
            "重点不是邮局出了问题。",
            "朋友看到了信息，但没有拿到那张纸。",
            "两人使用了实时影像交流。",
        ],
        "theme": "旅行与通讯",
        "trick": "收到祝福不等于收到实体明信片",
    },
    {
        "content_level": "all_ages",
        "title": "最后一名的奖牌",
        "surface": "阿远在比赛中最后一个到达终点，却获得了唯一一枚奖牌，而且所有参赛者都认为公平。为什么？",
        "solution": "这是一场接力式的公益徒步，奖牌交给最后一棒的代表，由他代全队领取。阿远是整支队伍最后通过终点的人，也是被事先指定的领奖代表；比赛并不按个人到达顺序排名。",
        "key_facts": [
            "比赛不是个人竞速排名",
            "阿远代表一个团队",
            "唯一奖牌属于全队并由最后一棒代领",
        ],
        "acceptable_variants": ["团体完赛纪念活动、非竞速接力等设定可视为等价"],
        "hints": [
            "最后到达不一定代表成绩最差。",
            "奖牌并不只属于阿远个人。",
            "这是团体完成的接力活动。",
        ],
        "theme": "体育与团队",
        "trick": "把团体完赛误认为个人竞速",
    },
    {
        "content_level": "normal",
        "title": "每天准时的空椅子",
        "surface": "餐馆每天打烊前都会摆出一把空椅子。某天店员忘了摆，老板立刻报警，警方也认为他的决定合理。为什么？",
        "solution": "餐馆与附近独居老人约定：老人每天散步时会把门口的折叠椅搬到指定位置，表示自己平安；店员打烊前再把椅子收回。那天椅子仍在店内，说明老人没有按约出现。老板联系不上老人后报警，警方上门发现老人突发疾病并及时救助。",
        "key_facts": [
            "空椅子是约定好的平安信号",
            "平时由附近老人移动椅子",
            "当天信号没有出现且老人失联",
            "报警目的是确认并救助老人",
        ],
        "acceptable_variants": ["其他通过日常物品确认独居者平安的约定可视为等价"],
        "hints": [
            "椅子不是为顾客准备的。",
            "它的位置代表某个人当天是否出现。",
            "这是餐馆与独居老人约定的平安信号。",
        ],
        "theme": "社区与平安确认",
        "trick": "空椅子是一种非语言信号",
    },
)
