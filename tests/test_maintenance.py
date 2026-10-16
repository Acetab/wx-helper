"""维护权限、私聊入口、资料迁移及清空后重新学习；全部使用假微信和假模型。"""
import json
import unittest
from datetime import timedelta
from unittest.mock import Mock

import test_chat_summary as daily
from wxbot_framework.authorization import LOCAL_EDITOR
from wxbot_framework.config import ConfigStore
from wxbot_framework.knowledge import KnowledgeStore
from wxbot_framework.locking import InstanceLock
from wxbot_framework.maintenance import MaintenanceRouter, MaintenanceService
from wxbot_framework.members import MemberError, MemberStore
from wxbot_framework.memory_worker import MemoryWorker
from wxbot_framework.runtime import BotApplication
from wxbot_framework.sdk import Message
from wxbot_framework.upgrades import StartupNotice, apply_upgrade

ROOM, OTHER_ROOM, OWNER, OTHER, ROBOT = daily.ROOM, daily.OTHER_ROOM, daily.CAT, daily.OTHER, daily.ROBOT


class MaintenanceTests(unittest.TestCase):
    setUp = daily.DailySummaryTests.setUp
    tearDown = daily.DailySummaryTests.tearDown
    new_store = daily.DailySummaryTests.new_store
    message = daily.DailySummaryTests.message
    model = daily.DailySummaryTests.model

    def kb(self):
        k = KnowledgeStore(self.store)
        k.initialize()
        return k

    def person_memory(self, k):
        entity = k.entity(ROOM, "示例甲", pinned=True)
        item = k.add_memory(ROOM, entity["id"], "爱好", "人工确认", "喜欢吉他", OWNER)
        return entity, item

    def record(self, text, sender=OWNER):
        message = self.message(text, sender=sender)
        self.store.record(message)
        return self.store.message_id(message)

    def test_owner_only_includes_other_members_own_records_and_robot(self):
        k = self.kb()
        self.record("别人的发言", OTHER)
        entity = k.member_entity(ROOM, OTHER)
        with k._db():
            item = k.put(ROOM, entity["id"], "爱好", "本人陈述", "喜欢鼓")
        service = MaintenanceService(k)
        for author in (OTHER, ROBOT, "local-cli", "维护者昵称"):
            with self.assertRaises(MemberError):
                k.edit(ROOM, item, author, content="越权修改")
            with self.assertRaises(MemberError):
                service.execute("人物 清空 别人", author)
        k.edit(ROOM, item, OWNER, content="喜欢鼓和贝斯")
        self.assertIn("贝斯", service.execute(f"记忆 列表 #{entity['id']}", LOCAL_EDITOR, current_room=ROOM))

    def test_legacy_admin_list_and_robot_do_not_grant_permission(self):
        config = json.loads(self.path.read_text(encoding="utf-8"))
        config.pop("owner_wxid")
        config["admin_wxids"] = [OTHER]
        self.path.write_text(json.dumps(config), encoding="utf-8")
        store = MemberStore(self.path, ROBOT)
        self.stores.append(store)
        self.assertFalse(store.is_admin(OTHER))
        self.assertFalse(store.is_admin(ROBOT))
        self.assertTrue(store.is_admin(LOCAL_EDITOR))

    def test_self_binding_never_links_same_name_person_or_grants_access(self):
        k = self.kb()
        person = k.entity(ROOM, "普通昵称", pinned=True)
        self.record("我要登记昵称", OTHER)
        self.store.register("普通昵称", ROOM, OTHER)
        self.assertIsNone(k.find_person(ROOM, "普通昵称")["wxid"])
        self.assertNotEqual(k.member_entity(ROOM, OTHER)["id"], person["id"])
        self.assertFalse(self.store.is_admin(OTHER))

    def test_private_owner_route_bypasses_group_allowlist_and_does_not_archive(self):
        config = {"wechat_account": ROBOT, "members_config_file": "members.json", "chat_allowlist": [ROOM],
                  "console": False, "plugins": {}, "maintenance": {"default_room": ROOM},
                  "text_send_delay": {"enabled": False}}
        path = self.root / "bot.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        app = BotApplication(daily.ROOT, ConfigStore(path))
        try:
            app.members.initialize()
            app.knowledge.initialize()
            app.service.bind(self.backend)
            app._emit(Message("/管理 状态", OWNER, sender_id=OWNER, session_id="test"))
            queued = app.maintenance_router.queue.get_nowait()
            app.maintenance_router.process(queued)
            self.assertEqual(self.backend.sent[-1][0], OWNER)
            self.assertIn(ROOM, self.backend.sent[-1][1])
            self.assertEqual(app.members._connection().execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
            app._emit(Message("/管理 状态", OTHER, sender_id=OTHER, session_id="test"))
            self.assertTrue(app.maintenance_router.queue.empty())
        finally:
            app.stop()

    def test_private_forged_chat_and_nonowner_commands_are_denied(self):
        router = MaintenanceRouter(MaintenanceService(self.kb(), options={"default_room": ROOM}), self.service)
        router.process(Message("/管理 状态", OTHER, sender_id=OWNER, session_id="test"))
        self.assertIn("不匹配", self.backend.sent[-1][1])
        router.process(Message("/管理 状态", OTHER, sender_id=OTHER, session_id="test"))
        self.assertIn("维护者", self.backend.sent[-1][1])
        self.assertNotIn(ROOM, self.backend.sent[-1][1])

    def test_group_cannot_silently_manage_another_group(self):
        service = MaintenanceService(self.kb())
        with self.assertRaises(MemberError):
            service.execute(f"--群 {OTHER_ROOM} 人物 列表", OWNER, current_room=ROOM)

    def test_identity_alias_changes_persist_across_old_seed_import(self):
        k = self.kb()
        entity = k.entity(ROOM, "示例甲", aliases=["旧别名"], pinned=True)
        k.change_identity(ROOM, entity["id"], OWNER, alias="旧别名", remove=True)
        k.change_identity(ROOM, entity["id"], OWNER, name="新名字")
        seed = self.root / "seed.json"
        seed.write_text(json.dumps({"groups": {ROOM: [{"name": "示例甲", "aliases": ["旧别名"], "memories": []}]}}), encoding="utf-8")
        k.import_seed(seed)
        self.assertEqual(k.find_person(ROOM, "新名字")["id"], entity["id"])
        with self.assertRaises(MemberError):
            k.find_person(ROOM, "旧别名")
        with self.assertRaises(MemberError):
            k.entity(ROOM, "另一个人", aliases=["新名字"])

    def test_clear_archives_preserves_sources_and_relearns_same_topic_from_new_messages(self):
        k = self.kb()
        entity, old_item = self.person_memory(k)
        old_message = self.record("示例甲以前喜欢吉他")
        snapshot = self.store.recent_snapshot(ROOM)
        _, _, code = k.prepare_clear(ROOM, entity["id"], OWNER)
        k.confirm_clear(ROOM, code, OWNER)
        self.assertFalse(self.store.snapshot_is_current(snapshot))
        self.assertEqual(k.search(ROOM, "示例甲"), [])
        with k._db():
            rejected = k.put(ROOM, entity["id"], "爱好", "他人提及", "旧话重提",
                             sources=[dict(k._db().execute("SELECT * FROM messages WHERE id=?", (old_message,)).fetchone())])
            stale = k.put(ROOM, entity["id"], "爱好", "他人提及", "旧任务", expected_epoch=0)
            seeded = k.put(ROOM, entity["id"], "爱好", "群内玩笑", "旧种子", origin="seed:old")
        self.assertIsNone(rejected)
        self.assertIsNone(stale)
        self.assertIsNone(seeded)
        new_message = self.record("示例甲现在喜欢钢琴")
        with k._db():
            current = k.put(ROOM, entity["id"], "爱好", "他人提及", "喜欢钢琴",
                            sources=[dict(k._db().execute("SELECT * FROM messages WHERE id=?", (new_message,)).fetchone())])
        self.assertNotEqual(old_item, current)
        self.assertEqual(k.search(ROOM, "示例甲")[0]["content"], "喜欢钢琴")
        self.assertEqual(k.admin_item(ROOM, old_item)["status"], "archived")
        self.assertEqual(k._db().execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
        self.assertTrue(k._db().execute("SELECT 1 FROM knowledge_sources WHERE item_id=?", (old_item,)).fetchone())

    def test_clear_confirmation_checks_owner_room_expiry_and_changed_records(self):
        k = self.kb()
        entity, item = self.person_memory(k)
        _, _, code = k.prepare_clear(ROOM, entity["id"], OWNER)
        for author, room in ((OTHER, ROOM), (OWNER, OTHER_ROOM)):
            with self.assertRaises(MemberError):
                k.confirm_clear(room, code, author)
        k.edit(ROOM, item, OWNER, content="修改过的内容")
        with self.assertRaises(MemberError):
            k.confirm_clear(ROOM, code, OWNER)
        _, _, code = k.prepare_clear(ROOM, entity["id"], OWNER)
        self.now += timedelta(minutes=6)
        with self.assertRaises(MemberError):
            k.confirm_clear(ROOM, code, OWNER)

    def test_inflight_learning_is_discarded_after_clear(self):
        k = self.kb()
        entity, _ = self.person_memory(k)
        message_id = self.record("示例甲喜欢吉他")
        def model(*args, **kwargs):
            _, _, code = k.prepare_clear(ROOM, entity["id"], OWNER)
            k.confirm_clear(ROOM, code, OWNER)
            return {"entities": [], "memories": [{"subject_type": "person", "subject": "示例甲", "key": "爱好",
                    "kind": "他人提及", "content": "喜欢吉他", "message_ids": [message_id]}]}
        worker = MemoryWorker(k, Mock(complete_json=model))
        worker.process(ROOM, k.batch(ROOM))
        self.assertEqual(k.cursor(ROOM), 0)
        self.assertEqual(k.search(ROOM, "示例甲"), [])

    def test_move_restore_classify_and_audit(self):
        k = self.kb()
        entity, item = self.person_memory(k)
        other = k.entity(ROOM, "示例乙", pinned=True)
        service = MaintenanceService(k)
        service.execute(f"记忆 移动 {item} 示例乙", OWNER, current_room=ROOM)
        self.assertEqual(k.admin_item(ROOM, item)["entity_id"], other["id"])
        service.execute(f"记忆 分类 {item} 群内玩笑", OWNER, current_room=ROOM)
        service.execute(f"记忆 停用 {item}", OWNER, current_room=ROOM)
        service.execute(f"记忆 恢复 {item}", OWNER, current_room=ROOM)
        self.assertEqual(k.admin_item(ROOM, item)["status"], "active")
        self.assertEqual(k.admin_item(ROOM, item)["kind"], "群内玩笑")
        _, _, code = k.prepare_clear(ROOM, other["id"], OWNER)
        k.confirm_clear(ROOM, code, OWNER)
        new_item = k.restore_memory(ROOM, item, OWNER)
        self.assertNotEqual(new_item, item)
        self.assertTrue(k._db().execute("SELECT 1 FROM knowledge_entity_edits WHERE action='restore-archive'").fetchone())

    def test_startup_notice_sent_once_across_recreated_service(self):
        k = self.kb()
        options = {"id": "test-version", "room": ROOM, "text": "机器人更新完成，大家好。"}
        notice = StartupNotice(k, self.service, options)
        self.assertTrue(notice.send())
        self.assertFalse(StartupNotice(k, self.service, options).send())
        self.assertEqual(notice.status(), "sent")
        self.assertEqual(len(self.backend.sent), 1)

    def test_notice_unknown_result_does_not_automatically_repeat(self):
        k = self.kb()
        options = {"id": "uncertain", "room": ROOM, "text": "更新完成。"}
        facade = Mock(session_id="test", send_text=Mock(side_effect=RuntimeError("断线")))
        notice = StartupNotice(k, facade, options)
        self.assertFalse(notice.send())
        self.assertEqual(notice.status(), "unknown")
        self.assertFalse(notice.send())
        self.assertEqual(facade.send_text.call_count, 1)

    def test_profile_upgrade_splits_people_and_preserves_item_ids_and_sources(self):
        k = self.kb()
        old = k.entity(ROOM, "示例甲", aliases=["示例乙", "示例丙", "待确认外号"], pinned=True)
        item = k.add_memory(ROOM, old["id"], "配音", "群内玩笑", "示例甲喜欢配音", OWNER)
        path = self.root / "upgrade.json"
        payload = {"id": "test-split", "account": ROBOT, "room": ROOM,
                   "remove_aliases": {"示例甲": ["示例乙", "示例丙", "待确认外号"]},
                   "profiles": [{"name": "示例甲", "memories": [{"key": "背景", "content": "喜欢运动"}]},
                                {"name": "示例乙"}, {"name": "示例丙"}],
                   "moves": [{"from": "示例甲", "to": "示例乙", "slots": ["配音"]}]}
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertTrue(apply_upgrade(k, path))
        self.assertFalse(apply_upgrade(k, path))
        self.assertEqual(k.admin_item(ROOM, item)["entity_id"], k.find_person(ROOM, "示例乙")["id"])
        self.assertIn("示例乙", k.admin_item(ROOM, item)["content"])
        self.assertTrue(k._db().execute("SELECT 1 FROM knowledge_sources WHERE item_id=?", (item,)).fetchone())
        self.assertIsNone(k.find_person(ROOM, "示例丙")["wxid"])
        self.assertTrue(list((self.root / "backups").glob("*-before-profile-fix-*.sqlite3")))

    def test_offline_lock_excludes_running_process(self):
        path = self.root / "bot.lock"
        with InstanceLock(path):
            with self.assertRaises(RuntimeError):
                InstanceLock(path).acquire()

    def test_v1_schema_migration_keeps_item_number_and_source(self):
        # 真实旧结构没有 epoch；该编号已被使用者记住，升级后必须仍能查询。
        db = self.store._connection()
        db.executescript("""
            CREATE TABLE knowledge_entities(id INTEGER PRIMARY KEY,account TEXT NOT NULL,room TEXT NOT NULL,
                entity_key TEXT NOT NULL,name TEXT NOT NULL,aliases TEXT NOT NULL DEFAULT '[]',wxid TEXT,
                pinned INTEGER NOT NULL DEFAULT 0,UNIQUE(account,room,entity_key));
            CREATE TABLE knowledge_items(id INTEGER PRIMARY KEY,account TEXT NOT NULL,room TEXT NOT NULL,
                entity_id INTEGER NOT NULL,slot TEXT NOT NULL,kind TEXT NOT NULL,content TEXT NOT NULL,
                tags TEXT NOT NULL DEFAULT '[]',status TEXT NOT NULL DEFAULT 'active',manual INTEGER NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL,UNIQUE(account,room,entity_id,slot));
            CREATE TABLE knowledge_sources(item_id INTEGER NOT NULL,origin TEXT NOT NULL,message_id INTEGER,
                speaker_id TEXT,source_time TEXT NOT NULL,PRIMARY KEY(item_id,origin));
        """)
        with db:
            db.execute("INSERT INTO knowledge_entities VALUES(7,?,?,'person:示例','示例','[]',NULL,1)", (ROBOT, ROOM))
            db.execute("INSERT INTO knowledge_items VALUES(42,?,?,7,'爱好','他人提及','喜欢吉他','[]','active',0,2,'2026-01-01')", (ROBOT, ROOM))
            db.execute("INSERT INTO knowledge_sources VALUES(42,'seed:test',NULL,NULL,'2026-01-01')")
        k = self.kb()
        self.assertEqual(k.admin_item(ROOM, 42)["epoch"], 0)
        self.assertEqual(k.admin_item(ROOM, 42)["revision"], 2)
        self.assertEqual(k.search(ROOM, "示例")[0]["sources"][0]["origin"], "seed:test")
        self.assertTrue(list((self.root / "backups").glob("*-before-maintenance-*.sqlite3")))
        k.initialize()
        self.assertEqual(len(list((self.root / "backups").glob("*-before-maintenance-*.sqlite3"))), 1)

    def test_runtime_feature_settings_persist_and_reject_invalid_hours(self):
        config = {"wechat_account": ROBOT, "members_config_file": "members.json", "console": False,
                  "memory": {"enabled": False}, "hourly_chat": {"enabled": False}}
        path = self.root / "bot.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        app = BotApplication(daily.ROOT, ConfigStore(path))
        try:
            app.members.initialize()
            app.knowledge.initialize()
            app.maintenance.execute("定时 设置 10 22 300", OWNER, current_room=ROOM)
            self.assertEqual(ConfigStore(path).load()["hourly_chat"]["context_limit"], 300)
            with self.assertRaises(ValueError):
                app.maintenance.execute("定时 设置 24 22 300", OWNER, current_room=ROOM)
            self.assertEqual(ConfigStore(path).load()["hourly_chat"]["start_hour"], 10)
        finally:
            app.stop()

    def test_upgrade_on_empty_database_and_private_content_sanitization(self):
        k = self.kb()
        path = self.root / "fresh-upgrade.json"
        payload = {"id": "fresh", "account": ROBOT, "room": ROOM,
                   "remove_aliases": {"示例甲": ["错误别名"]},
                   "profiles": [{"name": "示例甲", "memories": [
                       {"key": "联系方式", "content": "邮箱 example@example.invalid，喜欢吉他", "tags": ["吉他"]}]}]}
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertTrue(apply_upgrade(k, path))
        result = k.search(ROOM, "吉他")[0]
        self.assertNotIn("example@", result["content"])
        self.assertEqual(json.loads(result["tags"]), ["吉他"])

    def test_failed_upgrade_rolls_back_identity_changes(self):
        k = self.kb()
        entity = k.entity(ROOM, "示例甲", aliases=["旧别名"], pinned=True)
        k.entity(ROOM, "示例乙", aliases=["占用的别名"], pinned=True)
        path = self.root / "bad-upgrade.json"
        payload = {"id": "bad", "account": ROBOT, "room": ROOM,
                   "remove_aliases": {"示例甲": ["旧别名"]},
                   "profiles": [{"name": "示例丙", "aliases": ["占用的别名"]}]}
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(MemberError):
            apply_upgrade(k, path)
        self.assertEqual(k.find_person(ROOM, "旧别名")["id"], entity["id"])
        self.assertIsNone(k._db().execute("SELECT 1 FROM applied_upgrades WHERE upgrade_id='bad'").fetchone())

    def test_disconnected_queue_cannot_execute_old_management(self):
        k = self.kb()
        router = MaintenanceRouter(MaintenanceService(k, options={"default_room": ROOM}), self.service)
        router.process(Message("/管理 人物 新建 旧连接人物", OWNER, sender_id=OWNER, session_id="old"))
        with self.assertRaises(MemberError):
            k.find_person(ROOM, "旧连接人物")

    def test_flat_admin_commands_bind_alias_rename_remember_and_unbind(self):
        k = self.kb()
        service = MaintenanceService(k, options={"default_room": ROOM})
        router = MaintenanceRouter(service, self.service)
        self.store.record(self.message("大家好", sender=OTHER, data={"sender_wechat_name": "示例微信昵称", "self_wxid": ROBOT}))
        # 1. /成员 查看 M 编号
        router.process(Message("/成员", OWNER, sender_id=OWNER, session_id="test"))
        self.assertIn("示例微信昵称", self.backend.sent[-1][1])
        # 2. /绑定 通过微信昵称一步绑定并自动创建人物，同时同步 people 与 aliases
        router.process(Message("/绑定 示例微信昵称 示例人物乙", OWNER, sender_id=OWNER, session_id="test"))
        self.assertIn("已绑定", self.backend.sent[-1][1])
        self.assertEqual(self.store.display_name(OTHER, ROOM), "示例人物乙")
        self.assertEqual(self.store.at_display_name(OTHER, ROOM), "示例微信昵称")
        # 3. /别名 添加外号并同步到 MemberStore.resolve
        router.process(Message("/别名 示例人物乙 +乙哥", OWNER, sender_id=OWNER, session_id="test"))
        self.assertEqual(self.store.resolve("乙哥", ROOM).wxid, OTHER)
        # 4. /记住 新增人工记忆
        router.process(Message('/记住 示例人物乙 长语音 群内玩笑 "喜欢发超长语音"', OWNER, sender_id=OWNER, session_id="test"))
        self.assertEqual(k.search(ROOM, "长语音")[0]["name"], "示例人物乙")
        # 5. /人物 查看详情
        router.process(Message("/人物 示例人物乙", OWNER, sender_id=OWNER, session_id="test"))
        self.assertIn("示例微信昵称", self.backend.sent[-1][1])
        # 6. /解绑 解除微信绑定，保留人物资料
        router.process(Message("/解绑 示例人物乙", OWNER, sender_id=OWNER, session_id="test"))
        self.assertIsNone(k.find_person(ROOM, "示例人物乙")["wxid"])
        self.assertIsNone(self.store.find_by_wxid(OTHER, ROOM))


if __name__ == "__main__":
    unittest.main()
