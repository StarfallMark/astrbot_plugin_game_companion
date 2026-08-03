from __future__ import annotations

import pytest

from astrbot_plugin_game_companion.pig_dice import PigDiceGame
from astrbot_plugin_game_companion.room_manager import RoomManager


def test_roll_accumulates_until_player_holds() -> None:
    game = PigDiceGame(turn="human")

    assert game.roll("human", value=4) == 4
    assert game.roll("human", value=6) == 6
    assert game.turn_total == 10
    assert game.hold("human") == 10

    assert game.human_score == 10
    assert game.turn == "bot"
    assert game.turn_total == 0


def test_rolling_one_loses_only_the_unbanked_score() -> None:
    game = PigDiceGame(turn="human", human_score=12)
    game.roll("human", value=5)

    game.roll("human", value=1)

    assert game.human_score == 12
    assert game.turn_total == 0
    assert game.turn == "bot"
    assert game.history[-1]["lost"] == 5


def test_holding_at_target_finishes_the_game() -> None:
    game = PigDiceGame(turn="human", human_score=46)
    game.roll("human", value=4)

    game.hold("human")

    assert game.finished
    assert game.winner == "human"
    assert game.human_score == 50


def test_bot_strategy_adjusts_to_style_and_score_pressure() -> None:
    cautious = PigDiceGame(difficulty="easy", turn="bot")
    balanced = PigDiceGame(difficulty="normal", turn="bot")
    bold = PigDiceGame(difficulty="hard", turn="bot")

    assert cautious.bot_hold_threshold() < balanced.bot_hold_threshold()
    assert balanced.bot_hold_threshold() < bold.bot_hold_threshold()

    balanced.human_score = 35
    balanced.bot_score = 15
    assert balanced.bot_hold_threshold() > 15

    balanced.human_score = 10
    balanced.bot_score = 35
    assert balanced.bot_hold_threshold() < 15


def test_bot_always_holds_when_banked_score_would_win() -> None:
    game = PigDiceGame(difficulty="hard", turn="bot", bot_score=47)
    game.roll("bot", value=3)

    assert game.bot_should_hold()


def test_wrong_actor_and_empty_hold_are_rejected() -> None:
    game = PigDiceGame(turn="human")

    with pytest.raises(ValueError, match="轮到"):
        game.roll("bot", value=3)
    with pytest.raises(ValueError, match="没有可以存下"):
        game.hold("human")


@pytest.mark.asyncio
async def test_room_manager_runs_dynamic_bot_turn_without_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict[str, object]]] = []

    async def callback(event: str, _room, payload: dict[str, object]) -> None:
        events.append((event, payload))

    async def no_delay(_seconds: float) -> None:
        return None

    monkeypatch.setattr(
        "astrbot_plugin_game_companion.pig_dice.secrets.randbelow", lambda _n: 3
    )
    monkeypatch.setattr(
        "astrbot_plugin_game_companion.room_manager.asyncio.sleep", no_delay
    )
    manager = RoomManager(event_callback=callback)
    room = await manager.create_room(
        source="private",
        session_id="aiocqhttp:private:10001",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type="pig_dice",
        difficulty="normal",
    )
    visitor = await manager.join(room)
    room.player_token = visitor.token
    room.status = "active"
    room.game = PigDiceGame(turn="human", difficulty="normal")
    room.game.roll("human", value=5)

    await manager.player_dice_action(room, visitor.token, "hold")

    assert isinstance(room.game, PigDiceGame)
    assert room.game.human_score == 5
    assert room.game.bot_score == 16
    assert room.game.turn == "human"
    assert [event for event, _payload in events].count("dice_changed") == 6


@pytest.mark.asyncio
async def test_room_manager_finishes_and_scores_a_pig_game() -> None:
    manager = RoomManager()
    room = await manager.create_room(
        source="private",
        session_id="aiocqhttp:private:10001",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type="pig_dice",
        difficulty="normal",
    )
    visitor = await manager.join(room)
    room.player_token = visitor.token
    room.status = "active"
    room.game = PigDiceGame(turn="human", human_score=47)
    room.game.roll("human", value=3)

    await manager.player_dice_action(room, visitor.token, "hold")

    assert room.status == "finished"
    assert room.completed_games == 1
    assert room.human_wins == 1
    assert room.bot_wins == 0
