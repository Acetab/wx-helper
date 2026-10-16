"""持久化记忆、来源、权限、定时发送的行为验证；不连接微信或真实模型。"""
import json
import sqlite3
from contextlib import closing
import unittest
from datetime import timedelta
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

    def test_import_promotes_existing_candidate_and_adds_explicit_alias(self):
        kb = self.kb()
        candidate = kb.entity(ROOM, "新人物")
        self.assertFalse(kb.visible(candidate))
        promoted = kb.entity(ROOM, "新人物", aliases=["新外号"], pinned=True)
        self.assertEqual(promoted["id"], candidate["id"])
        self.assertTrue(kb.visible(promoted))
        self.assertEqual(kb.find_person(ROOM, "新外号")["id"], candidate["id"])

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

    def test_high_frequency_person_needs_three_messages_two_speakers(self):
        kb = self.kb()
        entity = kb.entity(ROOM, "群外人物")
        with kb._lock, kb._db() as db:
            kb.put(ROOM, entity["id"], "话题", "他人提及", "常聊电影")
            for number, sender in ((1, CAT), (2, CAT)):
                db.execute("INSERT INTO knowledge_mentions VALUES(?,?,?)", (entity["id"], number, sender))
        self.assertEqual(kb.search(ROOM, "群外人物"), [])
        with kb._lock, kb._db() as db:
            db.execute("INSERT INTO knowledge_mentions VALUES(?,?,?)", (entity["id"], 3, OTHER))
        self.assertEqual(len(kb.search(ROOM, "群外人物")), 1)

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
                       {"memory": {"batch_size": True}}, {"memory": {"flush_seconds": 0}}):
            with self.assertRaises(ValueError):
                feature_options(config)

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

    def scheduler(self, client=None):
        self.now = self.now.replace(hour=1, minute=59, second=59)  # 北京09:59:59。
        client = client or Mock(complete_json=Mock(return_value={"topic": "吉他练习", "text": "刚才聊到吉他，你们最近在练什么曲子？"}))
        return HourlyScheduler(self.kb(), self.service, client, now=lambda: self.now)

    def due(self, scheduler):
        self.record("最近在练吉他")
        self.now += timedelta(seconds=1)
        scheduler.tick()

    def test_active_hour_sends_once_with_no_raw_member_ids(self):
        scheduler = self.scheduler()
        self.due(scheduler)
        scheduler.tick()
        scheduler.run_slot(ROOM, self.now.astimezone(daily.timezone(timedelta(hours=8))))
        self.assertEqual(len(self.backend.sent), 1)
        self.assertNotIn(CAT, scheduler.client.complete_json.call_args.args[1])
        self.assertNotIn("@", self.backend.sent[0][1])

    def test_empty_room_and_only_commands_do_not_generate(self):
        scheduler = self.scheduler()
        self.record("/help")
        self.store.record(self.message("机器人输出", data={"bot_generated": True}))
        self.now += timedelta(seconds=1)
        scheduler.tick()
        self.assertEqual(self.backend.sent, [])
        scheduler.client.complete_json.assert_not_called()

    def test_midnight_is_silent(self):
        self.now = self.now.replace(hour=15, minute=59, second=59)
        scheduler = HourlyScheduler(self.kb(), self.service, Mock(), now=lambda: self.now)
        self.due(scheduler)
        self.assertEqual(self.backend.sent, [])

    def test_offline_does_not_catch_up_after_reconnect(self):
        scheduler = self.scheduler()
        self.service.close()
        self.due(scheduler)
        self.backend = daily.Backend()
        self.service.bind(self.backend)
        scheduler.tick()
        self.assertEqual(self.backend.sent, [])

    def test_repeated_topic_is_skipped_next_hour(self):
        scheduler = self.scheduler()
        self.due(scheduler)
        self.now += timedelta(minutes=59, seconds=59)
        self.record("吉他又练了一会")
        self.now += timedelta(seconds=1)
        scheduler.tick()
        self.assertEqual(len(self.backend.sent), 1)

    def test_reset_during_generation_prevents_send(self):
        def generate(*args, **kwargs):
            self.store.reset_context(ROOM)
            return {"topic": "过时话题", "text": "这句话不应发出"}
        scheduler = self.scheduler(Mock(complete_json=generate))
        self.due(scheduler)
        self.assertEqual(self.backend.sent, [])

    def test_reset_during_send_pacing_prevents_native_send(self):
        scheduler = self.scheduler()
        cancel = Mock(is_set=lambda: False)
        cancel.wait = lambda _: self.store.reset_context(ROOM) or False
        self.service._cancel = cancel
        self.due(scheduler)
        self.assertEqual(self.backend.sent, [])

    def test_existing_claim_after_crash_is_not_resent(self):
        scheduler = self.scheduler()
        self.record("新话题")
        self.now += timedelta(seconds=1)
        with scheduler.knowledge._lock, scheduler.knowledge._db() as db:
            db.execute("INSERT INTO hourly_runs VALUES(?,?,?,'claimed','',0,?)",
                       (ROBOT, ROOM, self.now.astimezone(daily.timezone(timedelta(hours=8))).isoformat(),
                        scheduler.knowledge.now()))
        scheduler.tick()
        self.assertEqual(self.backend.sent, [])

    def test_missed_slot_is_not_backfilled(self):
        scheduler = self.scheduler()
        self.record("新聊天")
        self.now += timedelta(minutes=5)
        scheduler.tick()
        self.assertEqual(self.backend.sent, [])

    def test_generation_failure_is_not_retried_within_slot(self):
        scheduler = self.scheduler(Mock(complete_json=Mock(side_effect=LLMError("模拟失败"))))
        self.due(scheduler)
        scheduler.tick()
        self.assertEqual(scheduler.client.complete_json.call_count, 1)
        self.assertEqual(self.backend.sent, [])

    def test_connection_change_during_generation_cancels_send(self):
        replacement = daily.Backend()
        replacement.session_id = "new-session"
        def generate(*args, **kwargs):
            self.service.close()
            self.service.bind(replacement)
            return {"topic": "旧连接话题", "text": "这句话不能发到新连接"}
        scheduler = self.scheduler(Mock(complete_json=generate))
        self.due(scheduler)
        self.assertEqual(self.backend.sent, [])
        self.assertEqual(replacement.sent, [])

    def test_recent_chat_retrieves_relevant_long_term_knowledge(self):
        scheduler = self.scheduler()
        entity = scheduler.knowledge.entity(ROOM, "吉他话题", pinned=True)
        with scheduler.knowledge._lock, scheduler.knowledge._db():
            scheduler.knowledge.put(ROOM, entity["id"], "练习", "群内玩笑", "每天都说从明天练吉他", tags=["吉他"])
        self.due(scheduler)
        payload = json.loads(scheduler.client.complete_json.call_args.args[1])
        self.assertEqual(payload["knowledge"][0]["kind"], "群内玩笑")
        self.assertIn("吉他", payload["knowledge"][0]["content"])
