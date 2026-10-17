"""群报、群梗及语录竞猜的数据库/插件集成测试；不连接微信和外部模型。"""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from plugins.group_fun import Plugin, QUIZ
from wxbot_framework.commands import effective_text
from wxbot_framework.group_fun import GroupReporter, quiz_candidates
from wxbot_framework.group_fun_store import GroupFunStore, source_ids
from wxbot_framework.llm import LLMError
from wxbot_framework.members import MemberError, MemberStore, SnapshotExpired
from wxbot_framework.room_state import RoomStateStore
from wxbot_framework.sdk import Message, PluginContext, ServiceFacade
from wxbot_framework.text import TextRouter

ROOT = Path(__file__).resolve().parent.parent
ROOM, OTHER_ROOM = "111@chatroom", "222@chatroom"
ROBOT, A, B, C = "wxid_test_robot", "wxid_test_a", "wxid_test_b", "wxid_test_c"


class Backend:
    session_id = "group-fun-tests"

    def __init__(self):
        self.sent = []
        self.succeeds = True

    def send_text(self, room, text):
        self.sent.append((room, text))
        return self.succeeds

    def stop(self):
        return True


class GroupFunTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / ".runtime/tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 10, 17, 10, tzinfo=timezone.utc)
        options = {"database_file": "data.sqlite3", "history_enabled": True, "daily_clear": False,
                   "owner_wxid": A, "groups": {ROOM: [], OTHER_ROOM: []}}
        self.config = self.root / "members.json"
        self.config.write_text(json.dumps(options), encoding="utf-8")
        self.members = MemberStore(self.config, ROBOT, now=lambda: self.now)
        self.members.initialize()
        for room in (ROOM, OTHER_ROOM):
            for sender, name in ((A, "阿甲"), (B, "阿乙"), (C, "阿丙")):
                self.members.register(name, room, sender)
        self.backend = Backend()
        self.service = ServiceFacade(text_delay={"enabled": False})
        self.service.bind(self.backend)
        self.state = RoomStateStore(self.members)
        self.ctx = PluginContext("group_fun", {}, self.service, [], member_store=self.members,
                                 room_state_store=self.state)
        self.calls = []
        self.ctx._llm = Mock(complete_json=self.model)
        self.sequence = 0
        self.reload()

    def tearDown(self):
        self.service.close()
        self.members.close()
        self.temp.cleanup()

    def reload(self):
        self.plugin = Plugin()
        self.plugin.on_load(self.ctx)
        self.plugin.rng.seed(42)

    def model(self, system, user):
        data = json.loads(user)
        self.calls.append(data)
        ids = [item["id"] for item in data["messages"]]
        if "term" in data:
            return {"meaning": "这些片段里是在拿做饭的事打趣。", "ids": ids[:1]}
        return {"topics": [{"summary": "大家聊了做饭的事。", "ids": ids[:2]}],
                "quotes": [{"ids": ids[:1]}], "followups": [], "memes": []}

    def record(self, text, sender=A, room=ROOM, **kwargs):
        self.sequence += 1
        message = Message(text, room, sender_id=sender, is_group=True, session_id=self.backend.session_id,
                          sequence=self.sequence, **kwargs)
        self.members.record(message)
        return message

    def send(self, text, sender=C, room=ROOM, **kwargs):
        message = self.record(text, sender, room, **kwargs)
        self.plugin.on_message(message, self.ctx)
        return message

    def text(self):
        return "\n".join(text for _, text in self.backend.sent)

    def seed(self):
        texts = (("我煮个面都能把锅烧出烟来", A), ("这顿饭的水平已经能直接参加厨神比赛了", B),
                 ("我那是故意留给你们发挥的空间懂不懂", A), ("饭还没熟你们已经给我发厨神奖状了", B),
                 ("这个锅今天算是替我背了全部的责任", A), ("我建议你先把火关了再继续发表感言", B))
        messages = [self.record(text, sender) for text, sender in texts]
        return [self.members.message_id(message) for message in messages]

    def quiz(self):
        return self.state.get(ROOM, QUIZ)

    def answer(self):
        state = self.quiz()
        question = state["questions"][state["index"]]
        author = self.plugin.store.rows(ROOM, [question["id"]])[0]["sender_id"]
        return question["options"].index(author) + 1, author

    def test_no_history_is_not_invented(self):
        self.send("/谁说的")
        self.send("/群报")
        self.send("/群梗 没有这个词")
        self.assertIn("素材够了", self.text())
        self.assertIn("不硬凑", self.text())
        self.assertIn("不编词源", self.text())
        self.assertEqual(self.calls, [])

    def test_quiz_has_real_quote_options_and_local_scoring(self):
        ids = self.seed()
        self.send("这话谁说的")
        state = self.quiz()
        self.assertEqual(len(state["questions"]), 5)
        self.assertTrue(all(item["id"] in ids for item in state["questions"]))
        self.assertNotIn("#" + str(state["questions"][0]["id"]), self.backend.sent[-1][1])
        option, _ = self.answer()
        self.send(f"/猜 {option}")
        self.assertEqual(self.quiz()["scores"], {C: 1})
        self.assertEqual(self.quiz()["phase"], "revealed")
        self.assertIn("相邻原话", self.text())
        self.assertIn("猜中了", self.text())
        self.assertEqual(self.calls, [])

    def test_wrong_guess_once_and_late_guess_cannot_score_again(self):
        self.seed()
        self.send("/谁说的")
        option, author = self.answer()
        self.send(f"/猜 {3 - option}")
        self.send(f"/猜 {option}")
        self.assertEqual(self.quiz()["scores"], {})
        self.assertIn("已经猜过", self.backend.sent[-1][1])
        self.send(f"/猜 {option}", sender=author)
        self.assertIn("本人认领", self.text())
        self.assertEqual(self.quiz()["scores"], {})
        self.send(f"/猜 {option}")
        self.assertIn("已揭晓", self.backend.sent[-1][1])

    def test_hint_and_scores_survive_plugin_reload(self):
        self.seed()
        self.send("/谁说的")
        self.send("/谁说的 提示")
        self.assertTrue(self.quiz()["hinted"])
        original = copy.deepcopy(self.quiz())
        self.reload()
        self.send("/谁说的 提示")
        self.assertEqual(self.quiz(), original)
        self.assertIn("已经用过", self.backend.sent[-1][1])
        option, _ = self.answer()
        self.send(f"/猜 {option}")
        self.reload()
        self.send("/谁说的 下一题")
        self.assertEqual(self.quiz()["scores"], {C: 1})
        self.assertEqual(self.quiz()["index"], 1)

    def test_failed_question_hint_guess_and_reveal_do_not_advance(self):
        self.seed()
        self.backend.succeeds = False
        self.send("/谁说的")
        self.assertIsNone(self.quiz())
        self.backend.succeeds = True
        self.send("/谁说的")
        original = copy.deepcopy(self.quiz())
        self.backend.succeeds = False
        self.send("/谁说的 提示")
        option, _ = self.answer()
        self.send(f"/猜 {option}")
        self.send("/谁说的 揭晓")
        self.assertEqual(self.quiz(), original)

    def test_stale_answer_cannot_score_new_round(self):
        self.seed()
        self.send("/谁说的")
        self.send("/谁说的 揭晓")
        self.send("/谁说的 下一题")
        option, _ = self.answer()
        count = len(self.backend.sent)
        self.send(f"/猜 {option}", received_at=self.plugin.ready[ROOM] - 1)
        self.assertEqual(len(self.backend.sent), count)
        self.assertEqual(self.quiz()["scores"], {})

    def test_quiz_ends_after_five_reveals_and_can_restart(self):
        self.seed()
        self.send("/谁说的")
        for index in range(5):
            self.send("/谁说的 揭晓")
            if index != 4:
                self.send("/谁说的 下一题")
        self.assertEqual(self.quiz()["phase"], "finished")
        self.assertIn("这局收工", self.text())
        self.send("/谁说的")
        self.assertEqual(self.quiz()["index"], 0)

    def test_opt_out_affects_current_question_and_survives_reload(self):
        self.seed()
        self.send("/谁说的")
        _, author = self.answer()
        self.send("/谁说的 排除我", sender=author)
        self.reload()
        self.assertIn(author, self.plugin.store.excluded(ROOM))
        self.backend.sent.clear()
        self.send("/谁说的 揭晓")
        self.assertEqual(self.quiz()["phase"], "void")
        self.assertIn("已不可用", self.text())
        self.assertNotIn("相邻原话", self.text())
        self.send("/谁说的 恢复我", sender=author)
        self.assertNotIn(author, self.plugin.store.excluded(ROOM))

    def test_opt_out_during_send_guard_cancels_quote(self):
        self.seed()
        self.send("/谁说的")
        _, author = self.answer()
        original = self.ctx.reply
        def intercept(message, text, **options):
            self.plugin.store.set_excluded(ROOM, author, True)
            return original(message, text, **options)
        self.ctx.reply = intercept
        self.backend.sent.clear()
        self.send("/谁说的 揭晓")
        self.assertEqual(self.backend.sent, [])
        self.assertEqual(self.quiz()["phase"], "question")

    def test_plain_chat_and_other_room_do_not_answer_or_leak_quiz(self):
        self.seed()
        self.send("/谁说的")
        count = len(self.backend.sent)
        for text in ("1", "哈哈哈哈", "阿甲", "你是真能吹"):
            self.send(text)
        self.assertEqual(len(self.backend.sent), count)
        self.send("/猜 1", room=OTHER_ROOM)
        self.assertIn("先发", self.backend.sent[-1][1])

    def test_report_is_manual_anchored_and_uses_only_selected_day(self):
        self.now -= timedelta(days=1)
        self.record("昨天那条消息不能出现在今天的群报里")
        self.now += timedelta(days=1)
        ids = self.seed()
        self.record("这条机器人消息不能作为群报原话", data={"bot_generated": True})
        self.send("今日群报")
        given = [item for call in self.calls for item in call["messages"]]
        self.assertEqual({item["id"] for item in given}, set(ids))
        self.assertNotIn(A, json.dumps(self.calls))
        self.assertIn("今天聊啥了", self.text())
        self.assertIn("出处 #", self.text())
        self.assertNotIn("后续呢", self.text())

    def test_report_yesterday_respects_beijing_midnight(self):
        self.now = datetime(2026, 10, 16, 15, 59, tzinfo=timezone.utc)
        previous = self.record("午夜之前这条属于北京昨天的群聊消息")
        self.now += timedelta(minutes=2)
        self.record("午夜之后这条已经属于北京今天的群聊消息")
        self.send("/群报 昨天")
        self.assertIn(str(self.members.message_id(previous)), self.text())
        self.assertIn("午夜之前", self.text())
        self.assertNotIn("午夜之后", self.text())

    def test_report_rejects_invented_source_and_keeps_real_quotes(self):
        self.seed()
        self.ctx._llm.complete_json = Mock(return_value={"quotes": [{"ids": [999999]}]})
        self.send("/群报")
        self.assertIn("暂时没能", self.text())
        self.assertNotIn("999999", self.text())
        self.backend.sent.clear()
        def rewritten(system, user):
            number = json.loads(user)["messages"][0]["id"]
            return {"quotes": [{"ids": [number], "summary": "凭空捏造的一句话"}]}
        self.ctx._llm.complete_json = Mock(side_effect=rewritten)
        self.send("/群报")
        self.assertNotIn("凭空捏造", self.text())
        self.assertIn("我煮个面", self.text())

    def test_report_clear_during_generation_cancels_delivery(self):
        self.seed()
        def clear(system, user):
            self.members.reset_context(ROOM)
            return self.model(system, user)
        self.ctx._llm.complete_json = Mock(side_effect=clear)
        self.send("/群报")
        self.assertEqual(self.backend.sent, [])

    def test_report_short_day_needs_no_model_and_truncation_is_visible(self):
        self.record("今天消息不多就翻翻这一句原话好了")
        self.send("/群报")
        self.assertEqual(self.calls, [])
        self.assertIn("聊天不多", self.text())
        self.backend.sent.clear()
        for index in range(55):
            self.record(f"这是今天第{index}条关于做饭的讨论消息")
        self.plugin.report_limit = 50
        self.send("/群报")
        self.assertIn("并非全天完整记录", self.text())
        self.assertEqual(sum(len(call["messages"]) for call in self.calls), 50)

    def test_meme_search_labels_inference_and_only_claims_archive_first(self):
        self.now -= timedelta(days=3)
        first = self.record("这个厨神称号我觉得你就先收下吧")
        self.now += timedelta(days=3)
        self.record("今天厨神又把锅给烧穿了吗", sender=B)
        self.send("/群梗 厨神")
        self.assertIn("当前存档最早", self.text())
        self.assertIn("根据这些片段推测（未确认）", self.text())
        self.assertIn(f"#{self.members.message_id(first)}", self.text())
        self.assertIn("不一定是", self.text())

    def test_meme_model_failure_falls_back_to_real_evidence(self):
        self.seed()
        self.ctx._llm.complete_json = Mock(side_effect=LLMError("模拟故障"))
        self.send("/群梗 厨神")
        self.assertIn("先保留能核对的原话", self.text())
        self.assertIn("这顿饭的水平", self.text())

    def test_meme_record_supplement_revision_permissions_and_history(self):
        ids = self.seed()
        evidence = ids[1]
        self.send(f"/群梗收录 厨神 | 只是调侃做饭水平的叫法 | #{evidence}", sender=B)
        entry = self.plugin.store.memes(ROOM)[0]
        number = entry["id"]
        self.send(f"/群梗补充 {number} | 那次其实没有烧坏锅 | #{evidence}", sender=C)
        self.send(f"/群梗修订 {number} | 冒名修改的说明 | #{evidence}", sender=C)
        self.assertIn("只有收录者或管理员", self.backend.sent[-1][1])
        self.send(f"/群梗修订 {number} | 大家开玩笑叫的称号 | #{evidence}", sender=B)
        self.reload()
        self.backend.sent.clear()
        self.send("/群梗 厨神")
        self.assertIn("群友修订（阿乙）", self.text())
        self.assertIn("群友补充（阿丙）", self.text())
        self.assertIn("共 3 次", self.text())
        self.assertNotIn("冒名修改", self.text())

    def test_meme_wrong_room_missing_source_and_name_mismatch_are_rejected(self):
        message = self.record("别的群里也有厨神这个说法", room=OTHER_ROOM)
        number = self.members.message_id(message)
        self.send(f"/群梗收录 厨神 | 群友说明 | #{number}")
        self.assertEqual(self.plugin.store.memes(ROOM), [])
        self.assertIn("部分原话不存在", self.text())
        ids = self.seed()
        self.send(f"/群梗收录 完全不匹配 | 群友说明 | #{ids[0]}")
        self.assertIn("需要包含", self.backend.sent[-1][1])
        self.assertEqual(self.plugin.store.memes(ROOM), [])

    def test_original_quote_and_meme_queries_cannot_cross_rooms(self):
        ids = self.seed()
        self.send(f"/原话 #{ids[0]}", room=OTHER_ROOM)
        self.assertNotIn("我煮个面", self.text())
        self.send(f"/群梗收录 厨神 | 群友说明 | #{ids[1]}")
        self.backend.sent.clear()
        self.send("/群梗 厨神", room=OTHER_ROOM)
        self.assertIn("没找到", self.text())
        self.assertNotIn("群友说明", self.text())

    def test_stored_meme_marks_deleted_sources_unverifiable(self):
        ids = self.seed()
        self.send(f"/群梗收录 厨神 | 群友提供的说明 | #{ids[1]}")
        self.members.clear(ROOM)
        self.backend.sent.clear()
        self.send("/群梗 厨神")
        self.assertIn("暂不能完整核验", self.text())
        self.assertNotIn("这顿饭的水平", self.text())

    def test_search_percent_and_underscore_are_literal(self):
        self.record("这条聊天里面有百分号%和下划线_两个字符")
        self.record("这条聊天没有对应特殊字符所以不该被检索")
        anchor = self.members.latest_id(ROOM) + 1
        self.assertEqual(len(self.plugin.store.search(ROOM, "%", before_id=anchor)), 1)
        self.assertEqual(len(self.plugin.store.search(ROOM, "_", before_id=anchor)), 1)

    def test_quiz_and_preferences_are_account_isolated(self):
        self.seed()
        self.plugin.store.set_excluded(ROOM, A, True)
        other = MemberStore(self.config, "wxid_other_robot", now=lambda: self.now)
        other.initialize()
        try:
            store = GroupFunStore(other)
            store.initialize()
            self.assertEqual(store.excluded(ROOM), set())
            self.assertEqual(store.search(ROOM, "厨神", before_id=99999), [])
            self.assertIsNone(RoomStateStore(other).get(ROOM, QUIZ))
        finally:
            other.close()

    def test_config_and_command_routing(self):
        for value in (True, 0, 49, 2001, "500"):
            self.ctx.config = {"report_limit": value}
            with self.subTest(value=value), self.assertRaises(ValueError):
                Plugin().on_load(self.ctx)
        router = TextRouter()
        router.convert(ROBOT, "filehelper", "init")
        for text in ("/群报", "/群梗 厨神", "/谁说的", "/猜 1", "/原话 1", "这话谁说的", "今日群报"):
            self.assertFalse(effective_text(text))
            self.assertTrue(router.convert(ROOM, ROBOT, ROBOT + ":\n" + text)["dispatch_to_plugins"])

    def test_candidates_exclude_duplicates_contacts_self_names_and_opted_out(self):
        self.seed()
        phone = "138" + "1234" + "5678"
        for text in ("我是阿甲这句话已经直接告诉你们是谁了", "这个链接看看https://example.com呀",
                     f"今天给我拨打{phone}这个号码", "重复转述会误导竞猜所以不要拿来出题", "重复转述会误导竞猜所以不要拿来出题"):
            self.record(text)
        rows = self.members.recent_snapshot(ROOM, limit=100).messages
        candidates = quiz_candidates(rows, {B})
        self.assertTrue(candidates)
        self.assertTrue(all(row["sender_id"] == A for row in candidates))
        self.assertTrue(all("阿甲" not in row["text"] and "https" not in row["text"] and "重复转述" not in row["text"]
                            and phone not in row["text"] for row in candidates))

    def test_source_parser_rejects_invalid_or_excessive_ids(self):
        self.assertEqual(source_ids("#12, 18，#12"), [12, 18])
        for value in ("", "-1", "1 OR 1=1", "#0", "1,2,3,4,5,6", "1.2"):
            with self.subTest(value=value), self.assertRaises(MemberError):
                source_ids(value)

    def test_new_commands_are_not_also_judged_by_idiom_game(self):
        from plugins.idiom_chain import Plugin as IdiomPlugin, Game, Chain
        idiom = IdiomPlugin()
        idiom.on_load(self.ctx)
        idiom.games[ROOM] = Game("事到如今", Chain("事到如今", ("事到如今",)))
        message = self.record("今日群报")
        idiom.on_message(message, self.ctx)
        self.assertEqual(self.backend.sent, [])
        self.assertEqual(self.calls, [])

    def test_quiz_does_not_accept_sender_spoofing_by_nickname(self):
        self.seed()
        self.send("/谁说的")
        _, author = self.answer()
        self.send("/谁说的 认领", sender=C)
        self.assertIn("不是你发的", self.backend.sent[-1][1])
        self.assertEqual(self.quiz()["phase"], "question")
        self.send("/谁说的 认领", sender=author)
        self.assertEqual(self.quiz()["scores"], {})
        self.assertEqual(self.quiz()["phase"], "revealed")

    def test_removed_quote_is_skipped_without_reading_another_room(self):
        self.seed()
        self.send("/谁说的")
        number = self.quiz()["questions"][0]["id"]
        with self.members._lock, self.members._connection() as db:
            db.execute("DELETE FROM messages WHERE account=? AND room=? AND id=?", (ROBOT, ROOM, number))
        self.backend.sent.clear()
        self.send("/谁说的 揭晓")
        self.assertIn("已不可用", self.text())
        self.send("/谁说的 下一题")
        self.assertEqual(self.quiz()["index"], 1)

    def test_all_outbound_parts_stay_within_native_utf8_limit(self):
        self.seed()
        self.send("/群报")
        self.send("/群梗 厨神")
        self.send("/谁说的")
        self.send("/谁说的 揭晓")
        self.assertTrue(all(0 < len(text.encode("utf-8")) <= 1023 for _, text in self.backend.sent))

    def test_original_query_does_not_send_context_deleted_while_waiting(self):
        ids = self.seed()
        original = self.ctx.reply
        def intercept(message, text, **options):
            with self.members._lock, self.members._connection() as db:
                db.execute("DELETE FROM messages WHERE id=?", (ids[0],))
            return original(message, text, **options)
        self.ctx.reply = intercept
        self.send(f"/原话 #{ids[1]}")
        self.assertEqual(self.backend.sent, [])

    def test_meme_does_not_emit_stale_source_after_deleted_during_render(self):
        ids = self.seed()
        self.send(f"/群梗收录 厨神 | 群友提交的说明 | #{ids[1]}")
        self.backend.sent.clear()
        original = self.plugin.store.rows
        calls = []
        def read_then_delete(*args, **kwargs):
            rows = original(*args, **kwargs)
            calls.append(1)
            if len(calls) == 1:
                with self.members._lock, self.members._connection() as db:
                    db.execute("DELETE FROM messages WHERE id=?", (ids[1],))
            return rows
        self.plugin.store.rows = read_then_delete
        self.send("/群梗 厨神")
        self.assertEqual(self.backend.sent, [])

    def test_report_does_not_emit_source_deleted_during_generation(self):
        ids = self.seed()
        def read_then_delete(system, user):
            result = self.model(system, user)
            with self.members._lock, self.members._connection() as db:
                db.execute("DELETE FROM messages WHERE id=?", (ids[0],))
            return result
        self.ctx._llm.complete_json = Mock(side_effect=read_then_delete)
        self.send("/群报")
        self.assertEqual(self.backend.sent, [])


if __name__ == "__main__":
    unittest.main()
