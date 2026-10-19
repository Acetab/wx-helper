"""持久化记忆、来源、权限、定时发送的行为验证；不连接微信或真实模型。"""
import json
import sqlite3
from contextlib import closing
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import test_chat_summary as daily
from plugins.knowledge import Plugin as KnowledgePlugin
from plugins.chat_summary import parse
from wxbot_framework.controls import prepare_reset
from wxbot_framework.feature_config import feature_options
from wxbot_framework.hourly import HourlyScheduler
from wxbot_framework.knowledge import KnowledgeStore
from wxbot_framework.llm import LLMError
from wxbot_framework.members import MemberError
from wxbot_framework.memory_worker import MemoryWorker
from wxbot_framework.config import ConfigStore
from wxbot_framework.runtime import BotApplication
from wxbot_framework.room_state import RoomStateStore

ROOM, CAT, OTHER, ROBOT = daily.ROOM, daily.CAT, daily.OTHER, daily.ROBOT


class MemoryTests(unittest.TestCase):
    setUp = daily.DailySummaryTests.setUp
    tearDown = daily.DailySummaryTests.tearDown
    new_store = daily.DailySummaryTests.new_store
    message = daily.DailySummaryTests.message
    model = daily.DailySummaryTests.model
    snapshot = daily.DailySummaryTests.snapshot

    def kb(self):
        knowledge = KnowledgeStore(self.store)
        knowledge.initialize()
        return knowledge

    def record(self, text, *, sender=CAT, sequence=0):
        msg = self.message(text, sender=sender, sequence=sequence)
        self.store.record(msg)
        return msg

    def put(self, kb, content="常聊吉他", *, wxid=CAT, slot="爱好"):
        self.record("一起聊吉他", sender=wxid)
        entity = kb.member_entity(ROOM, wxid)
        with kb._lock, kb._db():
            item = kb.put(ROOM, entity["id"], slot, "本人陈述", content, tags=["吉他"])
        return item

    def test_cross_day_history_context_and_snapshot_remain_available(self):
        self.record("昨晚聊吉他")
        snapshot = self.store.recent_snapshot(ROOM)
        self.now += timedelta(seconds=2)
        current = self.record("今天接着聊")
        self.assertEqual(len(self.store.recent_snapshot(ROOM).messages), 2)
        self.assertTrue(self.store.snapshot_is_current(snapshot))
        self.assertEqual(self.store.conversation_snapshot(current).messages[0]["text"], "昨晚聊吉他")

    def test_reset_preserves_archive_and_invalidates_inflight_snapshot(self):
        self.record("过去的话")
        snapshot = self.store.recent_snapshot(ROOM)
        prepared = prepare_reset(self.record("@机器人\u2005/清空"), self.store)
        self.assertTrue(prepared.data["context_reset"])
        current = self.record("新的提问")
        self.assertEqual(self.store.conversation_snapshot(current).messages, ())
        self.assertFalse(self.store.snapshot_is_current(snapshot))
        self.assertEqual(len(self.store.recent_snapshot(ROOM).messages), 2)

    def test_member_cannot_reset_group_context(self):
        msg = self.record("/清空", sender=OTHER)
        self.assertIn("管理员", prepare_reset(msg, self.store).data["context_error"])

    def test_recent_count_filters_before_limit_and_anchors_at_request(self):
        for text in ("第一条", "/绑定 昵称", "第二条", "@机器人\u2005/档案", "第三条"):
            self.record(text)
        request = self.record("/总结群聊 2")
        self.record("后来才来的消息")
        snapshot = self.store.recent_snapshot(ROOM, limit=2, before_id=self.store.message_id(request))
        self.assertEqual([r["text"] for r in snapshot.messages], ["第二条", "第三条"])

    def test_summary_parse_default_custom_and_mention(self):
        self.assertEqual(parse("/总结群聊"), (True, None, "recent", 200))
        self.assertEqual(parse("@机器人\u2005/总结 测试成员 500"), (False, "测试成员", "recent", 500))
        self.assertEqual(parse("/总结群聊 今天"), (True, None, "today", 200))
        for text in ("/总结群聊 0", "/总结群聊 5001", "/总结"):
            with self.assertRaises(MemberError):
                parse(text)

    def test_unknown_ids_are_registered_and_binding_keeps_history(self):
        self.record("没绑定也能存", sender="wxid_new")
        self.store.register("新朋友", ROOM, "wxid_new")
        self.assertEqual(self.store.history("新朋友", ROOM)[0]["text"], "没绑定也能存")
        self.assertTrue(self.store._connection().execute("SELECT first_seen FROM room_members WHERE wxid='wxid_new'").fetchone()[0])

    def test_binding_reminder_seven_day_cooldown_and_binding_stops_it(self):
        self.assertTrue(self.store.binding_reminder_due(ROOM, "wxid_new"))
        self.assertFalse(self.store.binding_reminder_due(ROOM, "wxid_new"))
        self.now += timedelta(days=8)
        self.assertTrue(self.store.binding_reminder_due(ROOM, "wxid_new"))
        self.store.register("新朋友", ROOM, "wxid_new")
        self.assertFalse(self.store.binding_reminder_due(ROOM, "wxid_new"))

    def test_human_correction_and_forget_cannot_be_overwritten(self):
        kb = self.kb()
        item_id = self.put(kb)
        kb.edit(ROOM, item_id, CAT, content="喜欢贝斯")
        self.put(kb, "常聊吉他")
        self.assertIn("喜欢贝斯", [i["content"] for i in kb.search(ROOM, "", entity_id=kb.member_entity(ROOM, CAT)["id"])])
        kb.edit(ROOM, item_id, CAT, forget=True)
        self.put(kb, "又说喜欢吉他")
        self.assertEqual(kb.search(ROOM, "吉他"), [])
        self.assertTrue(self.store.history("测试成员", ROOM))

    def test_member_cannot_edit_other_person_or_shared_joke(self):
        kb = self.kb()
        item_id = self.put(kb, wxid=OTHER)
        with self.assertRaises(MemberError):
            kb.edit(ROOM, item_id, "wxid_random", content="冒名修改")
        kb.edit(ROOM, item_id, CAT, content="管理员纠正")

    def test_memory_isolated_by_room_and_account(self):
        kb = self.kb()
        item = self.put(kb)
        self.assertEqual(kb.search(daily.OTHER_ROOM, "吉他"), [])
        other = KnowledgeStore(self.new_store("wxid_second_robot"))
        other.initialize()
        self.assertEqual(other.search(ROOM, "吉他"), [])
        with self.assertRaises(MemberError):
            other.edit(ROOM, item, ROBOT, forget=True)

    def test_search_skips_missing_entities_without_losing_valid_items(self):
        kb = self.kb()
        valid = self.put(kb)
        self.put(kb, "无效引用", wxid=OTHER)
        missing = kb.member_entity(ROOM, OTHER)["id"]
        with kb._db() as db:
            db.execute("DELETE FROM knowledge_entities WHERE id=?", (missing,))
        entity = kb.member_entity(ROOM, CAT)["id"]
        for query, options in (("吉他", {}), ("", {"entity_id": entity})):
            with self.subTest(query=query):
                self.assertEqual([item["id"] for item in kb.search(ROOM, query, **options)], [valid])

    def test_search_does_not_resolve_an_item_against_another_room_or_account(self):
        kb = self.kb()
        valid = self.put(kb)
        bad = self.put(kb, "吉他", wxid=OTHER)
        for foreign in (kb.entity(daily.OTHER_ROOM, "其他群人物", pinned=True),
                        KnowledgeStore(self.new_store("wxid_second_robot"))):
            if isinstance(foreign, KnowledgeStore):
                foreign.initialize()
                foreign = foreign.entity(ROOM, "其他账号人物", pinned=True)
            with kb._db() as db:
                db.execute("UPDATE knowledge_items SET entity_id=? WHERE id=?", (foreign["id"], bad))
            with self.subTest(entity=foreign["id"]):
                self.assertEqual([item["id"] for item in kb.search(ROOM, "吉他")], [valid])

    def test_room_state_snapshot_is_detached_and_isolated_by_room_and_account(self):
        state = RoomStateStore(self.store)
        state.put(ROOM, "idiom_chain", {"current": "一心一意"})
        state.put(daily.OTHER_ROOM, "idiom_chain", {"current": "异想天开"})
        before = state.snapshot(ROOM)
        state.put(ROOM, "idiom_chain", {"current": "发扬光大"})
        self.assertEqual(before, {"idiom_chain": {"current": "一心一意"}})
        self.assertEqual(state.snapshot(daily.OTHER_ROOM), {"idiom_chain": {"current": "异想天开"}})
        self.assertEqual(RoomStateStore(self.new_store("wxid_second_robot")).snapshot(ROOM), {})

    def test_same_nickname_does_not_link_space_person_to_member(self):
        kb = self.kb()
        person = kb.entity(ROOM, "示例哥", pinned=True)
        self.record("我是示例哥", sender=OTHER)
        self.store.register("示例哥", ROOM, OTHER)
        member = kb.member_entity(ROOM, OTHER)
        self.assertNotEqual(person["id"], member["id"])
        self.assertIsNone(kb.find_person(ROOM, "示例哥")["wxid"])
        kb.link(ROOM, OTHER, "示例哥", CAT)
        self.assertEqual(kb.find_person(ROOM, "示例哥")["wxid"], OTHER)

    def test_seed_import_is_idempotent_and_jokes_keep_classification(self):
        kb = self.kb()
        seed = self.root / "seed.json"
        seed.write_text(json.dumps({"groups": {ROOM: [{"name": "示例人物", "aliases": ["示例哥"],
            "memories": [{"key": "回归", "kind": "群内玩笑", "content": "离线后盛大回归"}]}]}}), encoding="utf-8")
        kb.import_seed(seed)
        revision = kb.search(ROOM, "示例哥")[0]["revision"]
        kb.import_seed(seed)
        items = kb.search(ROOM, "示例哥")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "群内玩笑")
        self.assertTrue(items[0]["sources"][0]["origin"].startswith("seed:"))
        self.assertEqual(items[0]["revision"], revision)

    def test_seed_import_preserves_forgotten_item_and_quarantined_person(self):
        kb = self.kb()
        seed = self.root / "seed.json"
        seed.write_text(json.dumps({"groups": {ROOM: [{"name": "种子人物", "memories": [
            {"key": "回归", "kind": "群内玩笑", "content": "种子内容"}]}]}}), encoding="utf-8")
        kb.import_seed(seed)
        item = kb.search(ROOM, "种子人物")[0]
        entity_id = item["entity_id"]
        kb.edit(ROOM, item["id"], CAT, forget=True)
        kb.quarantine_person(ROOM, f"#{entity_id}", CAT)
        with kb._lock, kb._db() as db:
            db.execute("UPDATE knowledge_entities SET pinned=0,aliases='[]' WHERE id=?", (entity_id,))
        before = kb.admin_entity(ROOM, f"#{entity_id}")
        seed.write_text(json.dumps({"groups": {ROOM: [{"name": "种子人物", "aliases": ["新别名"],
            "memories": [
                {"key": "回归", "kind": "群内玩笑", "content": "种子内容"},
                {"key": "隔离后新增", "kind": "群内玩笑", "content": "不应写入"}]}]}}), encoding="utf-8")
        kb.import_seed(seed)
        self.assertEqual(kb.admin_item(ROOM, item["id"])["status"], "forgotten")
        after = kb.admin_entity(ROOM, f"#{entity_id}")
        self.assertEqual(after["status"], "quarantined")
        self.assertEqual(after["pinned"], before["pinned"])
        self.assertEqual(after["aliases"], before["aliases"])
        _, count, rows = kb.admin_items(ROOM, f"#{entity_id}")
        self.assertEqual(count, 1)
        self.assertEqual([row["slot"] for row in rows], ["回归"])

    def test_group_seed_lands_in_group_memory_and_is_idempotent(self):
        kb = self.kb()
        seed = self.root / "group-seed.json"
        seed.write_text(json.dumps({"groups": {ROOM: [
            {"name": "本群", "aliases": [], "memories": [
                {"key": "群梗", "kind": "群内玩笑", "content": "大家都爱摸鱼"}]},
            {"name": "示例甲", "aliases": ["甲哥"], "memories": [
                {"key": "爱好", "kind": "人工确认", "content": "喜欢吉他"}]},
        ]}}), encoding="utf-8")
        kb.import_seed(seed)
        kb.import_seed(seed)
        kb.initialize(seed)
        keys = [entity["entity_key"] for entity in kb._entities(ROOM)]
        self.assertNotIn("person:本群", keys)
        self.assertIn("group:本群", keys)
        group = kb.group_entity(ROOM)
        count = kb._db().execute("SELECT COUNT(*) FROM knowledge_items WHERE entity_id=? AND slot='群梗'",
                                 (group["id"],)).fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(len(kb.search(ROOM, "摸鱼")), 1)
        self.assertEqual(kb.find_person(ROOM, "甲哥")["name"], "示例甲")

    def test_manual_sqlite_alias_edits_self_heal_on_initialize(self):
        kb = self.kb()
        seed = self.root / "seed.json"
        seed.write_text(json.dumps({"groups": {ROOM: [
            {"name": "人物甲", "aliases": ["外号A"], "memories": []},
            {"name": "人物乙", "aliases": ["外号B"], "memories": []},
        ]}}), encoding="utf-8")
        kb.import_seed(seed)
        with kb._lock, kb._db() as db:
            db.execute("UPDATE knowledge_entities SET aliases='' WHERE account=? AND room=? AND name='人物甲'", (ROBOT, ROOM))
            db.execute("""UPDATE knowledge_entities SET aliases='["Zeg", "外号A", "外号B"]'
                WHERE account=? AND room=? AND name='人物乙'""", (ROBOT, ROOM))
        kb.initialize(seed)
        self.assertEqual(kb.find_person(ROOM, "Zeg")["name"], "人物乙")
        self.assertEqual(kb.find_person(ROOM, "外号A")["name"], "人物乙")
        self.assertEqual(json.loads(kb.find_person(ROOM, "人物甲")["aliases"]), [])

    def test_people_are_created_only_by_admin_confirmation(self):
        kb = self.kb()
        # 随口提到的名字不再建档，也不会进入检索与模型上下文。
        with self.assertRaises(MemberError):
            kb.entity(ROOM, "新人物")
        self.assertIsNone(kb.resolve_person(ROOM, "新人物"))
        self.assertEqual(kb.lookup_people(ROOM, "新人物是谁"), [])
        # 管理员确认后才建档，且可补别名并立即可见。
        promoted = kb.entity(ROOM, "新人物", aliases=["新外号"], pinned=True)
        self.assertTrue(kb.visible(promoted))
        self.assertEqual(kb.find_person(ROOM, "新外号")["id"], promoted["id"])
        self.assertEqual(kb.lookup_people(ROOM, "新人物是谁")[0]["name"], "新人物")

    def test_worker_persists_source_and_cursor_atomically(self):
        kb = self.kb()
        msg = self.record("我喜欢吉他")
        msg_id = self.store.message_id(msg)
        model = Mock(complete_json=Mock(return_value={"entities": [], "memories": [{
            "subject_type": "speaker", "subject": "P1", "key": "爱好", "kind": "本人陈述",
            "content": "喜欢吉他", "tags": ["吉他"], "message_ids": [msg_id]}]}))
        worker = MemoryWorker(kb, model)
        worker.process(ROOM, kb.batch(ROOM))
        self.assertEqual(kb.cursor(ROOM), msg_id)
        self.assertEqual(kb.search(ROOM, "吉他")[0]["sources"][0]["message_id"], msg_id)
        self.assertNotIn(CAT, model.complete_json.call_args.args[1])

    def test_worker_invalid_source_does_not_advance_or_leave_partial_memories(self):
        kb = self.kb()
        self.record("真实消息")
        worker = MemoryWorker(kb, Mock(complete_json=Mock(return_value={"entities": [], "memories": [{
            "subject_type": "speaker", "subject": "P1", "key": "假来源", "kind": "本人陈述",
            "content": "没有发生", "tags": [], "message_ids": [999999]}]})))
        with self.assertRaises(LLMError):
            worker.process(ROOM, kb.batch(ROOM))
        self.assertEqual(kb.cursor(ROOM), 0)
        self.assertEqual(kb.search(ROOM, ""), [])

    def test_worker_does_not_learn_control_or_robot_output(self):
        kb = self.kb()
        self.record("@机器人\u2005/绑定 新名字")
        self.store.record(self.message("机器人自己编的话", data={"bot_generated": True}))
        client = Mock()
        worker = MemoryWorker(kb, client)
        worker.process(ROOM, kb.batch(ROOM))
        client.complete_json.assert_not_called()
        self.assertGreater(kb.cursor(ROOM), 0)

    def test_group_memory_needs_three_messages_two_speakers(self):
        kb = self.kb()
        group = kb.group_entity(ROOM)
        first = self.record("这是群梗：摸鱼", sender=CAT, sequence=1)
        second = self.record("又开始摸鱼了", sender=OTHER, sequence=2)
        first_id, second_id = self.store.message_id(first), self.store.message_id(second)
        with kb._lock, kb._db():
            item = kb.put(ROOM, group["id"], "摸鱼梗", "群内玩笑", "本群把偷懒叫摸鱼",
                          sources=[{"id": first_id, "sender_id": CAT, "received_at": "2026-10-10"},
                                   {"id": second_id, "sender_id": OTHER, "received_at": "2026-10-10"}])
        self.assertEqual(kb.search(ROOM, "摸鱼"), [])
        self.assertEqual(kb.admin_item(ROOM, item)["status"], "candidate")
        third = self.record("摸鱼摸鱼", sender=CAT, sequence=3)
        third_id = self.store.message_id(third)
        with kb._lock, kb._db():
            kb.put(ROOM, group["id"], "摸鱼梗", "群内玩笑", "本群把偷懒叫摸鱼",
                    sources=[{"id": third_id, "sender_id": CAT, "received_at": "2026-10-10"}])
        self.assertEqual(len(kb.search(ROOM, "摸鱼")), 1)
        self.assertEqual(kb.admin_item(ROOM, item)["status"], "active")

    def test_stale_group_candidates_are_archived_but_kept(self):
        kb = self.kb()
        group = kb.group_entity(ROOM)
        msg = self.record("只有一条证据")
        msg_id = self.store.message_id(msg)
        with kb._lock, kb._db():
            item = kb.put(ROOM, group["id"], "孤立梗", "群内玩笑", "只有一条证据的梗",
                          sources=[{"id": msg_id, "sender_id": CAT, "received_at": "2026-10-10"}])
        self.assertEqual(kb.admin_item(ROOM, item)["status"], "candidate")
        with kb._lock, kb._db() as db:
            db.execute("UPDATE knowledge_items SET updated_at='2026-01-01T00:00:00+00:00' WHERE id=?", (item,))
        self.assertEqual(kb.archive_stale_candidates(ROOM), 1)
        archived = kb.admin_item(ROOM, item)
        self.assertEqual(archived["status"], "archived")
        self.assertEqual(archived["content"], "只有一条证据的梗")
        self.assertTrue(kb._db().execute("SELECT 1 FROM knowledge_sources WHERE item_id=?", (item,)).fetchone())

    def test_new_evidence_refreshes_group_candidate_and_prevents_archive(self):
        kb = self.kb()
        group = kb.group_entity(ROOM)
        first = self.record("第一条证据", sender=CAT, sequence=1)
        with kb._lock, kb._db():
            item = kb.put(ROOM, group["id"], "持续话题", "群内玩笑", "持续有证据的话题",
                          sources=[{"id": self.store.message_id(first), "sender_id": CAT,
                                    "received_at": "2026-10-10"}])
        with kb._lock, kb._db() as db:
            db.execute("UPDATE knowledge_items SET updated_at='2026-01-01T00:00:00+00:00' WHERE id=?", (item,))
        second = self.record("第二条证据", sender=OTHER, sequence=2)
        with kb._lock, kb._db():
            kb.put(ROOM, group["id"], "持续话题", "群内玩笑", "持续有证据的话题",
                   sources=[{"id": self.store.message_id(second), "sender_id": OTHER,
                             "received_at": "2026-10-10"}])
        refreshed = kb.admin_item(ROOM, item)
        self.assertNotEqual(refreshed["updated_at"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(refreshed["status"], "candidate")
        self.assertEqual(kb.archive_stale_candidates(ROOM), 0)

    def test_archived_group_candidate_revives_with_new_evidence(self):
        kb = self.kb()
        group = kb.group_entity(ROOM)
        first = self.record("旧证据", sender=CAT, sequence=1)
        with kb._lock, kb._db():
            item = kb.put(ROOM, group["id"], "复活话题", "群内玩笑", "曾被归档的话题",
                          sources=[{"id": self.store.message_id(first), "sender_id": CAT,
                                    "received_at": "2026-10-10"}])
        with kb._lock, kb._db() as db:
            db.execute("UPDATE knowledge_items SET updated_at='2026-01-01T00:00:00+00:00' WHERE id=?", (item,))
        self.assertEqual(kb.archive_stale_candidates(ROOM), 1)
        self.assertEqual(kb.admin_item(ROOM, item)["status"], "archived")
        second = self.record("新证据", sender=OTHER, sequence=2)
        with kb._lock, kb._db():
            kb.put(ROOM, group["id"], "复活话题", "群内玩笑", "曾被归档的话题",
                   sources=[{"id": self.store.message_id(second), "sender_id": OTHER,
                             "received_at": "2026-10-10"}])
        self.assertEqual(kb.admin_item(ROOM, item)["status"], "candidate")
        self.assertEqual(kb.archive_stale_candidates(ROOM), 0)

    def test_history_window_stops_after_thirty_minutes_silence(self):
        self.record("很久以前的话")
        self.now += timedelta(minutes=45)
        self.record("中间的话")
        current = self.record("现在又说话了")
        history = [row["text"] for row in self.store.conversation_snapshot(current).history]
        self.assertEqual(history, ["中间的话"])

    def test_style_requires_thirty_samples_and_preserves_source_ids(self):
        kb = self.kb()
        client = Mock(complete_json=Mock(return_value={"style": "短句较多，爱聊吉他，常用反问。"}))
        worker = MemoryWorker(kb, client)
        for number in range(29):
            self.record(f"吉他练习{number}")
        worker.process_styles(ROOM)
        client.complete_json.assert_not_called()
        self.record("第三十条吉他练习")
        worker.process_styles(ROOM)
        self.assertEqual(kb.style(ROOM, CAT)["sample_count"], 30)
        worker.process_styles(ROOM)
        self.assertEqual(client.complete_json.call_count, 1)

    def test_help_and_edit_permissions_use_real_sender(self):
        kb = self.kb()
        plugin = KnowledgePlugin()
        plugin.on_load(self.ctx)
        self.ctx._knowledge = kb
        plugin.on_message(self.record("@机器人\u2005/help"), self.ctx)
        self.assertIn("/记忆", "".join(row[1] for row in self.backend.sent))

    def test_help_hides_admin_commands_from_ordinary_members(self):
        kb = self.kb()
        plugin = KnowledgePlugin()
        plugin.on_load(self.ctx)
        self.ctx._knowledge = kb
        plugin.on_message(self.record("@机器人\u2005/help", sender=OTHER), self.ctx)
        text = "".join(row[1] for row in self.backend.sent)
        self.assertIn("/记忆", text)
        self.assertNotIn("/绑定", text)
        self.assertNotIn("/管理", text)

    def test_other_members_can_look_up_a_bound_person_by_name(self):
        kb = self.kb()
        person = kb.entity(ROOM, "朱宏旭", aliases=["小朱"], pinned=True)
        self.record("我是朱宏旭", sender=OTHER)
        kb.link(ROOM, OTHER, "朱宏旭", CAT)
        profiles = kb.lookup_people(ROOM, "朱宏旭是谁？")
        self.assertEqual(profiles[0]["name"], "朱宏旭")
        self.assertTrue(profiles[0]["bound"])
        self.assertIn("小朱", profiles[0]["aliases"])
        self.assertEqual(person["id"], kb.find_person(ROOM, "朱宏旭")["id"])
        # 未建档或已隔离的名字不会凭空出现
        self.assertEqual(kb.lookup_people(ROOM, "另一个人是谁？"), [])

    def test_profile_and_memory_replies_hide_internal_fields(self):
        kb = self.kb()
        plugin = KnowledgePlugin()
        plugin.on_load(self.ctx)
        self.ctx._knowledge = kb
        person = kb.entity(ROOM, "朱宏旭", aliases=["小朱"], pinned=True)
        kb.add_memory(ROOM, person["id"], "身份", "人工确认", "群里的吉他手", CAT)
        plugin.on_message(self.record("@机器人\u2005/档案 小朱"), self.ctx)
        text = "".join(row[1] for row in self.backend.sent)
        self.assertIn("朱宏旭", text)
        self.assertIn("群里的吉他手", text)
        for hidden in ("entity_key", "learning_floor", "{", "}", "wxid"):
            self.assertNotIn(hidden, text)

    def test_migration_backs_up_existing_database_once(self):
        legacy = self.root / "legacy.sqlite3"
        with closing(sqlite3.connect(legacy)) as db:
            db.execute("CREATE TABLE sample(value TEXT)")
            db.execute("INSERT INTO sample VALUES('保留数据')")
            db.commit()
        from wxbot_framework.database import backup_before_migration
        with closing(sqlite3.connect(legacy)) as db:
            backup = backup_before_migration(db, legacy)
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute("SELECT value FROM sample").fetchone()[0], "保留数据")
        self.assertIsNone(backup_before_migration(self.store._connection(), self.store.path))

    def test_invalid_background_config_is_rejected(self):
        for config in ({"memory": []}, {"hourly_chat": {"start_hour": 24}},
                       {"hourly_chat": {"stale_candidate_minutes": 0}},
                       {"memory": {"batch_size": True}}, {"memory": {"flush_seconds": 0}}):
            with self.assertRaises(ValueError):
                feature_options(config)

    def test_end_hour_24_is_accepted(self):
        _, hourly = feature_options({"hourly_chat": {"end_hour": 24}})
        self.assertEqual(hourly["end_hour"], 24)

    def test_narrow_windows_do_not_require_evening_start_inside(self):
        for settings in ({"start_hour": 22, "end_hour": 23},
                         {"start_hour": 23, "end_hour": 24},
                         {"start_hour": 19, "end_hour": 23}):
            with self.subTest(settings=settings):
                _, hourly = feature_options({"hourly_chat": settings})
                self.assertEqual(hourly["start_hour"], settings["start_hour"])
                self.assertEqual(hourly["end_hour"], settings["end_hour"])

    def test_correcting_a_joke_does_not_turn_it_into_a_real_event(self):
        kb = self.kb()
        entity = kb.entity(ROOM, "示例哥", pinned=True)
        with kb._lock, kb._db():
            item_id = kb.put(ROOM, entity["id"], "归来", "群内玩笑", "离线归来梗")
        kb.edit(ROOM, item_id, CAT, content="只是夸张的上线贺告")
        self.assertEqual(kb.search(ROOM, "示例哥")[0]["kind"], "群内玩笑")

    def test_manual_edit_cancels_pending_reply_without_erasing_context(self):
        kb = self.kb()
        item_id = self.put(kb)
        snapshot = self.store.recent_snapshot(ROOM)
        kb.edit(ROOM, item_id, CAT, content="改为聊贝斯")
        self.assertFalse(self.store.snapshot_is_current(snapshot))
        current = self.record("新提问")
        self.assertTrue(self.store.conversation_snapshot(current).messages)

    def test_invalid_seed_is_checked_before_importing_any_profile(self):
        kb = self.kb()
        seed = self.root / "bad-seed.json"
        seed.write_text(json.dumps({"groups": {ROOM: [
            {"name": "可用人物", "memories": [{"key": "主题", "content": "不应该部分导入"}]},
            {"name": "错误人物", "memories": [{"key": "主题", "content": "错误", "kind": "未知类别"}]}]}}), encoding="utf-8")
        with self.assertRaises(MemberError):
            kb.import_seed(seed)
        self.assertEqual(kb.search(ROOM, "可用人物"), [])

    def test_batch_collects_effective_messages_past_control_messages(self):
        kb = self.kb()
        self.record("第一条有效")
        for _ in range(55):
            self.record("/help")
        self.record("第二条有效")
        rows = kb.batch(ROOM, limit=2)
        self.assertEqual(len(rows), 57)
        self.assertEqual(rows[-1]["text"], "第二条有效")

    def test_partial_batch_flushes_after_ten_minutes(self):
        kb = self.kb()
        clock = [0]
        client = Mock(complete_json=Mock(return_value={"entities": [], "memories": []}))
        worker = MemoryWorker(kb, client, clock=lambda: clock[0])
        worker.tick()
        self.record("不足一批的聊天")
        clock[0] = 599
        worker.tick()
        client.complete_json.assert_not_called()
        clock[0] = 600
        worker.tick()
        self.assertEqual(client.complete_json.call_count, 1)

    def test_duplicate_alias_does_not_guess_identity(self):
        kb = self.kb()
        kb.entity(ROOM, "人物甲", aliases=["小哥"], pinned=True)
        with self.assertRaises(MemberError):
            kb.entity(ROOM, "人物乙", aliases=["小哥"], pinned=True)
        self.assertEqual(kb.find_person(ROOM, "小哥")["name"], "人物甲")

    def test_learner_cannot_mark_generated_fact_as_human_confirmed(self):
        kb = self.kb()
        message_id = self.store.message_id(self.record("普通聊天"))
        client = Mock(complete_json=Mock(return_value={"entities": [], "memories": [{
            "subject_type": "speaker", "subject": "P1", "key": "结论", "kind": "人工确认",
            "content": "模型自封确认", "message_ids": [message_id]}]}))
        with self.assertRaises(LLMError):
            MemoryWorker(kb, client).process(ROOM, kb.batch(ROOM))
        self.assertEqual(kb.cursor(ROOM), 0)

    def test_runtime_starts_and_stops_background_services_without_wechat(self):
        self.record("用于离线集成验证的聊天")
        config = json.loads((daily.ROOT / "bot_config.example.json").read_text(encoding="utf-8-sig"))
        config.update(wechat_account=ROBOT, members_config_file="members.json", console=False, maintenance={},
                      memory={"enabled": True}, hourly_chat={"enabled": True})
        for options in config["plugins"].values():
            options["enabled"] = False
        config_path = self.root / "bot_config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        (self.root / "plugins").mkdir()
        app = BotApplication(self.root, ConfigStore(config_path))
        app._connect = Mock()
        client = Mock(complete_json=Mock(return_value={"entities": [], "memories": []}))
        with patch("wxbot_framework.llm.LLMClient.from_file", return_value=client):
            app.run(seconds=0.35)
        self.assertFalse(app.memory_worker.thread.is_alive())
        self.assertFalse(app.hourly_scheduler.thread.is_alive())
        client.complete_json.assert_called()
        self.assertGreater(self.kb().cursor(ROOM), 0)


class HourlyTests(unittest.TestCase):
    setUp = daily.DailySummaryTests.setUp
    tearDown = daily.DailySummaryTests.tearDown
    new_store = daily.DailySummaryTests.new_store
    message = daily.DailySummaryTests.message
    model = daily.DailySummaryTests.model
    snapshot = daily.DailySummaryTests.snapshot
    record = MemoryTests.record
    kb = MemoryTests.kb

    def scheduler(self, client=None, **options):
        self.now = self.now.replace(hour=2, minute=0, second=0)  # 北京10:00。
        client = client or Mock(complete_json=Mock(return_value={
            "speak": True, "topic": "吉他练习", "text": "刚才聊到吉他，你们最近在练什么曲子？"}))
        options.setdefault("run_id", "run-one")
        return HourlyScheduler(self.kb(), self.service, client, now=lambda: self.now, **options)

    def activity(self, scheduler):
        self.record("最近在练吉他", sender=CAT)
        self.record("我想学一首新曲子", sender=OTHER)
        self.now += timedelta(minutes=1)
        scheduler.next_candidate[ROOM] = self.now
        scheduler.tick()

    def candidate_statuses(self, scheduler):
        with scheduler.knowledge._lock:
            return [row[0] for row in scheduler.knowledge._db().execute(
                "SELECT status FROM proactive_candidates WHERE account=? AND room=? ORDER BY id",
                (ROBOT, ROOM))]

    def test_active_hour_sends_once_with_no_raw_member_ids(self):
        scheduler = self.scheduler()
        self.record("最近在练吉他", sender=CAT)
        self.record("我想学一首新曲子", sender=OTHER)
        self.now += timedelta(minutes=1)
        scheduler.next_candidate[ROOM] = self.now
        scheduler.tick()
        self.assertEqual(len(self.backend.sent), 1)
        self.assertNotIn(CAT, scheduler.model.client.complete_json.call_args.args[1])
        self.assertNotIn("@", self.backend.sent[0][1])
        result = json.loads(scheduler.model.client.complete_json.call_args.args[1])
        self.assertEqual(set(result), {"recent", "knowledge", "recent_topics"})
        self.assertEqual(scheduler.model.client.complete_json.call_count, 1)
        self.assertEqual(self.candidate_statuses(scheduler), ["sent"])

    def test_empty_room_and_only_commands_do_not_generate(self):
        scheduler = self.scheduler()
        self.record("/help")
        self.store.record(self.message("机器人输出", data={"bot_generated": True}))
        scheduler.next_candidate[ROOM] = self.now
        scheduler.tick()
        self.assertEqual(self.backend.sent, [])
        scheduler.model.client.complete_json.assert_not_called()

    def test_midnight_is_silent(self):
        scheduler = self.scheduler(run_id="night")
        self.now = self.now.replace(hour=15, minute=59, second=59)
        self.record("夜间聊天", sender=CAT)
        self.record("夜间接话", sender=OTHER)
        scheduler.next_candidate[ROOM] = self.now
        scheduler.tick()
        self.assertEqual(scheduler.next_candidate[ROOM].hour, 1)  # 北京次日09:00。
        self.assertEqual(self.backend.sent, [])
        scheduler.model.client.complete_json.assert_not_called()

    def test_offline_does_not_catch_up_after_reconnect(self):
        scheduler = self.scheduler()
        self.service.close()
        self.record("离线前的话一", sender=CAT)
        self.record("离线前的话二", sender=OTHER)
        scheduler.next_candidate[ROOM] = self.now
        scheduler.tick()
        self.assertEqual(self.backend.sent, [])
        scheduler.model.client.complete_json.assert_not_called()
        self.assertGreater(scheduler.next_candidate[ROOM], self.now)
        self.backend = daily.Backend()
        self.service.bind(self.backend)
        scheduler.tick()
        self.assertEqual(self.backend.sent, [])
        scheduler.model.client.complete_json.assert_not_called()

    def test_repeated_topic_is_skipped_next_hour(self):
        scheduler = self.scheduler()
        self.activity(scheduler)
        scheduler.model.client = Mock(complete_json=Mock(return_value={
            "speak": True, "topic": "吉他练习", "text": "吉他又练了一会，你们下次准备弹什么？"}))
        self.now += timedelta(minutes=46)
        self.record("吉他又练了一会", sender=CAT)
        self.record("想换一首慢歌", sender=OTHER)
        scheduler.next_candidate[ROOM] = self.now
        scheduler.tick()
        self.assertEqual(len(self.backend.sent), 1)
        self.assertEqual(scheduler.model.client.complete_json.call_count, 1)
        self.assertEqual(self.candidate_statuses(scheduler)[-1], "failed")

    def test_reset_during_generation_prevents_send(self):
        def generate(*args, **kwargs):
            self.store.reset_context(ROOM)
            return {"speak": True, "topic": "过时话题",
                    "text": "这句话不应发出，因为生成期间上下文已经清空。"}
        scheduler = self.scheduler(Mock(complete_json=generate))
        self.activity(scheduler)
        self.assertEqual(self.backend.sent, [])
        self.assertEqual(self.candidate_statuses(scheduler)[-1], "failed")

    def test_reset_during_send_pacing_prevents_native_send(self):
        scheduler = self.scheduler()
        cancel = Mock(is_set=lambda: False)
        cancel.wait = lambda _: self.store.reset_context(ROOM) or False
        self.service._cancel = cancel
        self.record("最近在练吉他", sender=CAT)
        self.record("我想学一首新曲子", sender=OTHER)
        scheduler.next_candidate[ROOM] = self.now
        scheduler.tick()
        self.assertEqual(self.backend.sent, [])

    def test_previous_run_scheduled_candidate_is_marked_missed(self):
        scheduler = self.scheduler(run_id="new-run")
        with scheduler.knowledge._lock, scheduler.knowledge._db() as db:
            db.execute("""INSERT INTO proactive_candidates
                (account,room,run_id,due_at,status,anchor_id,context_generation,created_at,updated_at)
                VALUES(?,?,?,?,'scheduled',0,0,?,?)""",
                (ROBOT, ROOM, "old-run", self.now.isoformat(), self.now.isoformat(), self.now.isoformat()))
        restarted = HourlyScheduler(scheduler.knowledge, self.service,
                                    Mock(complete_json=Mock()), now=lambda: self.now,
                                    run_id="restarted")
        statuses = self.candidate_statuses(restarted)
        self.assertEqual(statuses[0], "missed")
        self.assertEqual(restarted.model.client.complete_json.call_count, 0)
        self.assertEqual(self.backend.sent, [])

    def test_stale_candidate_is_not_backfilled_after_suspend(self):
        scheduler = self.scheduler()
        self.activity(scheduler)
        self.assertEqual(len(self.backend.sent), 1)
        self.assertEqual(self.candidate_statuses(scheduler), ["sent"])
        self.assertEqual(scheduler.model.client.complete_json.call_count, 1)
        scheduler.next_candidate[ROOM] = self.now - timedelta(minutes=16)
        scheduler.tick()
        self.assertEqual(len(self.backend.sent), 1)
        self.assertEqual(scheduler.model.client.complete_json.call_count, 1)
        self.assertGreater(scheduler.next_candidate[ROOM], self.now)

    def test_generation_failure_is_not_retried_within_slot(self):
        scheduler = self.scheduler(Mock(complete_json=Mock(side_effect=LLMError("模拟失败"))))
        self.activity(scheduler)
        self.assertEqual(scheduler.model.client.complete_json.call_count, 1)
        self.assertEqual(self.backend.sent, [])

    def test_connection_change_during_generation_cancels_send(self):
        replacement = daily.Backend()
        replacement.session_id = "new-session"
        def generate(*args, **kwargs):
            self.service.close()
            self.service.bind(replacement)
            return {"speak": True, "topic": "旧连接话题", "text": "这句话不能发到新连接"}
        scheduler = self.scheduler(Mock(complete_json=generate))
        self.activity(scheduler)
        self.assertEqual(self.backend.sent, [])
        self.assertEqual(replacement.sent, [])

    def test_recent_chat_retrieves_relevant_long_term_knowledge(self):
        scheduler = self.scheduler()
        entity = scheduler.knowledge.entity(ROOM, "吉他话题", pinned=True)
        with scheduler.knowledge._lock, scheduler.knowledge._db():
            scheduler.knowledge.put(ROOM, entity["id"], "练习", "群内玩笑", "每天都说从明天练吉他", tags=["吉他"])
        self.activity(scheduler)
        payload = json.loads(scheduler.model.client.complete_json.call_args.args[1])
        self.assertEqual(payload["knowledge"][0]["kind"], "群内玩笑")
        self.assertIn("吉他", payload["knowledge"][0]["content"])

    def test_schedule_uses_day_and_evening_windows_and_silent_night(self):
        scheduler = self.scheduler()
        day = datetime(2026, 10, 10, 3, 0, tzinfo=timezone.utc)      # 北京11:00。
        evening = datetime(2026, 10, 10, 11, 0, tzinfo=timezone.utc)  # 北京19:00。
        night = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)    # 北京23:00。
        scheduler.random = Mock(uniform=Mock(return_value=150))
        self.assertEqual(scheduler._schedule(ROOM, day), day + timedelta(minutes=150))
        scheduler.random.uniform.reset_mock()
        scheduler._schedule(ROOM, evening)
        self.assertEqual(scheduler.random.uniform.call_args.args, (180, 300))
        next_day = scheduler._schedule(ROOM, night)
        self.assertEqual(next_day.astimezone(daily.timezone(timedelta(hours=8))).hour, 9)

    def test_end_hour_24_schedules_inside_next_window(self):
        scheduler = self.scheduler(start_hour=9, end_hour=24,
                                   random_source=Mock(uniform=Mock(side_effect=lambda low, high: high)))
        at = datetime(2026, 10, 10, 15, 30, tzinfo=timezone.utc)  # 北京23:30。
        due = scheduler._schedule(ROOM, at).astimezone(daily.timezone(timedelta(hours=8)))
        self.assertEqual((due.hour, due.minute), (14, 0))
        self.assertEqual(due.date(), (at.astimezone(daily.timezone(timedelta(hours=8))) + timedelta(days=1)).date())

    def test_end_hour_24_window_ends_at_midnight(self):
        scheduler = self.scheduler(start_hour=9, end_hour=24)
        late = datetime(2026, 10, 10, 15, 30, tzinfo=timezone.utc)    # 北京23:30。
        midnight = datetime(2026, 10, 10, 16, 30, tzinfo=timezone.utc)  # 次日00:30。
        self.assertTrue(scheduler._in_window(late))
        self.assertFalse(scheduler._in_window(midnight))

    def test_narrow_evening_window_never_schedules_outside(self):
        scheduler = self.scheduler(start_hour=22, end_hour=23,
                                   random_source=Mock(uniform=Mock(side_effect=lambda low, high: high)))
        at = datetime(2026, 10, 10, 14, 10, tzinfo=timezone.utc)  # 北京22:10。
        due = scheduler._schedule(ROOM, at).astimezone(daily.timezone(timedelta(hours=8)))
        self.assertEqual((due.hour, due.minute), (22, 59))

    def test_daily_quota_and_cooldown_filter_before_model_call(self):
        scheduler = self.scheduler(daily_limit=1)
        start = self.now.astimezone(daily.timezone(timedelta(hours=8))).replace(
            hour=1, minute=0, second=0, microsecond=0)
        with scheduler.knowledge._lock, scheduler.knowledge._db() as db:
            db.execute("""INSERT INTO proactive_candidates
                (account,room,run_id,due_at,status,anchor_id,context_generation,
                 created_at,updated_at,sent_at)
                VALUES(?,?,?,?,'sent',0,0,'','',?)""",
                (ROBOT, ROOM, "old-sent", start.isoformat(), start.isoformat()))
        self.activity(scheduler)
        scheduler.model.client.complete_json.assert_not_called()
        self.assertEqual(self.backend.sent, [])
        candidate = scheduler.knowledge._db().execute("""SELECT status,error FROM proactive_candidates
            WHERE account=? AND room=? ORDER BY id DESC LIMIT 1""", (ROBOT, ROOM)).fetchone()
        self.assertEqual(tuple(candidate), ("filtered", "daily-limit"))

    def test_cooldown_blocks_model_but_filtered_and_failed_attempts_do_not(self):
        scheduler = self.scheduler(daily_limit=6, cooldown_minutes=45)
        with scheduler.knowledge._lock, scheduler.knowledge._db() as db:
            db.execute("""INSERT INTO proactive_candidates
                (account,room,run_id,due_at,status,anchor_id,context_generation,
                 created_at,updated_at,sent_at)
                VALUES(?,?,?,?,'unknown',0,0,?,?,?)""",
                (ROBOT, ROOM, "recent", self.now.isoformat(),
                 self.now.isoformat(), self.now.isoformat(), self.now.isoformat()))
        self.activity(scheduler)
        scheduler.model.client.complete_json.assert_not_called()
        candidate = scheduler.knowledge._db().execute("""SELECT status,error FROM proactive_candidates
            WHERE account=? AND room=? ORDER BY id DESC LIMIT 1""", (ROBOT, ROOM)).fetchone()
        self.assertEqual(tuple(candidate), ("filtered", "cooldown"))

        with scheduler.knowledge._lock, scheduler.knowledge._db() as db:
            db.execute("UPDATE proactive_candidates SET status='failed' WHERE run_id='recent'")
        self.now += timedelta(minutes=1)
        scheduler.next_candidate[ROOM] = self.now
        scheduler.tick()
        self.assertEqual(scheduler.model.client.complete_json.call_count, 1)
        self.assertEqual(self.candidate_statuses(scheduler)[-1], "sent")

    def test_daily_limit_resets_at_beijing_midnight(self):
        scheduler = self.scheduler(daily_limit=1)
        previous = datetime(2026, 10, 10, 15, 30, tzinfo=timezone.utc)  # 北京23:30。
        with scheduler.knowledge._lock, scheduler.knowledge._db() as db:
            db.execute("""INSERT INTO proactive_candidates
                (account,room,run_id,due_at,status,anchor_id,context_generation,
                 created_at,updated_at,sent_at)
                VALUES(?,?,?,?,'sent',0,0,?,?,?)""",
                (ROBOT, ROOM, "previous-day", previous.isoformat(),
                 previous.isoformat(), previous.isoformat(), previous.isoformat()))
        self.now = datetime(2026, 10, 10, 16, 30, tzinfo=timezone.utc)  # 北京次日00:30。
        self.assertEqual(scheduler._usage(ROOM, self.now), (0, True))
        scheduler.model.client.complete_json.assert_not_called()

        self.now = datetime(2026, 10, 11, 2, 0, tzinfo=timezone.utc)  # 北京时间10:00。
        self.record("新一天第一条", sender=CAT)
        self.record("新一天第二条", sender=OTHER)
        self.now += timedelta(minutes=1)
        scheduler.next_candidate[ROOM] = self.now
        scheduler.tick()
        self.assertEqual(scheduler.model.client.complete_json.call_count, 1)
        self.assertEqual(self.candidate_statuses(scheduler)[-1], "sent")

    def test_insufficient_activity_skips_model_and_records_details(self):
        scheduler = self.scheduler(min_messages=2, min_speakers=2)
        self.record("只有一个人说两句", sender=CAT)
        self.record("还是同一个人", sender=CAT)
        self.now += timedelta(minutes=1)
        scheduler.next_candidate[ROOM] = self.now
        scheduler.tick()
        scheduler.model.client.complete_json.assert_not_called()
        candidate = scheduler.knowledge._db().execute("""SELECT status,activity_count,speaker_count,error
            FROM proactive_candidates WHERE account=? AND room=? ORDER BY id DESC LIMIT 1""",
            (ROBOT, ROOM)).fetchone()
        self.assertEqual(tuple(candidate), ("filtered", 2, 1, "insufficient-activity"))

    def test_model_result_requires_boolean_speak_and_text_fields(self):
        for result in ({"topic": "旧格式", "text": "没有 speak 字段"},
                       {"speak": "yes", "topic": "错误布尔值", "text": "不应发送"},
                       {"speak": True, "topic": "缺正文"}):
            self.backend = daily.Backend()
            self.service.close()
            self.service.bind(self.backend)
            self.now = datetime(2026, 10, 10, 2, 0, tzinfo=timezone.utc)
            scheduler = self.scheduler(Mock(complete_json=Mock(return_value=result)),
                                       run_id="bad-" + str(len(result)))
            self.record("最近在练吉他", sender=CAT)
            self.record("我想学一首新曲子", sender=OTHER)
            self.now += timedelta(minutes=1)
            scheduler.next_candidate[ROOM] = self.now
            scheduler.tick()
            self.assertEqual(self.backend.sent, [])
            self.assertEqual(self.candidate_statuses(scheduler)[-1], "failed")

    def test_unconfirmed_send_counts_toward_quota_and_cooldown(self):
        class UnconfirmedBackend(daily.Backend):
            def send_text(self, room, text):
                self.sent.append((room, text))
                return False

        self.service.close()
        self.backend = UnconfirmedBackend()
        self.service.bind(self.backend)
        scheduler = self.scheduler(daily_limit=1)
        self.record("最近在练吉他", sender=CAT)
        self.record("我想学一首新曲子", sender=OTHER)
        self.now += timedelta(minutes=1)
        scheduler.next_candidate[ROOM] = self.now
        scheduler.tick()
        self.assertEqual(self.candidate_statuses(scheduler), ["unknown"])
        self.assertEqual(len(self.backend.sent), 1)
        count, cooldown = scheduler._usage(ROOM, self.now)
        self.assertEqual((count, cooldown), (1, False))
