from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from astrbot_plugin_game_companion.main import GameCompanionPlugin
from astrbot_plugin_game_companion.room_manager import RoomManager
from astrbot_plugin_game_companion.server import GameRoomServer
from astrbot_plugin_game_companion.turtle_soup import (
    TurtleSoupGame,
    fallback_puzzle,
    puzzle_from_mapping,
)
from astrbot_plugin_game_companion.turtle_soup_ai import (
    parse_answer_judgment,
    parse_question_judgment,
)


def puzzle_data() -> dict[str, object]:
    return {
        "title": "测试题",
        "surface": "一个人每天都在同一时间打开一扇没有上锁的门，但从不走进去。为什么？",
        "solution": "那是一间温室的通风门。他按时打开门是为了给植物换气，入口在另一侧，所以不从这里进入。",
        "key_facts": ["门用于温室通风", "他在照料植物", "真正入口位于另一侧"],
        "acceptable_variants": ["把温室描述为需要通风的种植房也可以"],
        "hints": [
            "门的用途不只是通行。",
            "这件事与室内环境有关。",
            "房间里主要是植物。",
        ],
        "theme": "植物照料",
        "trick": "把通风门误认为普通入口",
    }


def make_puzzle():
    return puzzle_from_mapping(puzzle_data(), content_level="all_ages")


async def make_soup_room(manager: RoomManager):
    room = await manager.create_room(
        source="private",
        session_id="aiocqhttp:private:10001",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type="turtle_soup",
        difficulty="normal",
    )
    visitor = await manager.join(room)
    await manager.claim_and_start(room, visitor.token, "")
    assert isinstance(room.game, TurtleSoupGame)
    return room, visitor, room.game


def test_active_snapshot_never_exposes_solution_or_key_facts() -> None:
    game = TurtleSoupGame(difficulty="normal", max_hints=3, content_level="all_ages")
    game.set_puzzle(make_puzzle())

    active = game.snapshot()
    assert active["puzzle"] == {
        "title": "测试题",
        "surface": puzzle_data()["surface"],
        "content_level": "all_ages",
    }

    game.give_up(source="web")
    finished = game.snapshot()
    assert finished["puzzle"]["solution"] == puzzle_data()["solution"]
    assert finished["puzzle"]["key_facts"] == puzzle_data()["key_facts"]


def test_judge_parsers_reject_unknown_verdict_and_overconfident_answer() -> None:
    verdict, facts = parse_question_judgment(
        '{"verdict":"maybe","matched_facts":[0,9,"1"]}', fact_count=3
    )
    solved, coverage, answer_facts = parse_answer_judgment(
        '{"solved":true,"coverage":0.7,"matched_facts":[0,1,2]}', fact_count=3
    )

    assert verdict == "irrelevant"
    assert facts == {0, 1}
    assert not solved
    assert coverage == 0.7
    assert answer_facts == {0, 1, 2}


@pytest.mark.asyncio
async def test_soup_start_is_async_and_generation_is_not_idle_expired() -> None:
    events: list[str] = []

    async def callback(event, _room, _payload):
        events.append(event)

    manager = RoomManager(idle_timeout=1, event_callback=callback)
    room, _visitor, game = await make_soup_room(manager)
    room.last_activity_at = 1.0

    assert game.phase == "preparing"
    assert events == ["soup_generation_requested"]
    assert await manager.sweep_expired(now=100.0) == []


@pytest.mark.asyncio
async def test_only_current_player_can_ask_and_questions_are_shared() -> None:
    manager = RoomManager()
    room, visitor, game = await make_soup_room(manager)
    await manager.complete_turtle_soup_generation(room, game, make_puzzle())
    spectator = await manager.join(room)

    with pytest.raises(PermissionError, match="当前玩家"):
        await manager.begin_turtle_soup_interaction(
            room,
            "这是温室吗？",
            source="web",
            visitor_token=spectator.token,
            limit=200,
        )

    active_game, question = await manager.begin_turtle_soup_interaction(
        room,
        "这是温室吗？",
        source="web",
        visitor_token=visitor.token,
        limit=200,
    )
    await manager.resolve_turtle_soup_question(
        room,
        active_game,
        question,
        "yes",
        source="web",
        matched_facts={0},
    )

    snapshot = room.public_snapshot(spectator.token)
    assert snapshot["game"]["question_count"] == 1
    assert snapshot["game"]["entries"][0]["response"] == "是"
    assert snapshot["game"]["discovered_fact_count"] == 1


@pytest.mark.asyncio
async def test_hints_are_progressive_and_honor_configured_limit() -> None:
    manager = RoomManager(turtle_soup_max_hints=2)
    room, visitor, game = await make_soup_room(manager)
    await manager.complete_turtle_soup_generation(room, game, make_puzzle())

    first = await manager.request_turtle_soup_hint(
        room, source="web", visitor_token=visitor.token
    )
    second = await manager.request_turtle_soup_hint(
        room, source="web", visitor_token=visitor.token
    )

    assert first != second
    assert game.snapshot()["hint_limit"] == 2
    with pytest.raises(ValueError, match="没有更多提示"):
        await manager.request_turtle_soup_hint(
            room, source="web", visitor_token=visitor.token
        )


@pytest.mark.asyncio
async def test_solving_updates_only_turtle_soup_score_and_stats() -> None:
    manager = RoomManager()
    room, visitor, game = await make_soup_room(manager)
    await manager.complete_turtle_soup_generation(room, game, make_puzzle())
    await manager.request_turtle_soup_hint(
        room, source="web", visitor_token=visitor.token
    )
    active_game, answer = await manager.begin_turtle_soup_interaction(
        room,
        "这是温室的通风门，他在照料植物，真正入口在另一边。",
        source="web",
        visitor_token=visitor.token,
        limit=800,
    )
    await manager.resolve_turtle_soup_answer(
        room,
        active_game,
        answer,
        solved=True,
        source="web",
        matched_facts={0, 1, 2},
    )

    assert room.status == "finished"
    assert room.scores["turtle_soup"].completed == 1
    assert room.scores["turtle_soup"].human_wins == 1
    assert room.scores["gomoku"].completed == 0
    assert room.turtle_soup_stats.hints == 1
    assert room.turtle_soup_stats.answer_attempts == 1


@pytest.mark.asyncio
async def test_rematch_keeps_room_but_returns_to_fresh_generation() -> None:
    manager = RoomManager()
    room, _visitor, game = await make_soup_room(manager)
    puzzle = make_puzzle()
    await manager.complete_turtle_soup_generation(room, game, puzzle)
    game.give_up()
    await manager._finish_game(room)
    room_id = room.room_id
    access_token = room.access_token

    await manager.restart_finished_game(room, difficulty="hard")

    assert room.room_id == room_id
    assert room.access_token == access_token
    assert isinstance(room.game, TurtleSoupGame)
    assert room.game is not game
    assert room.game.phase == "preparing"
    assert room.difficulty == "hard"
    assert puzzle.signature in room.turtle_soup_recent_signatures


@pytest.mark.asyncio
async def test_plugin_uses_model_generation_then_hidden_validation() -> None:
    generated = puzzle_data()
    provider = SimpleNamespace(
        text_chat=AsyncMock(
            side_effect=[
                SimpleNamespace(
                    completion_text=json.dumps(generated, ensure_ascii=False)
                ),
                SimpleNamespace(completion_text='{"valid":true,"reason":"自洽"}'),
            ]
        )
    )
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.context = SimpleNamespace(
        get_using_provider=lambda _session: provider,
        persona_manager=None,
    )
    plugin.manager = RoomManager()
    room, _visitor, game = await make_soup_room(plugin.manager)

    await plugin._prepare_turtle_soup(room, game)

    assert game.phase == "ready"
    assert game.puzzle is not None
    assert game.puzzle.title == "测试题"
    assert provider.text_chat.await_count == 2


@pytest.mark.asyncio
async def test_plugin_falls_back_when_room_has_no_model() -> None:
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.context = SimpleNamespace(
        get_using_provider=lambda _session: None,
        persona_manager=None,
    )
    plugin.manager = RoomManager(turtle_soup_content_level="all_ages")
    room, _visitor, game = await make_soup_room(plugin.manager)

    await plugin._prepare_turtle_soup(room, game)

    assert game.phase == "ready"
    assert game.puzzle is not None
    assert game.puzzle.content_level == "all_ages"


@pytest.mark.asyncio
async def test_failed_judgment_releases_busy_state_without_counting_question() -> None:
    provider = SimpleNamespace(text_chat=AsyncMock(side_effect=RuntimeError("offline")))
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.context = SimpleNamespace(get_using_provider=lambda _session: provider)
    plugin.manager = RoomManager()
    room, visitor, game = await make_soup_room(plugin.manager)
    await plugin.manager.complete_turtle_soup_generation(room, game, make_puzzle())

    with pytest.raises(RuntimeError, match="模型调用失败"):
        await plugin.submit_turtle_soup_question(
            room,
            "这是温室吗？",
            source="web",
            visitor_token=visitor.token,
        )

    assert not game.processing
    assert game.question_count == 0
    assert game.entries == []


@pytest.mark.asyncio
async def test_web_question_endpoint_uses_same_hidden_judge_flow() -> None:
    provider = SimpleNamespace(
        text_chat=AsyncMock(
            return_value=SimpleNamespace(
                completion_text='{"verdict":"yes","matched_facts":[0]}'
            )
        )
    )
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.context = SimpleNamespace(get_using_provider=lambda _session: provider)
    plugin.public_base_url = ""
    plugin.quick_tunnel = SimpleNamespace(url="", running=False)
    plugin.manager = RoomManager()
    room, visitor, game = await make_soup_room(plugin.manager)
    await plugin.manager.complete_turtle_soup_generation(room, game, make_puzzle())
    server = GameRoomServer(
        plugin,
        plugin.manager,
        host="127.0.0.1",
        port=0,
        web_root=Path(__file__).resolve().parents[1] / "web",
    )

    async with TestClient(TestServer(server._build_app())) as client:
        response = await client.post(
            f"/api/room/{room.access_token}/soup/question",
            json={"visitor_token": visitor.token, "text": "这是温室吗？"},
            headers={"Origin": str(client.make_url("/")).rstrip("/")},
        )
        payload = await response.json()

    assert response.status == 200
    assert payload["data"]["verdict"] == "yes"
    assert payload["data"]["room"]["game"]["entries"][0]["response"] == "是"
    assert "solution" not in payload["data"]["room"]["game"]["puzzle"]


def test_fallback_avoids_recent_signature_when_another_choice_exists() -> None:
    first = fallback_puzzle(content_level="all_ages")
    choices = {
        fallback_puzzle(
            content_level="all_ages", excluded_signatures={first.signature}
        ).signature
        for _index in range(10)
    }

    assert first.signature not in choices
