from __future__ import annotations

from pathlib import Path

import pytest

from astrbot_plugin_game_companion.pikafish import PikafishService
from astrbot_plugin_game_companion.room_manager import RoomManager
from astrbot_plugin_game_companion.xiangqi import RED, XiangqiGame


class FakePikafish:
    def __init__(self) -> None:
        self.ready = False

    async def ensure_ready(self) -> None:
        self.ready = True

    async def legal_moves(self, moves: list[str]) -> list[str]:
        positions = {
            (): ["a3a4", "b0c2"],
            ("a3a4",): ["a6a5"],
            ("a3a4", "a6a5"): ["b0c2"],
            ("b0c2",): [],
        }
        return positions.get(tuple(moves), [])

    async def choose_move(self, moves: list[str], _difficulty: str) -> str:
        assert moves == ["a3a4"]
        return "a6a5"


class FailingBotPikafish(FakePikafish):
    async def choose_move(self, moves: list[str], _difficulty: str) -> str:
        raise RuntimeError("引擎测试退出")


def test_xiangqi_coordinates_round_trip() -> None:
    assert XiangqiGame.coordinates_to_iccs(6, 0, 5, 0) == "a3a4"
    assert XiangqiGame.iccs_to_coordinates("b0c2") == [9, 1, 7, 2]

    with pytest.raises(ValueError, match="超出棋盘"):
        XiangqiGame.coordinates_to_iccs(10, 0, 5, 0)


@pytest.mark.asyncio
async def test_xiangqi_uses_engine_legal_moves_and_undoes_a_round() -> None:
    engine = FakePikafish()
    game = await XiangqiGame.create(engine, human_side=RED, difficulty="normal")

    await game.place_human(engine, 6, 0, 5, 0)
    await game.place_bot(engine)

    assert game.moves == ["a3a4", "a6a5"]
    assert game.board()[5][0] == "P"
    assert game.board()[4][0] == "p"
    assert await game.undo_round(engine) == 2
    assert game.moves == []
    assert game.halfmove_clock == 0
    assert game.turn == RED


@pytest.mark.asyncio
async def test_no_legal_reply_finishes_xiangqi_game() -> None:
    engine = FakePikafish()
    game = await XiangqiGame.create(engine, human_side=RED, difficulty="normal")

    await game.place_human(engine, 9, 1, 7, 2)

    assert game.finished
    assert game.winner == RED


@pytest.mark.asyncio
async def test_switch_game_preserves_room_link_seat_and_independent_scores() -> None:
    engine = FakePikafish()
    manager = RoomManager(xiangqi_engine=engine)  # type: ignore[arg-type]
    room = await manager.create_room(
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
    visitor = await manager.join(room)
    await manager.claim_and_start(room, visitor.token, "human_black")
    room.completed_games = 2
    room.human_wins = 1
    original = (room.room_id, room.access_token, room.player_token, visitor.number)

    with pytest.raises(ValueError, match="明确放弃"):
        await manager.switch_game(room, "xiangqi")

    assert await manager.switch_game(room, "xiangqi", force=True)
    assert (room.room_id, room.access_token, room.player_token, visitor.number) == original
    assert room.status == "setup"
    assert room.game is None
    assert room.completed_games == 0
    assert room.scores["gomoku"].completed == 2
    assert room.scores["gomoku"].human_wins == 1
    assert engine.ready


@pytest.mark.asyncio
async def test_pikafish_fake_uci_process(tmp_path: Path) -> None:
    engine = tmp_path / "pikafish-fake"
    engine.write_text(
        """#!/usr/bin/env python3
import sys
for raw in sys.stdin:
    command = raw.strip()
    if command == "uci":
        print("id name Pikafish Fake")
        print("uciok")
    elif command == "isready":
        print("readyok")
    elif command == "go perft 1":
        print("a3a4: 1")
        print("b0c2: 1")
        print("Nodes searched: 2")
    elif command.startswith("go "):
        print("info depth 3 multipv 1 score cp 10 pv a3a4")
        print("bestmove a3a4")
    elif command == "quit":
        break
    sys.stdout.flush()
""",
        encoding="utf-8",
    )
    engine.chmod(0o755)
    (tmp_path / "pikafish.nnue").write_bytes(b"fake network")
    service = PikafishService(data_dir=tmp_path / "data", configured_path=str(engine))

    try:
        assert await service.legal_moves([]) == ["a3a4", "b0c2"]
        assert await service.choose_move([], "hard") == "a3a4"
        assert service.status()["version"] == "Pikafish Fake"
    finally:
        await service.close()

    assert not service.running


@pytest.mark.asyncio
async def test_xiangqi_engine_failure_pauses_game_after_human_move() -> None:
    engine = FailingBotPikafish()
    manager = RoomManager(xiangqi_engine=engine)  # type: ignore[arg-type]
    room = await manager.create_room(
        source="private",
        session_id="aiocqhttp:private:10001",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type="xiangqi",
        difficulty="normal",
    )
    visitor = await manager.join(room)
    await manager.claim_and_start(room, visitor.token, "human_red")

    with pytest.raises(RuntimeError, match="引擎测试退出"):
        await manager.player_move(
            room,
            visitor.token,
            from_row=6,
            from_column=0,
            to_row=5,
            to_column=0,
        )

    assert room.status == "paused"
    assert room.game is not None
    assert room.game.moves == ["a3a4"]
    assert "象棋引擎暂时不可用" in str(room.messages[-1]["content"])
