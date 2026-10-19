"""验证全群共享接龙、普通聊天不入局、抢答与失败时的状态，使用内存上下文。"""
import logging
import time
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from plugins.idiom_chain import FEATURE, Plugin, HELP
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


class FakeRoomState:
    """按群共享状态的替身：跨插件读取与重启续局都靠它验证。"""

    def __init__(self):
        self.data = {}

    def get(self, room, feature):
        state = self.data.get((room, feature))
        return dict(state) if state is not None else None

    def put(self, room, feature, payload):
        self.data[(room, feature)] = dict(payload)

    def clear(self, room, feature):
        self.data.pop((room, feature), None)


class FakeMembers:
    """只提供接龙需要的水位查询，模拟持久记录与清空后的 ID 复用。"""

    def __init__(self):
        self.history_enabled = True
        self.latest = 0
        self.ids = {}

    def latest_id(self, room):
        return self.latest

    def message_id(self, message):
        return self.ids.get(message.text)


class Context:
    def __init__(self):
        self.config = {"member_names": {ROOM: {A: "小明", B: "小红"}}}
        self.llm = JSONClient({"idiom": "一心一意"})
        self.logger = logging.getLogger("IdiomTests")
        self.sent = []
        self.succeeds = True
        self.room_state = None

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


class NoMemberContext(Context):
    """模拟未配置成员目录的 PluginContext：读取 members 会抛能力异常。"""

    @property
    def members(self):
        from wxbot_framework.sdk import CapabilityError
        raise CapabilityError("尚未配置框架成员目录")


class IdiomPluginTests(unittest.TestCase):
    def setUp(self):
        self.ctx = Context()
        self.plugin = Plugin()
        self.plugin.on_load(self.ctx)
        self.sequence = 0

    def send(self, text, sender=A, room=ROOM, *, session="one", received_at=None, is_self=False,
             is_group=True, at_self=False, sequence=None):
        if sequence is None:
            self.sequence += 1
            sequence = self.sequence
        else:
            self.sequence = max(self.sequence, sequence)
        message = Message(text, room, sender_id=sender, session_id=session, is_group=is_group,
                          is_self=is_self, received_at=time.monotonic() if received_at is None else received_at,
                          sequence=sequence, data={"is_at_self": at_self})
        self.plugin.on_message(message, self.ctx)
        return message

    def start(self):
        self.send("成语接龙")
        self.ctx.llm.result = {"is_idiom": True, "reason": "常见成语", "next_idiom": "发扬光大"}

    def use_state(self, state):
        """换一个带共享状态的插件实例，模拟重启后重新加载。"""
        self.ctx.room_state = state
        self.plugin = Plugin()
        self.plugin.on_load(self.ctx)

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
        self.send("意气风发", A, received_at=arrived, sequence=2)
        count = len(self.ctx.sent)
        # 与第一条答案同一 sequence：模拟判定期间排队、发题后才被处理的消息。
        self.send("大大方方", B, received_at=arrived + 0.00000001, sequence=2)
        self.assertEqual(len(self.ctx.sent), count)
        self.assertEqual(self.plugin.games[ROOM].chain.current, "发扬光大")
        self.ctx.llm.result["next_idiom"] = "方兴未艾"
        self.send("大大方方", B)
        self.assertEqual(self.plugin.games[ROOM].chain.current, "方兴未艾")

    def test_same_timestamp_later_sequence_is_still_queued(self):
        self.start()
        arrived = time.monotonic()
        self.send("意气风发", A, received_at=arrived, sequence=2)
        count = len(self.ctx.sent)
        self.ctx.llm.result["next_idiom"] = "发扬光大"
        self.send("大大方方", B, received_at=arrived, sequence=3)
        self.assertEqual(len(self.ctx.sent), count)
        self.assertEqual(self.plugin.games[ROOM].chain.current, "发扬光大")

    def test_pre_reply_answer_with_larger_sequence_is_ignored(self):
        self.start()
        count = len(self.ctx.sent)
        # 发题回复尚未完成时到达：即使 sequence 正常递增也不能进入下一轮。
        self.send("意气风发", A, received_at=0)
        self.assertEqual(len(self.ctx.sent), count)

    def test_pre_reply_answer_with_larger_message_id_is_ignored(self):
        members = FakeMembers()
        members.latest = 10
        self.ctx.members = members
        self.start()
        members.ids["意气风发"] = 99  # 记录线程晚提交，ID 比发题水位更大。
        count = len(self.ctx.sent)
        self.send("意气风发", A, received_at=0)
        self.assertEqual(len(self.ctx.sent), count)
        self.assertEqual(self.plugin.games[ROOM].chain.current, "一心一意")

    def test_opening_still_starts_without_member_directory(self):
        self.ctx = NoMemberContext()
        self.plugin.on_load(self.ctx)
        self.start()
        self.assertIn(ROOM, self.plugin.games)
        self.ctx.llm.result["next_idiom"] = "发扬光大"
        self.send("意气风发", A)
        self.assertEqual(self.plugin.games[ROOM].chain.current, "发扬光大")

    def test_reused_message_id_after_clear_still_counts(self):
        members = FakeMembers()
        members.latest = 10
        self.ctx.members = members
        self.start()
        members.ids["意气风发"] = 1  # 清空后 rowid 从 1 重新分配。
        self.ctx.llm.result["next_idiom"] = "发扬光大"
        self.send("意气风发", A)
        self.assertEqual(self.plugin.games[ROOM].chain.current, "发扬光大")

    def test_old_message_id_is_rejected_even_when_sequence_advanced(self):
        members = FakeMembers()
        members.latest = 10
        self.ctx.members = members
        self.start()
        members.ids["意气风发"] = 10
        count = len(self.ctx.sent)
        self.send("意气风发", A, received_at=0)
        self.assertEqual(len(self.ctx.sent), count)
        self.assertEqual(self.plugin.games[ROOM].chain.current, "一心一意")

    def test_reconnected_sequence_restart_still_counts(self):
        members = FakeMembers()
        members.history_enabled = False
        self.ctx.members = members
        self.start()
        self.ctx.llm.result["next_idiom"] = "发扬光大"
        self.send("意气风发", A, session="two", sequence=1)
        self.assertEqual(self.plugin.games[ROOM].chain.current, "发扬光大")

    def test_equal_timestamp_uses_message_id_as_tiebreak(self):
        members = FakeMembers()
        members.latest = 10
        self.ctx.members = members
        self.start()
        game = self.plugin.games[ROOM]
        game.chain.ready_at = 100.0
        members.ids["意气风发"] = 11  # ID 大于发题水位。
        self.ctx.llm.result["next_idiom"] = "发扬光大"
        self.send("意气风发", A, received_at=100.0)
        self.assertEqual(game.chain.current, "发扬光大")

    def test_equal_timestamp_old_message_id_is_ignored(self):
        members = FakeMembers()
        members.latest = 10
        self.ctx.members = members
        self.start()
        game = self.plugin.games[ROOM]
        game.chain.ready_at = 100.0
        members.ids["意气风发"] = 10  # ID 未超过发题水位。
        count = len(self.ctx.sent)
        self.send("意气风发", A, received_at=100.0)
        self.assertEqual(len(self.ctx.sent), count)
        self.assertEqual(game.chain.current, "一心一意")

    def test_equal_timestamp_uses_sequence_as_tiebreak(self):
        self.start()
        game = self.plugin.games[ROOM]
        game.chain.ready_at = 100.0
        self.ctx.llm.result["next_idiom"] = "发扬光大"
        self.send("意气风发", A, received_at=100.0, sequence=2)
        self.assertEqual(game.chain.current, "发扬光大")

    def test_answer_queued_before_opening_reply_is_ignored(self):
        arrived = time.monotonic()
        self.send("成语接龙", sequence=1)
        count = len(self.ctx.sent)
        # 消息在开场题发出前就已到达，只是排队到发题完成后才被插件处理。
        self.send("意气风发", A, received_at=arrived, sequence=1)
        self.assertEqual(len(self.ctx.sent), count)
        self.assertEqual(self.plugin.games[ROOM].chain.current, "一心一意")

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

    def test_at_self_idiom_counts_as_answer(self):
        # 群里习惯艾特机器人接龙，剥掉艾特标签后仍按答案判定。
        self.start()
        self.send("@有个 ai 意气风发", at_self=True)
        self.assertEqual(self.ctx.sent[-1][2:], (A, "小明"))
        self.assertEqual(self.plugin.games[ROOM].chain.current, "发扬光大")

    def test_idiom_with_trailing_at_label_counts_as_answer(self):
        # 先打成语再补艾特也是常见写法，判定口径要和人格对话一致。
        self.start()
        self.send("意气风发@有个 ai ")
        self.assertEqual(self.plugin.games[ROOM].chain.current, "发扬光大")

    def test_at_self_chat_that_is_not_an_answer_is_ignored(self):
        self.start()
        count = len(self.ctx.sent)
        for text in ("晚上吃啥", "意气风发啊"):
            self.send(text, B, at_self=True)
        self.assertEqual(len(self.ctx.sent), count)
        self.assertEqual(self.plugin.games[ROOM].chain.current, "一心一意")

    def test_own_long_reply_does_not_create_loop_and_private_is_ignored(self):
        self.start()
        count = len(self.ctx.sent)
        self.send("接龙成功！一心一意 → 意气风发", is_self=True)
        self.send("/help", is_group=False)
        self.assertEqual(len(self.ctx.sent), count)

    def test_progress_survives_restart(self):
        state = FakeRoomState()
        self.use_state(state)
        self.start()
        self.send("意气风发", A)
        self.assertEqual(state.data[(ROOM, FEATURE)]["current"], "发扬光大")
        # 重启后换一个插件实例，靠共享状态接着上一局继续。
        self.use_state(state)
        self.ctx.llm.result["next_idiom"] = "方兴未艾"
        self.send("大大方方", B)
        self.assertIn("发扬光大", self.ctx.sent[-1][1])
        self.assertEqual(self.plugin.games[ROOM].chain.current, "方兴未艾")

    def test_restored_progress_keeps_remaining_idle_time(self):
        state = FakeRoomState()
        self.use_state(state)
        recent = datetime.now(timezone.utc) - timedelta(seconds=self.plugin.idle_seconds - 30)
        state.data[(ROOM, FEATURE)] = {"current": "一心一意", "used": ["一心一意"],
                                       "updated_at": recent.isoformat()}
        self.send("合并接龙")
        game = self.plugin.games[ROOM]
        self.assertIn("一心一意", self.ctx.sent[-1][1])
        # 续上的局只保留剩余空闲时间，重启本身不给它续命。
        self.assertAlmostEqual(time.monotonic() - game.updated_at, self.plugin.idle_seconds - 30, delta=5)

    def test_expired_persisted_progress_is_ignored(self):
        state = FakeRoomState()
        self.use_state(state)
        stale = datetime.now(timezone.utc) - timedelta(seconds=self.plugin.idle_seconds + 60)
        state.data[(ROOM, FEATURE)] = {"current": "一心一意", "used": ["一心一意"],
                                       "updated_at": stale.isoformat()}
        count = len(self.ctx.sent)
        self.send("意气风发")
        self.assertEqual(len(self.ctx.sent), count)
        self.assertEqual(self.plugin.games, {})

    def test_broken_persisted_progress_is_ignored(self):
        state = FakeRoomState()
        self.use_state(state)
        for payload in ({"current": "一心一意"}, {"current": "一心一意", "used": [], "updated_at": "昨天"},
                        {"current": "一心一意", "used": ["意气风发"], "updated_at": "2026-10-16T09:00:00+00:00"}):
            state.data[(ROOM, FEATURE)] = payload
            with self.subTest(payload=payload):
                self.use_state(state)
                count = len(self.ctx.sent)
                self.send("意气风发")
                self.assertEqual(len(self.ctx.sent), count)
                self.assertEqual(self.plugin.games, {})

    def test_exit_and_expiry_clear_persisted_progress(self):
        state = FakeRoomState()
        self.use_state(state)
        self.start()
        self.assertIn((ROOM, FEATURE), state.data)
        self.send("退出接龙")
        self.assertNotIn((ROOM, FEATURE), state.data)
        self.ctx.llm.result = {"idiom": "一心一意"}
        self.send("成语接龙")
        self.plugin.games[ROOM].updated_at -= self.plugin.idle_seconds + 1
        self.send("意气风发")
        self.assertNotIn((ROOM, FEATURE), state.data)

    def test_progress_is_kept_across_reconnect(self):
        self.start()
        self.send("意气风发", session="two")
        self.assertEqual(self.plugin.games[ROOM].chain.current, "发扬光大")

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

    def test_failed_bot_continuation_commits_player_answer_for_the_next_player(self):
        state = FakeRoomState()
        self.use_state(state)
        self.start()
        self.ctx.llm.result["next_idiom"] = "假的"
        self.send("意气风发", A)
        self.assertEqual(self.plugin.games[ROOM].chain.current, "意气风发")
        self.assertEqual(self.plugin.games[ROOM].chain.used, ("一心一意", "意气风发"))
        self.assertEqual(state.data[(ROOM, FEATURE)]["current"], "意气风发")
        self.assertIn("发", self.ctx.sent[-1][1])
        self.assertNotIn("暂时无法判定", self.ctx.sent[-1][1])
        self.assertNotIn("我接：意气风发", self.ctx.sent[-1][1])
        self.ctx.llm.result["next_idiom"] = "大有可为"
        self.send("发扬光大", B)
        self.assertEqual(self.plugin.games[ROOM].chain.current, "大有可为")

    def test_failed_send_keeps_original_question_even_when_bot_cannot_continue(self):
        self.start()
        self.ctx.llm.result["next_idiom"] = "假的"
        self.ctx.succeeds = False
        self.send("意气风发")
        self.assertEqual(self.plugin.games[ROOM].chain.current, "一心一意")
        self.assertEqual(self.plugin.games[ROOM].chain.used, ("一心一意",))

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
