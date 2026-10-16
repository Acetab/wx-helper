"""验证全群共享接龙、普通聊天不入局、抢答与失败时的状态，使用内存上下文。"""
import logging
import time
import unittest
from contextlib import nullcontext
from unittest.mock import patch

from plugins.idiom_chain import Plugin, HELP
from wxbot_framework.llm import LLMError
from wxbot_framework.sdk import Message
from wxbot_framework.personality import PersonalityState

ROOM = "12345678@chatroom"
A, B = "wxid_player_a", "wxid_player_b"


class JSONClient:
    def __init__(self, result):
        self.result = result

    def complete_json(self, system, user):
        return self.result


class Context:
    def __init__(self):
        self.config = {"member_names": {ROOM: {A: "小明", B: "小红"}}}
        self.llm = JSONClient({"idiom": "一心一意"})
        self.logger = logging.getLogger("IdiomTests")
        self.sent = []
        self.succeeds = True

    def personality(self, message):
        return PersonalityState(message.chat_id)

    def llm_for(self, message, *, personality=None):
        return self.llm

    def personality_guard(self, state):
        return nullcontext()

    def reply(self, message, text, *, delivery_guard=None):
        self.sent.append((message.chat_id, text, None, None))
        return self.succeeds

    def reply_at(self, message, text, *, display_name=None, delivery_guard=None):
        self.sent.append((message.chat_id, text, message.sender_id, display_name))
        return self.succeeds


class IdiomPluginTests(unittest.TestCase):
    def setUp(self):
        self.ctx = Context()
        self.plugin = Plugin()
        self.plugin.on_load(self.ctx)

    def send(self, text, sender=A, room=ROOM, *, session="one", received_at=None, is_self=False,
             is_group=True, at_self=False):
        message = Message(text, room, sender_id=sender, session_id=session, is_group=is_group,
                          is_self=is_self, received_at=time.monotonic() if received_at is None else received_at,
                          data={"is_at_self": at_self})
        self.plugin.on_message(message, self.ctx)
        return message

    def start(self):
        self.send("成语接龙")
        self.ctx.llm.result = {"is_idiom": True, "reason": "常见成语", "next_idiom": "发扬光大"}

    def test_start_reply_explains_rule(self):
        self.send("成语接龙")
        self.assertEqual(len(self.ctx.sent), 1)
        self.assertIn("一心一意", self.ctx.sent[-1][1])
        self.assertIn("四字成语", self.ctx.sent[-1][1])
        self.assertEqual(self.plugin.games[ROOM].chain.current, "一心一意")

    def test_players_share_one_chain_and_inherit_progress(self):
        self.start()
        self.send("意气风发", A)
        self.assertEqual(self.ctx.sent[-1][2:], (A, "小明"))
        self.assertIn("发扬光大", self.ctx.sent[-1][1])
        # 第二位玩家接着上一位玩家报出的“大”继续，而不是回到开场成语。
        self.ctx.llm.result["next_idiom"] = "方兴未艾"
        self.send("大大方方", B)
        self.assertEqual(self.ctx.sent[-1][2:], (B, "小红"))
        self.assertIn("大大方方", self.ctx.sent[-1][1])
        self.assertEqual(self.plugin.games[ROOM].chain.current, "方兴未艾")
        self.assertEqual(self.plugin.games[ROOM].chain.used[-2:], ("大大方方", "方兴未艾"))

    def test_plain_chat_is_ignored_while_game_runs(self):
        self.start()
        game = self.plugin.games[ROOM]
        count = len(self.ctx.sent)
        for text in ("晚上吃啥", "哈哈哈哈", "你们先聊", "?", "大" * 5):
            self.send(text, B)
        self.assertEqual(len(self.ctx.sent), count)
        self.assertEqual(game.chain.current, "一心一意")
        self.assertEqual(game.chain.used, ("一心一意",))

    def test_wrong_first_character_is_treated_as_plain_chat(self):
        self.start()
        count = len(self.ctx.sent)
        self.send("异想天开", B)
        self.assertEqual(len(self.ctx.sent), count)
        self.assertEqual(self.plugin.games[ROOM].chain.current, "一心一意")

    def test_correct_prefix_but_not_idiom_reports_failure(self):
        self.start()
        self.ctx.llm.result = {"is_idiom": False, "reason": "这不是成语", "next_idiom": ""}
        self.send("意料之外", B)
        self.assertIn("接龙失败", self.ctx.sent[-1][1])
        self.assertEqual(self.ctx.sent[-1][2:], (B, "小红"))
        self.assertEqual(self.plugin.games[ROOM].chain.current, "一心一意")

    def test_only_first_answer_of_a_round_counts(self):
        self.start()
        arrived = time.monotonic()
        self.send("意气风发", A, received_at=arrived)
        count = len(self.ctx.sent)
        self.send("大大方方", B, received_at=arrived + 0.00000001)
        self.assertEqual(len(self.ctx.sent), count)
        self.assertEqual(self.plugin.games[ROOM].chain.current, "发扬光大")
        self.ctx.llm.result["next_idiom"] = "方兴未艾"
        self.send("大大方方", B)
        self.assertEqual(self.plugin.games[ROOM].chain.current, "方兴未艾")

    def test_merged_command_keeps_the_same_shared_chain(self):
        self.start()
        self.send("意气风发", A)
        self.send("合并接龙")
        self.assertIn("发扬光大", self.ctx.sent[-1][1])
        self.ctx.llm.result["next_idiom"] = "方兴未艾"
        self.send("大大方方", B)
        self.assertEqual(self.plugin.games[ROOM].chain.current, "方兴未艾")

    def test_status_command_reports_current_word_without_starting_over(self):
        self.start()
        self.send("意气风发", A)
        opening = self.plugin.games[ROOM].opening
        self.ctx.llm.result = {"idiom": "自相矛盾"}
        self.send("成语接龙", B)
        self.assertIn("发扬光大", self.ctx.sent[-1][1])
        self.assertIn("大", self.ctx.sent[-1][1])
        self.assertEqual(self.plugin.games[ROOM].opening, opening)

    def test_groups_do_not_share_game(self):
        self.start()
        self.send("意气风发", room="87654321@chatroom")
        self.assertEqual(len(self.ctx.sent), 1)

    def test_exit_and_restart(self):
        self.start()
        self.send("退出接龙")
        self.assertNotIn(ROOM, self.plugin.games)
        count = len(self.ctx.sent)
        self.send("意气风发")
        self.assertEqual(len(self.ctx.sent), count)
        self.ctx.llm.result = {"idiom": "一心一意"}
        self.send("成语接龙")
        self.assertIn(ROOM, self.plugin.games)

    def test_help_and_primary_phone_commands(self):
        self.send("/help", is_self=True)
        self.assertEqual(self.ctx.sent, [])  # 帮助统一由知识插件处理，不重复回复。
        self.assertIn("/记忆", HELP)
        self.send("成语接龙", is_self=True)
        self.assertIn("一心一意", self.ctx.sent[-1][1])
        self.assertIn(ROOM, self.plugin.games)
        self.ctx.llm.result = {"is_idiom": True, "reason": "常见成语", "next_idiom": "发扬光大"}
        self.send("意气风发", is_self=True)
        self.assertEqual(self.plugin.games[ROOM].chain.current, "发扬光大")

    def test_at_self_chat_is_left_to_mention_plugin(self):
        self.start()
        count = len(self.ctx.sent)
        self.send("意气风发", at_self=True)
        self.assertEqual(len(self.ctx.sent), count)
        self.assertEqual(self.plugin.games[ROOM].chain.current, "一心一意")

    def test_own_long_reply_does_not_create_loop_and_private_is_ignored(self):
        self.start()
        count = len(self.ctx.sent)
        self.send("接龙成功！一心一意 → 意气风发", is_self=True)
        self.send("/help", is_group=False)
        self.assertEqual(len(self.ctx.sent), count)

    def test_reconnect_resets_game(self):
        self.start()
        self.send("意气风发", session="two")
        self.assertEqual(self.plugin.games, {})

    def test_failed_send_does_not_advance_question(self):
        self.start()
        self.ctx.succeeds = False
        self.send("意气风发")
        self.assertEqual(self.plugin.games[ROOM].chain.current, "一心一意")
        self.ctx.succeeds = True
        self.send("意气风发")
        self.assertEqual(self.plugin.games[ROOM].chain.current, "发扬光大")

    def test_model_failure_keeps_question(self):
        self.start()
        with patch("plugins.idiom_chain.IdiomJudge.evaluate", side_effect=LLMError("测试超时")), self.assertLogs("IdiomTests"):
            self.send("意气风发")
        self.assertEqual(self.plugin.games[ROOM].chain.current, "一心一意")
        self.assertIn("重试", self.ctx.sent[-1][1])

    def test_model_opening_failure_does_not_create_game(self):
        with patch("plugins.idiom_chain.IdiomJudge.opening", side_effect=LLMError("测试超时")), self.assertLogs("IdiomTests"):
            self.send("成语接龙")
        self.assertEqual(self.plugin.games, {})

    def test_idle_game_expires_and_is_not_treated_as_chain(self):
        self.start()
        self.plugin.games[ROOM].updated_at -= self.plugin.idle_seconds + 1
        count = len(self.ctx.sent)
        self.send("意气风发")
        self.assertEqual(len(self.ctx.sent), count)
        self.assertNotIn(ROOM, self.plugin.games)
        self.ctx.llm.result = {"idiom": "一心一意"}
        self.send("成语接龙")
        self.assertIn(ROOM, self.plugin.games)

    def test_invalid_member_name_configuration(self):
        for options in ({"member_names": []}, {"member_names": {ROOM: {A: "\n"}}},
                        {"member_names": {ROOM: {A: "A" * 65}}}):
            self.ctx.config = options
            with self.subTest(options=options), self.assertRaises(ValueError):
                Plugin().on_load(self.ctx)

    def test_invalid_idle_configuration(self):
        for value in (True, 0, 59, 86401, "1800"):
            self.ctx.config = {"member_names": {}, "idle_seconds": value}
            with self.subTest(value=value), self.assertRaises(ValueError):
                Plugin().on_load(self.ctx)


if __name__ == "__main__":
    unittest.main()
