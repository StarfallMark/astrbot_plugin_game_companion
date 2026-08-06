from __future__ import annotations

import pytest
from astrbot_plugin_game_companion.gomoku import WHITE, GomokuGame
from astrbot_plugin_game_companion.main import GameCompanionPlugin
from astrbot_plugin_game_companion.models import GameRoom
from astrbot_plugin_game_companion.pig_dice import PigDiceGame
from astrbot_plugin_game_companion.tictactoe import NOUGHT, TicTacToeGame
from astrbot_plugin_game_companion.xiangqi import BLACK, XiangqiGame


def make_room(game_type: str, game: object) -> GameRoom:
    room = GameRoom(
        room_id="room-1",
        access_token="access-token",
        source="private",
        session_id="aiocqhttp:private:10001",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type=game_type,  # type: ignore[arg-type]
        difficulty="normal",
    )
    room.game = game  # type: ignore[assignment]
    return room


@pytest.mark.parametrize(
    ("room", "expected"),
    [
        (
            make_room("gomoku", GomokuGame(human_color=WHITE)),
            ("玩家执白，Bot 执黑", "黑方先手", "由Bot先行"),
        ),
        (
            make_room("xiangqi", XiangqiGame(human_side=BLACK)),
            ("玩家执黑，Bot 执红", "红方先手", "由Bot先行"),
        ),
        (
            make_room("tictactoe", TicTacToeGame(human_mark=NOUGHT)),
            ("玩家执 O，Bot 执 X", "X 先手", "由Bot先行"),
        ),
        (
            make_room("pig_dice", PigDiceGame(turn="human")),
            ("随机先手结果已经确定", "由玩家先掷", "目标是先得到 50 分"),
        ),
    ],
)
def test_opening_commentary_uses_authoritative_side_and_first_turn(
    room: GameRoom, expected: tuple[str, ...]
) -> None:
    prompt = GameCompanionPlugin._opening_commentary_prompt(room)

    for fact in expected:
        assert fact in prompt
    assert "不得说反双方身份、颜色、标记或先后手" in prompt
