"""验证接龙进行中人格对话的边界：答案交给接龙插件，进度按真实状态注入。"""
import json
import logging
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from plugins.mention_chat import Plugin
from wxbot_framework.dialogue import SYSTEM, Dialogue
from wxbot_framework.members import ConversationSnapshot
from wxbot_framework.sdk import Message

ROOM = "12345678@chatroom"
CTX = type("Ctx", (), {"logger": logging.getLogger("MentionTests")})()


def active_game(**changes):
    state = {"current": "一心一意", "used": ["一心一意"], "idle_seconds": 1800,
             "updated_at": datetime.now(timezone.utc).isoformat()}
    state.update(changes)
    return state


class FakeRoomState:
    def __init__(self, state):
        self.state = state

    def get(self, room, feature):
        if isinstance(self.state, Exception):
            raise self.state
        return self.state


class RoomGameTests(unittest.TestCase):
    def test_answer_is_left_to_the_game_plugin(self):
        self.assertTrue(Plugin.is_move("赞不绝口", {"current": "交口称赞"}))
        self.assertFalse(Plugin.is_move("赞不绝口", {"current": "没齿难忘"}))
        self.assertFalse(Plugin.is_move("赞不绝口啊", {"current": "交口称赞"}))
        self.assertFalse(Plugin.is_move("赞不绝口！", {"current": "交口称赞"}))
        self.assertFalse(Plugin.is_move("", {"current": "交口称赞"}))

    def test_game_context_only_carries_real_progress(self):
        context = Plugin.game_context({"current": "一心一意", "used": ["词" + str(i) for i in range(30)]})
        self.assertEqual(context["current"], "一心一意")
        self.assertEqual(context["next_char"], "意")
        self.assertEqual(len(context["used"]), 10)
        self.assertEqual(context["used"][-1], "词29")

    def test_room_game_reads_shared_state(self):
        plugin = Plugin()
        plugin.room_state = FakeRoomState(active_game())
        self.assertEqual(plugin.room_game(ROOM, CTX)["current"], "一心一意")

    def test_game_context_includes_real_goal_and_remaining_help(self):
        context = Plugin.game_context(active_game(rounds=4, goal=10, hints_left=1, swaps_left=0))
        self.assertEqual((context["rounds"], context["goal"], context["hints_left"], context["swaps_left"]),
                         (4, 10, 1, 0))

    def test_game_commands_are_not_answered_by_chat(self):
        plugin = Plugin()
        ctx = Mock()
        plugin.members = Mock(account="wxid_robot", groups={ROOM})
        for text in ("接龙提示", "接龙换题", "接龙认输", "接龙状态", "/接龙 意气风发"):
            message = Message("@机器人\u2005" + text, ROOM, sender_id="wxid_player", is_group=True,
                              data={"is_at_self": True, "at_members": ("wxid_robot",)})
            plugin.on_message(message, ctx)
        ctx.knowledge.search.assert_not_called()
        ctx.reply_at.assert_not_called()

    def test_room_game_ignores_expired_or_invalid_progress(self):
        plugin = Plugin()
        now = datetime.now(timezone.utc)
        for state in (active_game(updated_at=(now - timedelta(seconds=61)).isoformat(), idle_seconds=60),
                      active_game(updated_at=(now + timedelta(seconds=60)).isoformat()),
                      active_game(updated_at="昨天"), active_game(used=None),
                      active_game(used=["意气风发"]), active_game(idle_seconds=True)):
            plugin.room_state = FakeRoomState(state)
            with self.subTest(state=state):
                self.assertIsNone(plugin.room_game(ROOM, CTX))

    def test_answer_does_not_get_a_chat_reply_after_game_progresses(self):
        plugin = Plugin()
        ctx = Mock()
        plugin.members = Mock(account="wxid_robot", groups={ROOM})
        before = active_game()
        plugin.room_state = FakeRoomState(active_game(current="发扬光大", used=["一心一意", "意气风发", "发扬光大"]))
        message = Message("@机器人\u2005意气风发", ROOM, sender_id="wxid_player", is_group=True,
                          data={"is_at_self": True, "at_members": ("wxid_robot",),
                                "room_states": {"idiom_chain": before}})
        plugin.on_message(message, ctx)
        ctx.knowledge.search.assert_not_called()
        ctx.reply_at.assert_not_called()

    def test_captured_empty_state_is_not_replaced_by_a_later_game(self):
        plugin = Plugin()
        plugin.room_state = FakeRoomState(active_game())
        message = Message("意气风发", ROOM, is_group=True, data={"room_states": {}})
        self.assertIsNone(plugin.room_game(ROOM, CTX, message=message))

    def test_room_game_ignores_absent_or_broken_state(self):
        plugin = Plugin()
        for state in (None, {}, {"current": 5}, {"current": "一心一意啊"}, ["一心一意"]):
            plugin.room_state = FakeRoomState(state)
            with self.subTest(state=state):
                self.assertIsNone(plugin.room_game(ROOM, CTX))
        plugin.room_state = None
        self.assertIsNone(plugin.room_game(ROOM, CTX))

    def test_room_game_survives_state_read_failure(self):
        plugin = Plugin()
        plugin.room_state = FakeRoomState(RuntimeError("库已关闭"))
        with self.assertLogs("MentionTests"):
            self.assertIsNone(plugin.room_game(ROOM, CTX))


class DialoguePayloadTests(unittest.TestCase):
    class Client:
        def __init__(self):
            self.payload = None

        def complete_text(self, system, user, **options):
            self.payload = json.loads(user)
            return "好"

    def snapshot(self):
        return ConversationSnapshot(ROOM, "2026-10-16", 0, "小明", (), "这是什么进度", ())

    def test_room_game_is_injected_only_when_given(self):
        client = self.Client()
        Dialogue(client).reply(self.snapshot(), room_game={"current": "一心一意", "next_char": "意"})
        self.assertEqual(client.payload["room_game"], {"current": "一心一意", "next_char": "意"})
        Dialogue(client).reply(self.snapshot())
        self.assertNotIn("room_game", client.payload)

    def test_system_tells_the_model_not_to_judge_the_chain(self):
        # 规则必须写进提示词，避免大模型自己编造接龙进度。
        self.assertIn("room_game", SYSTEM)
        self.assertIn("不要自己判定接龙", SYSTEM)

    def test_reply_without_game_still_works(self):
        client = self.Client()
        self.assertEqual(Dialogue(client).reply(self.snapshot()), "好")

    def test_legacy_json_reply_from_model_is_still_unwrapped(self):
        class LegacyClient:
            def complete_text(self, system, user, **options):
                return '{"reply":"旧格式也能正常显示"}'

        self.assertEqual(Dialogue(LegacyClient()).reply(self.snapshot()), "旧格式也能正常显示")


if __name__ == "__main__":
    unittest.main()
