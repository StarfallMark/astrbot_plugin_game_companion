from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_game_companion.blackjack import BlackjackCard, BlackjackGame
from astrbot_plugin_game_companion.main import GameCompanionPlugin
from astrbot_plugin_game_companion.room_manager import RoomManager


def make_plugin() -> GameCompanionPlugin:
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.config = {}
    plugin.manager = RoomManager()
    plugin.blackjack_max_players = 1
    plugin.xiangqi_engine = SimpleNamespace(
        allow_download=True,
        auto_download=False,
    )
    plugin._settings_lock = asyncio.Lock()
    return plugin


def test_settings_snapshot_contains_every_game_and_editable_values() -> None:
    plugin = make_plugin()

    snapshot = plugin._game_settings_snapshot()

    games = {item["game_type"]: item for item in snapshot["games"]}
    assert set(games) == {
        "gomoku",
        "xiangqi",
        "tictactoe",
        "turtle_soup",
        "pig_dice",
        "draw_guess",
        "blackjack",
    }
    assert all(item["enabled"] is True for item in games.values())
    pig_fields = {item["key"]: item for item in games["pig_dice"]["fields"]}
    assert pig_fields["target_score"]["value"] == 50
    assert pig_fields["target_score"]["minimum"] == 20
    blackjack_fields = {
        item["key"]: item for item in games["blackjack"]["fields"]
    }
    assert blackjack_fields["max_players"]["value"] == 1
    assert blackjack_fields["max_players"]["maximum"] == 6


@pytest.mark.asyncio
async def test_validated_settings_persist_and_apply_without_reloading_plugin() -> None:
    plugin = make_plugin()
    changes = plugin._validated_game_settings(
        {
            "games": {
                "gomoku": {"enabled": False},
                "pig_dice": {"enabled": True, "target_score": 80},
                "draw_guess": {
                    "enabled": True,
                    "duration_seconds": 180,
                    "max_guesses": 7,
                    "vision_provider_id": "vision-provider",
                },
                "blackjack": {"enabled": True, "max_players": 3},
            }
        }
    )

    await plugin._persist_game_settings(plugin._game_settings_config_patch(changes))
    plugin._apply_game_settings_runtime()

    assert plugin.config["gomoku"]["enabled"] is False
    assert plugin.manager.game_enabled("gomoku") is False
    assert plugin.manager.pig_dice_target_score == 80
    assert plugin.manager.draw_guess_duration_seconds == 180
    assert plugin.manager.draw_guess_max_guesses == 7
    assert plugin.draw_guess_vision_provider_id == "vision-provider"
    assert plugin.manager.blackjack_max_players == 3


@pytest.mark.asyncio
async def test_accepted_blackjack_rematch_starts_a_new_round_in_the_same_room(
    monkeypatch,
) -> None:
    plugin = make_plugin()
    plugin._generate_persona_text = AsyncMock(
        return_value='{"accept": true, "difficulty": "hard", "reply": "再来一局。"}'
    )
    original_deal = BlackjackGame.deal.__func__

    def fixed_deal(*, difficulty, player_numbers, **_kwargs):
        ranks = ["10", "6", "9", "8"]
        suits = ("♠", "♥", "♦", "♣")
        shoe = [
            BlackjackCard(rank=rank, suit=suits[index % 4])
            for index, rank in enumerate(reversed(ranks))
        ]
        return original_deal(
            BlackjackGame,
            difficulty=difficulty,
            player_numbers=player_numbers,
            shoe=shoe,
        )

    monkeypatch.setattr(BlackjackGame, "deal", staticmethod(fixed_deal))
    room = await plugin.manager.create_room(
        source="private",
        session_id="aiocqhttp:private:10001",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type="blackjack",
        difficulty="normal",
    )
    visitor = await plugin.manager.join(room)
    await plugin.manager.claim_and_start(room, visitor.token, "")
    previous_game = room.game
    assert isinstance(previous_game, BlackjackGame)
    previous_game.finished = True
    room.status = "finished"

    await plugin.manager.request_rematch(room, visitor.token)
    await plugin._decide_rematch(room, visitor=visitor)

    assert room.status == "active"
    assert isinstance(room.game, BlackjackGame)
    assert room.game is not previous_game
    assert room.difficulty == "hard"


@pytest.mark.asyncio
@pytest.mark.parametrize("model_reply", ["", "{accept: true}", '{"reply": "再来一局"}'])
async def test_malformed_rematch_decision_never_restarts_room(model_reply: str) -> None:
    plugin = make_plugin()
    plugin._generate_persona_text = AsyncMock(return_value=model_reply)
    room = await plugin.manager.create_room(
        source="private",
        session_id="aiocqhttp:private:10001",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type="gomoku",
        difficulty="normal",
    )
    visitor = await plugin.manager.join(room)
    await plugin.manager.claim_and_start(room, visitor.token, "human_black")
    room.status = "finished"
    await plugin.manager.request_rematch(room, visitor.token)

    await plugin._decide_rematch(room, visitor=visitor)

    assert room.room_id not in plugin.manager.rooms


@pytest.mark.parametrize(
    "payload",
    [
        {"games": {"pig_dice": {"target_score": 19}}},
        {"games": {"draw_guess": {"max_guesses": 11}}},
        {"games": {"turtle_soup": {"content_level": "invalid"}}},
        {"games": {"gomoku": {"enabled": "yes"}}},
        {"games": {"unknown": {"enabled": True}}},
    ],
)
def test_invalid_game_settings_are_rejected_atomically(payload: dict) -> None:
    plugin = make_plugin()

    with pytest.raises(ValueError):
        plugin._validated_game_settings(payload)

    assert plugin.config == {}
