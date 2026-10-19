"""固定熟人损友语气、真实艾特、上下文及并发取消的验证。"""
import json
import sys
import unittest
from datetime import timedelta
from unittest.mock import Mock

import test_chat_summary as daily
from plugins.mention_chat import Plugin as ChatPlugin
from plugins.personality import Plugin as PersonalityPlugin
from wxbot_framework.members import MemberError
from wxbot_framework.mentions import parse_at_members, strip_at_labels
from wxbot_framework.personality import FIXED_NAME, PersonalityState, prepare_switch, game_reply
from wxbot_framework.runtime import BotApplication
from wxbot_framework.sdk import Message

ROOM, CAT, ROBOT, OTHER = daily.ROOM, daily.CAT, daily.ROBOT, daily.OTHER


class FixedPersonalityTests(unittest.TestCase):
    setUp = daily.DailySummaryTests.setUp
    tearDown = daily.DailySummaryTests.tearDown
    new_store = daily.DailySummaryTests.new_store
    message = daily.DailySummaryTests.message
    model = daily.DailySummaryTests.model
    snapshot = daily.DailySummaryTests.snapshot

    def chat(self, text="你怎么看？", *, sequence=100, sender=CAT, at_members=(ROBOT,), **kwargs):
        msg = self.message("@机器人\u2005" + text, sequence=sequence, sender=sender,
                           data={"at_members": at_members, "is_at_self": True, **kwargs})
        self.store.record(msg)
        return msg

    def setup_chat(self, provider=None):
        self.backend.send_at_text = lambda room, text, ids: self.backend.sent.append((room, text, ids)) or True
        self.ctx._llm = Mock(complete_text=provider or Mock(return_value="这是自然具体的回答。"))
        plugin = ChatPlugin()
        plugin.on_load(self.ctx)
        return plugin

    def test_old_mode_command_returns_notice_without_switching(self):
        prepared = prepare_switch(self.message("@机器人\u2005/切换人格 嘴臭"), self.store)
        plugin = PersonalityPlugin()
        plugin.on_load(self.ctx)
        plugin.on_message(prepared, self.ctx)
        self.assertIn("已固定", self.backend.sent[-1][1])
        self.assertEqual(self.store.personality(ROOM).name, FIXED_NAME)
        with self.assertRaises(MemberError):
            self.store.set_personality(ROOM, "嘴臭")

    def test_fixed_style_wraps_function_constraints_without_truncation(self):
        provider = Mock(return_value={"reply": "回答"})
        self.ctx._llm = Mock(complete_json=provider)
        self.ctx.llm_for(self.message("问题")).complete_json("必须遵守JSON字段", "输入")
        system = provider.call_args.args[0]
        self.assertIn("熟人损友", system)
        self.assertIn("必须遵守JSON字段", system)
        self.assertNotIn("每次回复控制在 20", system)

    def test_group_and_own_context_are_separate_and_keep_real_at(self):
        self.store.record(self.message("本人前文", sequence=1))
        self.store.record(self.message("别人聊吉他", sender=OTHER, sequence=2))
        provider = Mock(return_value="刚才大家聊吉他，你也想试试吗？")
        plugin = self.setup_chat(provider)
        plugin.on_message(self.chat("刚才聊什么？"), self.ctx)
        payload = json.loads(provider.call_args.args[1])
        self.assertEqual(payload["previous"][0]["text"], "本人前文")
        self.assertEqual(payload["group_recent"][0]["text"], "别人聊吉他")
        self.assertNotIn(CAT, provider.call_args.args[1])
        self.assertNotIn(OTHER, provider.call_args.args[1])
        self.assertEqual(self.backend.sent[-1][2], (CAT,))
        self.assertTrue(self.backend.sent[-1][1].startswith("@测试成员\u2005"))

    def test_recent_context_excludes_later_messages_commands_and_robot(self):
        for number in range(12):
            self.store.record(self.message(f"前文{number}", sequence=number + 1))
        self.store.record(self.message("/档案"))
        self.store.record(self.message("机器人输出", data={"bot_generated": True}))
        msg = self.chat()
        self.store.record(self.message("后来才收到", sequence=101))
        self.assertEqual([row["text"] for row in self.store.conversation_snapshot(msg).messages],
                         [f"前文{number}" for number in range(2, 12)])

    def test_robot_reply_is_saved_and_reused_as_assistant_context(self):
        provider = Mock(return_value="B 是这样来的。")
        plugin = self.setup_chat(provider)
        plugin.on_message(self.chat("A 是什么？", sequence=10), self.ctx)
        stored = self.store.recent_rows(ROOM, include_bot=True)
        self.assertIn("B 是这样来的。", [row["text"] for row in stored])
        plugin.on_message(self.chat("那你为什么觉得 B 是这样？", sequence=11), self.ctx)
        history = provider.call_args.kwargs["history"]
        self.assertEqual([row["role"] for row in history], ["user", "assistant"])
        self.assertIn("A 是什么", history[0]["content"])
        self.assertEqual(history[1]["content"], "B 是这样来的。")

    def test_cross_day_does_not_cancel_reply(self):
        def change(*args, **kwargs):
            self.now += timedelta(seconds=1)
            return "跨天仍能继续这段对话。"
        plugin = self.setup_chat(change)
        plugin.on_message(self.chat(), self.ctx)
        self.assertIn("跨天仍能", self.backend.sent[-1][1])

    def test_context_reset_during_model_request_discards_result(self):
        def change(*args, **kwargs):
            self.store.reset_context(ROOM)
            return "这条已经过时"
        plugin = self.setup_chat(change)
        plugin.on_message(self.chat(), self.ctx)
        self.assertEqual(self.backend.sent, [])

    def test_mention_of_other_person_and_command_do_not_call_model(self):
        provider = Mock(return_value="回答")
        plugin = self.setup_chat(provider)
        for msg in (self.chat(at_members=(OTHER,)), self.chat("/绑定 新名", sequence=101),
                    self.chat(sequence=102, bot_generated=True), self.message("@机器人 只有文字"),
                    Message("私聊", CAT)):
            plugin.on_message(msg, self.ctx)
        provider.assert_not_called()
        self.assertEqual(self.backend.sent, [])

    def test_unbound_member_gets_real_id_and_binding_reminder_once(self):
        plugin = self.setup_chat()
        plugin.on_message(self.chat(sender="wxid_unbound", sender_wechat_name="路人甲"), self.ctx)
        self.assertEqual(self.backend.sent[-1][2], ("wxid_unbound",))
        self.assertTrue(self.backend.sent[-1][1].startswith("@路人甲\u2005"))
        self.assertNotIn("/绑定", self.backend.sent[-1][1])

    def test_bound_person_uses_wechat_name_for_at_and_person_name_for_llm_context(self):
        provider = Mock(return_value="你是示例人物乙（乙哥），长语音担当。")
        plugin = self.setup_chat(provider)
        kb = self.ctx.knowledge
        person = kb.entity(ROOM, "示例人物乙", aliases=["乙哥", "小乙"], pinned=True)
        kb.add_memory(ROOM, person["id"], "长语音担当", "群内玩笑", "乙哥的经典形象是超长语音。", CAT)
        self.store.record(self.message("测试发言", sender=OTHER, sequence=1,
                                       data={"sender_wechat_name": "示例微信昵称", "self_wxid": ROBOT}))
        kb.link(ROOM, OTHER, "示例人物乙", CAT)
        plugin.on_message(self.chat("我是谁", sender=OTHER, sequence=2, sender_wechat_name="示例微信昵称"), self.ctx)
        payload = json.loads(provider.call_args.args[1])
        self.assertEqual(payload["name"], "示例人物乙")
        self.assertTrue(payload["speaker_profile"]["bound"])
        self.assertEqual(payload["speaker_profile"]["name"], "示例人物乙")
        self.assertIn("乙哥", payload["speaker_profile"]["aliases"])
        self.assertEqual(payload["speaker_profile"]["wechat_name"], "示例微信昵称")
        self.assertEqual(payload["knowledge"][0]["name"], "示例人物乙")
        self.assertTrue(self.backend.sent[-1][1].startswith("@示例微信昵称\u2005\n"))

    def test_long_reply_respects_byte_limit_and_is_not_twenty_characters(self):
        plugin = self.setup_chat(Mock(return_value="中文🌸" * 190))
        plugin.on_message(self.chat(), self.ctx)
        self.assertGreater(len(self.backend.sent), 1)
        self.assertTrue(all(len(row[1].encode("utf-8")) <= 1023 for row in self.backend.sent))
        self.assertTrue(all(row[2] == (CAT,) for row in self.backend.sent))

    def test_invalid_model_reply_preserves_context(self):
        plugin = self.setup_chat(Mock(return_value=""))
        plugin.on_message(self.chat(), self.ctx)
        self.assertIn("暂时无法回复", self.backend.sent[-1][1])
        self.assertTrue(self.store.history("测试成员", ROOM))

    def test_game_reply_preserves_next_word_and_rule(self):
        reply = game_reply(FIXED_NAME, True, "", "一心一意", "意气风发", "发扬光大")
        self.assertIn("发扬光大", reply)
        self.assertIn("以“大”", reply)

    def test_runtime_prepares_reset_before_dispatch(self):
        app = object.__new__(BotApplication)
        app.config, app.members, app.plugins = {"chat_allowlist": []}, self.store, Mock()
        self.store.record(self.message("旧消息"))
        snapshot = self.store.recent_snapshot(ROOM)
        app._emit(self.message("/清空"))
        self.assertTrue(app.plugins.dispatch.call_args.args[0].data["context_reset"])
        self.assertFalse(self.store.snapshot_is_current(snapshot))


class NativeAtReceiveTests(unittest.TestCase):
    def test_cdata_and_multiple_members_are_parsed_without_guessing_names(self):
        self.assertEqual(parse_at_members("<msgsource><atuserlist><![CDATA[wxid_a,amy123,wxid_a]]></atuserlist></msgsource>"),
                         ("wxid_a", "amy123"))
        self.assertEqual(parse_at_members("<msgsource><atuserlist>filehelper,坏名字,wxid_b</atuserlist></msgsource>"), ("wxid_b",))
        for source in ("", "@机器人", "<msgsource>", "<!DOCTYPE a [<!ENTITY x 'abc'>]><a>&x;</a>", "x" * 4096):
            self.assertEqual(parse_at_members(source), ())

    def test_visible_labels_are_removed_on_unicode_at_boundary(self):
        self.assertEqual(strip_at_labels("@机器 人\u2005@小明\u2005请看看这句话"), "请看看这句话")
        self.assertEqual(strip_at_labels("普通 @example.com 文本"), "普通 @example.com 文本")

    def test_native_metadata_marks_only_actual_robot_group_at(self):
        native = daily.ROOT / "native"
        if str(native) not in sys.path:
            sys.path.insert(0, str(native))
        from text_service import NativeTextService
        from filehelper_service import ReceivedTextMessage
        service = NativeTextService(pid=123)
        service.router.set_account(ROBOT)
        msg = ReceivedTextMessage(type=1, sender=ROOM.encode(), receiver=ROBOT.encode(),
                                  text=(CAT + ":\n@机器人\u2005你好").encode(),
                                  mention_source=f"<msgsource><atuserlist>{ROBOT}</atuserlist></msgsource>".encode())
        self.assertTrue(service._map_message(msg)["is_at_self"])
        msg.mention_source = f"<msgsource><atuserlist>{OTHER}</atuserlist></msgsource>".encode()
        self.assertFalse(service._map_message(msg)["is_at_self"])
        msg.mention_source = b""
        self.assertFalse(service._map_message(msg)["is_at_self"])
        msg.sender, msg.text = CAT.encode(), "普通私聊".encode()
        msg.mention_source = f"<msgsource><atuserlist>{ROBOT}</atuserlist></msgsource>".encode()
        self.assertFalse(service._map_message(msg)["is_at_self"])


if __name__ == "__main__":
    unittest.main()
