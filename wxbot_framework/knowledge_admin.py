"""知识维护服务：所有人工写入再次验证维护者，保留来源与可审阅的历史。"""
from __future__ import annotations

import hashlib
import json
import secrets

from .authorization import editor_label
from .members import MemberError, name_key, parse_aliases


class KnowledgeAdminMixin:
    def require_editor(self, editor):
        if not self.members.is_admin(editor):
            raise MemberError("只有配置的维护者（管理员）或服务器命令行可以维护资料")

    def invalidate(self, room):
        self._db().execute("UPDATE chat_buffers SET generation=generation+1 WHERE account=? AND room=?",
                           (self.account, room))

    def assert_person_names(self, room, name, aliases=(), entity_id=None):
        names = {name_key(name), *(name_key(value) for value in aliases)}
        for entity in self._entities(room):
            if entity["id"] == entity_id or not entity["entity_key"].startswith("person:"):
                continue
            existing = {name_key(entity["name"]), *parse_aliases(entity["aliases"])}
            if names & existing:
                raise MemberError("人物主名或别名已属于另一人物，请先核对归属")

    def admin_entity(self, room, value):
        self._room(room)
        token = str(value).lstrip("#")
        if token.isdigit():
            row = self._db().execute("SELECT * FROM knowledge_entities WHERE account=? AND room=? AND id=?",
                                     (self.account, room, int(token))).fetchone()
            if not row:
                raise MemberError("本群没有这个人物编号")
            return dict(row)
        try:
            return self.find_person(room, value)
        except MemberError:
            return self.find_entity(room, value)

    def entity_audit(self, room, entity_id, editor, action, previous, replacement):
        self._db().execute("""INSERT INTO knowledge_entity_edits
            (account,room,entity_id,editor,action,previous,replacement,edited_at) VALUES(?,?,?,?,?,?,?,?)""",
            (self.account, room, entity_id, editor_label(editor), action,
             json.dumps(previous, ensure_ascii=False), json.dumps(replacement, ensure_ascii=False), self.now()))
        self.invalidate(room)

    def sync_person_member(self, room, entity_id, *, previous_key=None, previous_wxid=None):
        db = self._db()
        entity = db.execute("SELECT * FROM knowledge_entities WHERE id=? AND account=? AND room=?",
                            (entity_id, self.account, room)).fetchone()
        all_aliases = set()
        if entity and entity["entity_key"].startswith("person:"):
            all_aliases = {name_key(entity["name"]), *parse_aliases(entity["aliases"])}
        keys_to_clear = set()
        if previous_key:
            keys_to_clear.add(previous_key)
        if entity:
            keys_to_clear.add(entity["entity_key"])
        for wxid in (previous_wxid, entity["wxid"] if entity else None):
            if wxid:
                for row in db.execute("SELECT person_key FROM people WHERE account=? AND room=? AND wxid=?",
                                      (self.account, room, wxid)).fetchall():
                    keys_to_clear.add(row["person_key"])
        if all_aliases:
            placeholders = ",".join("?" for _ in all_aliases)
            for row in db.execute(f"""SELECT DISTINCT p.person_key FROM people p
                LEFT JOIN aliases a USING(account,room,person_key)
                WHERE p.account=? AND p.room=? AND (p.wxid IS NULL OR p.wxid=?)
                AND a.alias IN ({placeholders})""",
                (self.account, room, entity["wxid"], *all_aliases)).fetchall():
                keys_to_clear.add(row["person_key"])
            for key in tuple(keys_to_clear):
                for row in db.execute("SELECT alias FROM aliases WHERE account=? AND room=? AND person_key=?",
                                      (self.account, room, key)).fetchall():
                    all_aliases.add(row["alias"])
        for key in keys_to_clear:
            db.execute("DELETE FROM aliases WHERE account=? AND room=? AND person_key=?", (self.account, room, key))
            db.execute("DELETE FROM people WHERE account=? AND room=? AND person_key=?", (self.account, room, key))
        if entity and entity["wxid"] and entity["entity_key"].startswith("person:"):
            person_key = entity["entity_key"]
            db.execute("""INSERT INTO people(account,room,person_key,name,display_name,wxid,self_named)
                VALUES(?,?,?,?,?,?,0)""",
                (self.account, room, person_key, entity["name"], entity["name"], entity["wxid"]))
            for alias in sorted(all_aliases):
                db.execute("INSERT OR REPLACE INTO aliases(account,room,alias,person_key) VALUES(?,?,?,?)",
                           (self.account, room, alias, person_key))

    def change_identity(self, room, value, editor, *, name=None, alias=None, remove=False, unlink=False):
        self.require_editor(editor)
        with self._lock, self._db() as db:
            entity = self.admin_entity(room, value)
            before = dict(entity)
            if unlink and not entity["entity_key"].startswith("person:"):
                raise MemberError("这个档案本身是微信成员记录，没有可解除的人物关联")
            aliases = set(parse_aliases(entity["aliases"]))
            if name is not None:
                name_key(name)
                if not entity["entity_key"].startswith("person:"):
                    raise MemberError("微信成员称呼请使用管理员成员绑定命令修改")
                aliases.add(name_key(entity["name"]))
                entity.update(name=name.strip(), entity_key="person:" + name_key(name))
            if alias is not None:
                alias = name_key(alias)
                if remove:
                    aliases.discard(alias)
                    db.execute("INSERT OR IGNORE INTO knowledge_alias_blocks VALUES(?,?,?,?)",
                               (self.account, room, entity["id"], alias))
                else:
                    aliases.add(alias)
                    db.execute("DELETE FROM knowledge_alias_blocks WHERE entity_id=? AND alias=?", (entity["id"], alias))
            aliases.discard(name_key(entity["name"]))
            self.assert_person_names(room, entity["name"], aliases, entity["id"])
            if unlink:
                entity["wxid"] = None
            entity["aliases"] = json.dumps(sorted(aliases), ensure_ascii=False)
            db.execute("UPDATE knowledge_entities SET name=?,entity_key=?,aliases=?,wxid=?,aliases_manual=1 WHERE id=?",
                       (entity["name"], entity["entity_key"], entity["aliases"], entity["wxid"], entity["id"]))
            self.sync_person_member(room, entity["id"], previous_key=before["entity_key"], previous_wxid=before["wxid"])
            self.entity_audit(room, entity["id"], editor, "identity", before, entity)
            return entity

    def admin_items(self, room, value, *, page=1, history=False):
        if not 1 <= page <= 100000:
            raise MemberError("页码必须为正整数")
        with self._lock:
            entity = self.admin_entity(room, value)
            sql = "SELECT * FROM knowledge_items WHERE account=? AND room=? AND entity_id=?"
            args = [self.account, room, entity["id"]]
            if not history:
                sql += " AND epoch=?"
                args.append(entity["epoch"])
            count = self._db().execute("SELECT COUNT(*) FROM (" + sql + ")", args).fetchone()[0]
            rows = [dict(row) for row in self._db().execute(sql + " ORDER BY id DESC LIMIT 10 OFFSET ?",
                                                          [*args, (page-1)*10])]
            return entity, count, rows

    def admin_item(self, room, item_id):
        self._room(room)
        row = self._db().execute("SELECT * FROM knowledge_items WHERE id=? AND account=? AND room=?",
                                 (item_id, self.account, room)).fetchone()
        if row is None:
            raise MemberError("本群没有这条记忆")
        return dict(row)

    def add_memory(self, room, value, slot, kind, content, editor):
        from .knowledge import sanitized, KINDS
        self.require_editor(editor)
        slot, content = sanitized(slot, 80), sanitized(content)
        if kind not in KINDS:
            raise MemberError("未知知识类别")
        with self._lock, self._db() as db:
            entity = self.admin_entity(room, value)
            if db.execute("SELECT 1 FROM knowledge_items WHERE entity_id=? AND epoch=? AND slot=?",
                          (entity["id"], entity["epoch"], slot)).fetchone():
                raise MemberError("当前已有这个主题，请用记忆修改命令")
            cursor = db.execute("""INSERT INTO knowledge_items
                (account,room,entity_id,slot,kind,content,manual,updated_at,epoch) VALUES(?,?,?,?,?,?,1,?,?)""",
                (self.account, room, entity["id"], slot, kind, content, self.now(), entity["epoch"]))
            item_id = cursor.lastrowid
            db.execute("INSERT INTO knowledge_sources VALUES(?,?,NULL,NULL,?)", (item_id, "manual:" + str(item_id), self.now()))
            db.execute("INSERT INTO knowledge_edits(item_id,editor,action,previous,replacement,edited_at) VALUES(?,?,?,?,?,?)",
                       (item_id, editor_label(editor), "add", "", content, self.now()))
            self.invalidate(room)
            return item_id

    def move_memory(self, room, item_id, value, editor):
        self.require_editor(editor)
        with self._lock, self._db() as db:
            item = self.admin_item(room, item_id)
            source = self.admin_entity(room, item["entity_id"])
            target = self.admin_entity(room, value)
            if item["epoch"] != source["epoch"]:
                raise MemberError("清空前的条目请先恢复，再移动")
            if db.execute("SELECT 1 FROM knowledge_items WHERE entity_id=? AND epoch=? AND slot=? AND id<>?",
                          (target["id"], target["epoch"], item["slot"], item_id)).fetchone():
                raise MemberError("目标人物已有同主题条目，请先分别核对")
            db.execute("UPDATE knowledge_items SET entity_id=?,epoch=?,manual=1,revision=revision+1,updated_at=? WHERE id=?",
                       (target["id"], target["epoch"], self.now(), item_id))
            db.execute("INSERT INTO knowledge_edits(item_id,editor,action,previous,replacement,edited_at) VALUES(?,?,?,?,?,?)",
                       (item_id, editor_label(editor), "move", str(source["id"]), str(target["id"]), self.now()))
            self.entity_audit(room, target["id"], editor, "move-memory", item,
                              {"item_id": item_id, "from": source["id"], "to": target["id"]})

    def restore_memory(self, room, item_id, editor):
        self.require_editor(editor)
        with self._lock:
            item = self.admin_item(room, item_id)
            entity = self.admin_entity(room, item["entity_id"])
            if item["epoch"] != entity["epoch"]:
                restored = self.add_memory(room, entity["id"], item["slot"], item["kind"], item["content"], editor)
                with self._db() as db:
                    db.execute("INSERT OR IGNORE INTO knowledge_sources SELECT ?,origin,message_id,speaker_id,source_time FROM knowledge_sources WHERE item_id=?",
                               (restored, item_id))
                    self.entity_audit(room, entity["id"], editor, "restore-archive", item, {"new_item_id": restored})
                return restored
            self.edit(room, item_id, editor, content=item["content"])
            return item_id

    def classify_memory(self, room, item_id, kind, editor):
        from .knowledge import KINDS
        self.require_editor(editor)
        if kind not in KINDS:
            raise MemberError("未知知识类别")
        with self._lock, self._db() as db:
            item = self.admin_item(room, item_id)
            entity = self.admin_entity(room, item["entity_id"])
            if item["epoch"] != entity["epoch"]:
                raise MemberError("历史条目请先恢复")
            db.execute("UPDATE knowledge_items SET kind=?,manual=1,revision=revision+1,updated_at=? WHERE id=?",
                       (kind, self.now(), item_id))
            db.execute("INSERT INTO knowledge_edits(item_id,editor,action,previous,replacement,edited_at) VALUES(?,?,?,?,?,?)",
                       (item_id, editor_label(editor), "classify", item["kind"], kind, self.now()))
            self.entity_audit(room, entity["id"], editor, "classify-memory", item, {"item_id": item_id, "kind": kind})

    def fingerprint(self, room, entity_id):
        entity = self.admin_entity(room, entity_id)
        rows = [dict(r) for r in self._db().execute("SELECT * FROM knowledge_items WHERE entity_id=? ORDER BY id", (entity_id,))]
        return hashlib.sha256(json.dumps([entity, rows], sort_keys=True).encode()).hexdigest()

    def prepare_clear(self, room, value, editor):
        self.require_editor(editor)
        with self._lock, self._db() as db:
            entity = self.admin_entity(room, value)
            count = db.execute("SELECT COUNT(*) FROM knowledge_items WHERE entity_id=? AND epoch=? AND status='active'",
                               (entity["id"], entity["epoch"])).fetchone()[0]
            code = secrets.token_hex(4)
            db.execute("INSERT INTO maintenance_confirmations VALUES(?,?,?,?,?,?,?)",
                       (code, self.account, room, editor_label(editor), entity["id"],
                        self.fingerprint(room, entity["id"]), self.members._now().timestamp()+300))
            return entity, count, code

    def confirm_clear(self, room, code, editor):
        self.require_editor(editor)
        with self._lock, self._db() as db:
            row = db.execute("SELECT * FROM maintenance_confirmations WHERE code=? AND account=? AND room=? AND editor=?",
                             (code, self.account, room, editor_label(editor))).fetchone()
            if not row or row["expires"] < self.members._now().timestamp():
                raise MemberError("确认编号无效或已过期，请重新预览")
            if row["fingerprint"] != self.fingerprint(room, row["entity_id"]):
                raise MemberError("资料在预览后发生变化，请重新预览")
            entity = self.admin_entity(room, row["entity_id"])
            floor = self.members.latest_id(room)
            db.execute("UPDATE knowledge_items SET status='archived',revision=revision+1 WHERE entity_id=? AND epoch=?",
                       (entity["id"], entity["epoch"]))
            db.execute("UPDATE knowledge_entities SET epoch=epoch+1,learning_floor=? WHERE id=?", (floor, entity["id"]))
            db.execute("DELETE FROM member_styles WHERE entity_id=?", (entity["id"],))
            db.execute("DELETE FROM maintenance_confirmations WHERE code=?", (code,))
            self.entity_audit(room, entity["id"], editor, "clear-and-relearn", entity,
                              {"epoch": entity["epoch"]+1, "learning_floor": floor})
            return entity["name"]

    def epoch_snapshot(self, room):
        with self._lock:
            return {e["id"]: e["epoch"] for e in self._entities(room)}

    def pending_binding(self, room):
        """仍然保留资料但尚未绑定微信身份的人物，供管理员补绑核对。"""
        with self._lock:
            return [e for e in self._entities(room)
                    if e["entity_key"].startswith("person:") and e["status"] == "active" and not e["wxid"]]

    def quarantine_person(self, room, value, editor):
        """隔离误建/可疑人物：不再出现在列表、检索与模型上下文，资料和来源原样保留。"""
        self.require_editor(editor)
        with self._lock, self._db() as db:
            entity = self.admin_entity(room, value)
            if not entity["entity_key"].startswith("person:"):
                raise MemberError("只能隔离人物档案")
            before = dict(entity)
            db.execute("UPDATE knowledge_entities SET status='quarantined' WHERE id=?", (entity["id"],))
            entity["status"] = "quarantined"
            self.entity_audit(room, entity["id"], editor, "quarantine", before, entity)
            return entity

    def restore_person(self, room, value, editor):
        self.require_editor(editor)
        with self._lock, self._db() as db:
            token = str(value).lstrip("#")
            row = db.execute("SELECT * FROM knowledge_entities WHERE account=? AND room=? AND id=?",
                             (self.account, room, int(token))).fetchone() if token.isdigit() else None
            if row is None:
                try:
                    entity = self.admin_entity(room, value)
                except MemberError:
                    entity = None
                if entity is not None and entity["status"] != "quarantined":
                    raise MemberError("该人物当前不是隔离状态")
                if entity is None:
                    raise MemberError("没有找到这个隔离人物，请用 #编号 恢复")
                row = db.execute("SELECT * FROM knowledge_entities WHERE id=?", (entity["id"],)).fetchone()
            if row is None or not row["entity_key"].startswith("person:"):
                raise MemberError("只能恢复被隔离的人物档案")
            before = dict(row)
            db.execute("UPDATE knowledge_entities SET status='active' WHERE id=?", (row["id"],))
            after = dict(db.execute("SELECT * FROM knowledge_entities WHERE id=?", (row["id"],)).fetchone())
            self.entity_audit(room, row["id"], editor, "restore-person", before, after)
            return after

    def merge_person(self, room, source, target, editor):
        """把同一个人重复建立的档案合并到保留档案，条目、来源与编号原样保留。"""
        self.require_editor(editor)
        with self._lock, self._db() as db:
            left, right = self.admin_entity(room, source), self.admin_entity(room, target)
            if left["id"] == right["id"]:
                raise MemberError("源人物和目标人物是同一份档案")
            if not left["entity_key"].startswith("person:") or not right["entity_key"].startswith("person:"):
                raise MemberError("只能合并人物档案")
            if right["wxid"] and left["wxid"] and left["wxid"] != right["wxid"]:
                raise MemberError("两份档案绑定了不同微信身份，请先核对")
            moved = 0
            for item in db.execute("SELECT * FROM knowledge_items WHERE entity_id=?", (left["id"],)).fetchall():
                conflict = db.execute("SELECT 1 FROM knowledge_items WHERE entity_id=? AND epoch=? AND slot=?",
                                      (right["id"], right["epoch"], item["slot"])).fetchone()
                slot = item["slot"] if not conflict else item["slot"][:60] + "#" + str(item["id"])
                active_epoch = item["epoch"] == left["epoch"]
                db.execute("UPDATE knowledge_items SET entity_id=?,slot=?,epoch=?,status=? WHERE id=?",
                           (right["id"], slot, right["epoch"] if active_epoch else -item["id"],
                            item["status"] if active_epoch else "archived", item["id"]))
                moved += 1
            db.execute("UPDATE OR IGNORE knowledge_mentions SET entity_id=? WHERE entity_id=?", (right["id"], left["id"]))
            db.execute("DELETE FROM knowledge_mentions WHERE entity_id=?", (left["id"],))
            db.execute("UPDATE OR REPLACE member_styles SET entity_id=? WHERE entity_id=?", (right["id"], left["id"]))
            db.execute("DELETE FROM member_styles WHERE entity_id=?", (left["id"],))
            aliases = sorted(set(parse_aliases(right["aliases"])) | set(parse_aliases(left["aliases"])) |
                             {name_key(left["name"])} - {name_key(right["name"])})
            wxid = right["wxid"] or left["wxid"]
            db.execute("UPDATE knowledge_entities SET aliases=?,wxid=?,pinned=1 WHERE id=?",
                       (json.dumps(aliases, ensure_ascii=False), wxid, right["id"]))
            db.execute("DELETE FROM knowledge_entities WHERE id=?", (left["id"],))
            merged = dict(db.execute("SELECT * FROM knowledge_entities WHERE id=?", (right["id"],)).fetchone())
            self.sync_person_member(room, right["id"])
            self.entity_audit(room, right["id"], editor, "merge-person", left, merged)
            return merged, moved
