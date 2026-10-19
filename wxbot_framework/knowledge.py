"""有来源、按群隔离的知识库。模型不能决定微信身份或获得编辑权限。"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta, timezone
from pathlib import Path

from .members import MemberError, name_key, parse_aliases, parse_json_list
from .memory_schema import initialize_schema
from .commands import effective_text
from .knowledge_admin import KnowledgeAdminMixin
from .authorization import editor_label
from .timeutil import iso_utc

KINDS = frozenset({"人工确认", "本人陈述", "他人提及", "群内玩笑", "存在争议", "语言习惯"})
GROUP_KEY = "group:本群"
GROUP_NAME = "本群"
# 群级公共记忆的自动沉淀门槛：至少 3 条去重原始消息，且来自 2 位不同成员。
GROUP_EVIDENCE_MESSAGES = 3
GROUP_EVIDENCE_SPEAKERS = 2
# 候选公共记忆 30 天没有新增有效证据后归档，内容与来源保留，管理员可恢复。
CANDIDATE_IDLE_DAYS = 30


def sanitized(value, limit=600):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise MemberError("记忆内容为空或过长")
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value).strip()
    value = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[联系方式已省略]", value)
    value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[邮箱已省略]", value)
    value = re.sub(r"(?<!\w)wxid_[A-Za-z0-9_]+", "[成员标识]", value)
    return value


def read_seed(path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or not isinstance(data.get("groups"), dict):
        raise MemberError("知识种子文件需要 groups 对象")
    for room, profiles in data["groups"].items():
        if not isinstance(profiles, list):
            raise MemberError("知识种子的每个群应为人物列表")
        claimed = set()
        for profile in profiles:
            if not isinstance(profile, dict):
                raise MemberError("知识种子人物应为对象")
            name_key(profile.get("name"))
            aliases, memories = profile.get("aliases", []), profile.get("memories", [])
            if not isinstance(aliases, list) or not isinstance(memories, list):
                raise MemberError("知识种子的 aliases 和 memories 应为列表")
            for alias in aliases:
                name_key(alias)
            names = {name_key(profile["name"]), *(name_key(alias) for alias in aliases)}
            if claimed & names:
                raise MemberError("知识种子中的人物主名或别名存在归属冲突")
            claimed.update(names)
            for entry in memories:
                if not isinstance(entry, dict) or entry.get("kind", "群内玩笑") not in KINDS:
                    raise MemberError("知识种子条目类别不正确")
                sanitized(entry.get("key"), 80)
                sanitized(entry.get("content"))
                tags = entry.get("tags", [])
                if not isinstance(tags, list) or len(tags) > 10:
                    raise MemberError("知识种子标签应为不超过十项的列表")
                for tag in tags:
                    sanitized(tag, 40)
    return data


class KnowledgeStore(KnowledgeAdminMixin):
    def __init__(self, members, *, display_timezone=None):
        self.members = members
        self.account = members.account
        self._lock = members._lock
        self.display_timezone = display_timezone or "Asia/Shanghai"

    def initialize(self, seed_file=None, *, upgrade_file=None):
        with self._lock:
            initialize_schema(self.members._connection(), self.members.path)
            self._heal_json_columns()
        if upgrade_file and Path(upgrade_file).is_file():
            from .upgrades import apply_upgrade
            apply_upgrade(self, upgrade_file)
        if seed_file and Path(seed_file).is_file():
            self.import_seed(seed_file)

    def _heal_json_columns(self):
        """自愈手动改库导致的空串、大小写未归一或非规范 JSON 列表，并自动标记人工修订。"""
        with self._db() as db:
            for row in db.execute("SELECT id, room, name, entity_key, aliases, wxid FROM knowledge_entities WHERE account=?",
                                  (self.account,)).fetchall():
                cleaned = [alias for alias in sorted(parse_aliases(row["aliases"])) if alias != name_key(row["name"])]
                canonical = json.dumps(cleaned, ensure_ascii=False)
                if row["aliases"] != canonical:
                    db.execute("UPDATE knowledge_entities SET aliases=?,aliases_manual=1 WHERE id=?",
                               (canonical, row["id"]))
                    if row["wxid"] and row["entity_key"].startswith("person:"):
                        self.sync_person_member(row["room"], row["id"])
            for row in db.execute("SELECT id, tags FROM knowledge_items WHERE account=?", (self.account,)).fetchall():
                cleaned_tags = parse_json_list(row["tags"])[:10]
                canonical_tags = json.dumps(cleaned_tags, ensure_ascii=False)
                if row["tags"] != canonical_tags:
                    db.execute("UPDATE knowledge_items SET tags=? WHERE id=?", (canonical_tags, row["id"]))

    def _db(self):
        return self.members._connection()

    def now(self):
        return iso_utc(self.members._now())

    def _room(self, room):
        if room not in self.members.groups:
            raise MemberError("这个群尚未启用知识库")

    def entity(self, room, name, *, aliases=(), pinned=False, reactivate=True):
        """人物档案只在管理员确认后建档；自动提炼、随口提及都不再自动创建。"""
        self._room(room)
        if name_key(name) == name_key(GROUP_NAME):
            # 「本群」永远落在群公共记忆主体，不再生成 person:本群。
            return self.group_entity(room)
        key = "person:" + name_key(name)
        clean = sorted({name_key(alias) for alias in aliases if len(alias.strip()) >= 2})
        with self._lock, self._db() as db:
            existing = db.execute("SELECT * FROM knowledge_entities WHERE account=? AND room=? AND entity_key=?",
                                  (self.account, room, key)).fetchone()
            if existing is None:
                matches = [e for e in self._entities(room) if e["entity_key"].startswith("person:") and
                           name_key(name) in {name_key(e["name"]), *parse_aliases(e["aliases"])}]
                if len(matches) == 1:
                    existing = matches[0]
                    key = existing["entity_key"]
            if existing is None and not pinned:
                raise MemberError("人物档案只能由管理员确认绑定后创建；随手提到的名字不会自动建档")
            if existing and existing["status"] == "quarantined" and not reactivate:
                # 隔离人物不接受种子或自动流程的改写；只能由管理员显式恢复。
                return dict(existing)
            if existing and existing["aliases_manual"]:
                clean = parse_aliases(existing["aliases"])
            if existing:
                blocked = {r[0] for r in db.execute("SELECT alias FROM knowledge_alias_blocks WHERE entity_id=?",
                                                    (existing["id"],))}
                clean = [alias for alias in clean if alias not in blocked]
            self.assert_person_names(room, name, clean, existing["id"] if existing else None)
            db.execute("""INSERT OR IGNORE INTO knowledge_entities
                (account,room,entity_key,name,aliases,pinned,status) VALUES(?,?,?,?,?,?,'active')""",
                (self.account, room, key, name.strip(), json.dumps(clean, ensure_ascii=False), int(pinned)))
            row = db.execute("SELECT * FROM knowledge_entities WHERE account=? AND room=? AND entity_key=?",
                             (self.account, room, key)).fetchone()
            merged = sorted(set(parse_aliases(row["aliases"])) | set(clean))
            db.execute("""UPDATE knowledge_entities SET aliases=?,pinned=MAX(pinned,?),
                status=CASE WHEN ? OR status<>'quarantined' THEN 'active' ELSE status END WHERE id=?""",
                (json.dumps(merged, ensure_ascii=False), int(pinned), int(reactivate), row["id"]))
            return dict(db.execute("SELECT * FROM knowledge_entities WHERE id=?", (row["id"],)).fetchone())

    def group_entity(self, room):
        """本群公共记忆主体；与人物档案分离，避免混入人物列表。"""
        self._room(room)
        with self._lock, self._db() as db:
            row = db.execute("SELECT * FROM knowledge_entities WHERE account=? AND room=? AND entity_key=?",
                             (self.account, room, GROUP_KEY)).fetchone()
            if row is None:
                db.execute("""INSERT OR IGNORE INTO knowledge_entities
                    (account,room,entity_key,name,aliases,pinned,status) VALUES(?,?,?,?,'[]',1,'active')""",
                    (self.account, room, GROUP_KEY, GROUP_NAME))
                row = db.execute("SELECT * FROM knowledge_entities WHERE account=? AND room=? AND entity_key=?",
                                 (self.account, room, GROUP_KEY)).fetchone()
            return dict(row)

    def resolve_person(self, room, name):
        """只在已建档案里查；找不到或同名歧义都返回 None，绝不新建。"""
        key = name_key(name)
        with self._lock:
            self._room(room)
            matches = [e for e in self._entities(room) if e["entity_key"].startswith("person:") and
                       e["status"] == "active" and key in {name_key(e["name"]), *parse_aliases(e["aliases"])}]
            return matches[0] if len(matches) == 1 else None

    def person_profile(self, room, entity):
        """给其他成员的姓名查询使用：返回可核对的身份信息。"""
        wxid = entity["wxid"]
        return {"name": entity["name"], "aliases": parse_aliases(entity["aliases"]), "bound": bool(wxid),
                "wechat_name": self.members.wechat_name(wxid) if wxid else None}

    def lookup_people(self, room, text, *, limit=3):
        """从当前提问里找出被提到的已建档人物，供其他成员按姓名查询身份。"""
        self._room(room)
        query = (text or "").casefold()
        if not query:
            return []
        with self._lock:
            found = []
            for entity in self._entities(room):
                if entity["status"] != "active" or not entity["entity_key"].startswith("person:"):
                    continue
                names = [entity["name"], *parse_aliases(entity["aliases"])]
                if any(len(name) >= 2 and name.casefold() in query for name in names):
                    found.append(entity)
            found.sort(key=lambda item: (not bool(item["wxid"]), len(item["name"])))
            return [self.person_profile(room, entity) for entity in found[:limit]]

    def member_entity(self, room, wxid):
        self._room(room)
        with self._lock, self._db() as db:
            row = db.execute("SELECT * FROM knowledge_entities WHERE account=? AND room=? AND wxid=?",
                             (self.account, room, wxid)).fetchone()
            if row:
                display = self.members.display_name(wxid, room)
                if display and row["entity_key"].startswith("member:") and display != row["name"]:
                    db.execute("UPDATE knowledge_entities SET name=? WHERE id=?", (display, row["id"]))
                    row = dict(row)
                    row["name"] = display
                return dict(row)
            seen = db.execute("SELECT 1 FROM room_members WHERE account=? AND room=? AND wxid=?",
                              (self.account, room, wxid)).fetchone()
            if not seen:
                raise MemberError("这个成员还没有在本群留下记录")
            name = self.members.display_name(wxid, room) or "未绑定成员"
            db.execute("""INSERT INTO knowledge_entities(account,room,entity_key,name,wxid,status)
                VALUES(?,?,?,?,?,'active')""", (self.account, room, "member:" + wxid, name, wxid))
            return dict(db.execute("SELECT * FROM knowledge_entities WHERE account=? AND room=? AND wxid=?",
                                   (self.account, room, wxid)).fetchone())

    def _entities(self, room):
        return [dict(row) for row in self._db().execute(
            "SELECT * FROM knowledge_entities WHERE account=? AND room=?", (self.account, room))]

    def find_entity(self, room, name):
        key = name_key(name)
        with self._lock:
            self._room(room)
            if key == name_key(GROUP_NAME):
                return self.group_entity(room)
            matches = [e for e in self._entities(room) if e["status"] == "active" and
                       key in {name_key(e["name"]), *parse_aliases(e["aliases"])}]
            # 明确绑定的称呼优先作为成员查询；不据此关联同名空间人物。
            try:
                member = self.members.resolve(name, room)
            except MemberError:
                member = None
            if member and member.wxid:
                return self.member_entity(room, member.wxid)
            if len(matches) != 1:
                raise MemberError("没有找到唯一人物，请使用更完整的名字或先绑定称呼")
            return matches[0]

    def find_person(self, room, name):
        """提到的名字只能查人物，不能按微信显示名推断身份。"""
        key = name_key(name)
        with self._lock:
            self._room(room)
            matches = [e for e in self._entities(room) if e["entity_key"].startswith("person:") and
                       e["status"] == "active" and key in {name_key(e["name"]), *parse_aliases(e["aliases"])}]
            if len(matches) != 1:
                raise MemberError("未找到唯一人物")
            return matches[0]

    def link(self, room, wxid, name, editor, *, create_if_missing=False, replace=False):
        if not self.members.is_admin(editor):
            raise MemberError("只有管理员可以关联人物")
        from .text import validate_mentions
        validate_mentions(room, [wxid])
        with self._lock:
            self._room(room)
            now = self.now()
            with self._db() as db:
                db.execute("""INSERT INTO room_members(account,room,wxid,first_seen,last_seen) VALUES(?,?,?,?,?)
                    ON CONFLICT(account,room,wxid) DO UPDATE SET first_seen=COALESCE(room_members.first_seen,excluded.first_seen)""",
                    (self.account, room, wxid, now, now))
            # 关联目标只取人物档案，不按刚绑定的同名微信昵称替换。
            token = str(name).strip()
            if token.startswith("#") and token[1:].isdigit():
                matches = [e for e in self._entities(room) if e["entity_key"].startswith("person:") and e["id"] == int(token[1:])]
            else:
                key = name_key(token)
                matches = [e for e in self._entities(room) if e["entity_key"].startswith("person:") and
                           key in {name_key(e["name"]), *parse_aliases(e["aliases"])}]
            if not matches and create_if_missing:
                matches = [self.entity(room, token, pinned=True)]
            if len(matches) != 1:
                raise MemberError("请指定唯一的已有空间人物或高频人物")
            target = matches[0]
            if target["wxid"] and target["wxid"] != wxid:
                if not replace:
                    raise MemberError("该人物已关联其他 ID，未修改原关联")
            old = self.member_entity(room, wxid)
            if old["id"] == target["id"]:
                with self._db():
                    self.sync_person_member(room, target["id"])
                return self.member_entity(room, wxid)
            if old["entity_key"].startswith("person:"):
                if not replace:
                    raise MemberError("该成员已关联另一人物，请先核对，不能自动合并")
            with self._db() as db:
                previous_target_wxid = target["wxid"]
                if old["entity_key"].startswith("person:"):
                    db.execute("UPDATE knowledge_entities SET wxid=NULL WHERE id=?", (old["id"],))
                    self.sync_person_member(room, old["id"], previous_wxid=wxid)
                else:
                    for item in db.execute("SELECT * FROM knowledge_items WHERE entity_id=?", (old["id"],)).fetchall():
                        conflict = db.execute("SELECT 1 FROM knowledge_items WHERE entity_id=? AND slot=?",
                                              (target["id"], item["slot"])).fetchone()
                        slot = item["slot"] + f"（成员记录{item['id']}）" if conflict else item["slot"]
                        active_epoch = item["epoch"] == old["epoch"]
                        db.execute("UPDATE knowledge_items SET entity_id=?,slot=?,epoch=?,status=? WHERE id=?",
                                   (target["id"], slot, target["epoch"] if active_epoch else -item["id"],
                                    item["status"] if active_epoch else "archived", item["id"]))
                    db.execute("UPDATE OR IGNORE knowledge_mentions SET entity_id=? WHERE entity_id=?", (target["id"], old["id"]))
                    db.execute("DELETE FROM knowledge_mentions WHERE entity_id=?", (old["id"],))
                    db.execute("UPDATE OR REPLACE member_styles SET entity_id=? WHERE entity_id=?", (target["id"], old["id"]))
                    db.execute("DELETE FROM knowledge_entities WHERE id=?", (old["id"],))
                db.execute("UPDATE knowledge_entities SET wxid=?,pinned=1 WHERE id=?", (wxid, target["id"]))
                self.sync_person_member(room, target["id"], previous_wxid=previous_target_wxid)
                self.entity_audit(room, target["id"], editor, "link", target, {"wxid": wxid, "member_entity": old["id"]})
            return self.member_entity(room, wxid)

    def speaker_profile(self, room, wxid):
        self._room(room)
        with self._lock:
            entity = self.member_entity(room, wxid)
            wechat = self.members.wechat_name(wxid)
            if entity["entity_key"].startswith("person:"):
                return {
                    "bound": True,
                    "name": entity["name"],
                    "aliases": parse_aliases(entity["aliases"]),
                    "wechat_name": wechat,
                }
            safe_name = self.members.safe_wechat_name(wxid, room)
            return {
                "bound": False,
                "name": safe_name or "群友",
                "aliases": [],
                "wechat_name": safe_name,
            }

    def put(self, room, entity_id, slot, kind, content, *, tags=(), sources=(), origin=None, expected_epoch=None):
        """自动写入只更新非人工、非遗忘条目；调用方负责事务边界。"""
        self._room(room)
        slot, content = sanitized(slot, 80), sanitized(content)
        if kind not in KINDS:
            raise MemberError("未知知识类别")
        if not isinstance(tags, (list, tuple)) or len(tags) > 10:
            raise MemberError("知识标签应为不超过十项的列表")
        tags = [sanitized(tag, 40) for tag in tags]
        db = self._db()
        entity = db.execute("SELECT * FROM knowledge_entities WHERE id=? AND account=? AND room=?",
                            (entity_id, self.account, room)).fetchone()
        if not entity:
            raise MemberError("知识人物不属于当前群")
        if entity["status"] == "quarantined":
            return None
        if expected_epoch is not None and entity["epoch"] != expected_epoch:
            return None
        if origin and origin.startswith("seed:") and entity["epoch"]:
            return None
        if sources and any(source["id"] <= entity["learning_floor"] for source in sources):
            return None
        old = db.execute("SELECT * FROM knowledge_items WHERE account=? AND room=? AND entity_id=? AND epoch=? AND slot=?",
                         (self.account, room, entity_id, entity["epoch"], slot)).fetchone()
        if old and (old["manual"] or old["status"] == "forgotten"):
            return old["id"]
        if old:
            # 不把相反说法悄悄覆盖；保留版本为另一条可见的争议记录。
            if old["content"] != content and old["kind"] != "语言习惯":
                slot = slot[:60] + "#" + hashlib.sha256(content.encode()).hexdigest()[:10]
                kind = "存在争议" if kind != "群内玩笑" else kind
        # 群级自动记忆先落候选，攒够跨成员证据才对外生效；已生效的旧条目不会被重新降级。
        status = "candidate" if entity["entity_key"].startswith("group:") else "active"
        now = self.now()
        db.execute("""INSERT INTO knowledge_items(account,room,entity_id,slot,kind,content,tags,updated_at,epoch,status)
            VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account,room,entity_id,epoch,slot) DO UPDATE SET
            content=excluded.content,tags=excluded.tags,updated_at=excluded.updated_at,
            revision=knowledge_items.revision+1 WHERE knowledge_items.manual=0 AND knowledge_items.status='active'
            AND (knowledge_items.content<>excluded.content OR knowledge_items.tags<>excluded.tags)""",
            (self.account, room, entity_id, slot, kind, content, json.dumps(tags, ensure_ascii=False), now,
             entity["epoch"], status))
        item_id = db.execute("SELECT id FROM knowledge_items WHERE account=? AND room=? AND entity_id=? AND epoch=? AND slot=?",
                             (self.account, room, entity_id, entity["epoch"], slot)).fetchone()[0]
        added_message = False
        for source in sources:
            added_message = db.execute("INSERT OR IGNORE INTO knowledge_sources VALUES(?,?,?,?,?)",
                                       (item_id, "message:" + str(source["id"]), source["id"], source["sender_id"],
                                        source["received_at"])).rowcount > 0 or added_message
        if origin:
            db.execute("INSERT OR IGNORE INTO knowledge_sources VALUES(?,?,NULL,NULL,?)", (item_id, origin, now))
        if entity["entity_key"].startswith("group:"):
            if added_message:
                # 新证据就是一次“活动”：刷新归档计时，并让已归档候选重新参与晋升。
                db.execute("""UPDATE knowledge_items SET updated_at=?,revision=revision+1,
                    status=CASE WHEN status='archived' THEN 'candidate' ELSE status END
                    WHERE id=? AND status IN ('candidate','archived')""", (now, item_id))
            self._refresh_group_item(db, item_id)
        return item_id

    def _refresh_group_item(self, db, item_id):
        """群级自动记忆只有攒够跨成员证据才对外可见，否则留在候选中。"""
        row = db.execute("SELECT manual,status FROM knowledge_items WHERE id=?", (item_id,)).fetchone()
        if row is None or row["status"] != "candidate":
            return
        messages, speakers = self.group_evidence(db, item_id)
        if messages >= GROUP_EVIDENCE_MESSAGES and speakers >= GROUP_EVIDENCE_SPEAKERS:
            db.execute("UPDATE knowledge_items SET status='active' WHERE id=?", (item_id,))

    @staticmethod
    def group_evidence(db, item_id):
        """去重原始消息数与参与成员数；机器人回复与同一句复读都不计入。"""
        rows = db.execute("""SELECT message_id,speaker_id FROM knowledge_sources
            WHERE item_id=? AND message_id IS NOT NULL""", (item_id,)).fetchall()
        messages, speakers = set(), set()
        for row in rows:
            messages.add(row["message_id"])
            speakers.add(row["speaker_id"])
        return len(messages), len(speakers)

    def archive_stale_candidates(self, room, *, days=CANDIDATE_IDLE_DAYS):
        """候选公共记忆长期没有新增有效证据后归档，内容和来源保留。"""
        self._room(room)
        cutoff = iso_utc(self.members._now() - timedelta(days=days))
        with self._lock, self._db() as db:
            rows = db.execute("""SELECT id FROM knowledge_items WHERE account=? AND room=?
                AND status='candidate' AND replace(updated_at,'+00:00','Z')<?""",
                (self.account, room, cutoff)).fetchall()
            for row in rows:
                db.execute("UPDATE knowledge_items SET status='archived',revision=revision+1 WHERE id=?", (row["id"],))
            return len(rows)

    def import_seed(self, path):
        data = read_seed(path)
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
        for room, profiles in data["groups"].items():
            if room not in self.members.groups:
                continue
            for profile in profiles:
                profile_key = name_key(profile["name"])
                claimed_elsewhere = set()
                for row in self._entities(room):
                    if not row["entity_key"].startswith("person:"):
                        continue
                    row_names = {name_key(row["name"]), *parse_aliases(row["aliases"])}
                    if profile_key in row_names:
                        continue
                    claimed_elsewhere.update(row_names)
                seed_aliases = [alias for alias in profile.get("aliases", []) if name_key(alias) not in claimed_elsewhere]
                entity = self.entity(room, profile["name"], aliases=seed_aliases, pinned=True, reactivate=False)
                if entity.get("status") == "quarantined":
                    continue
                with self._lock, self._db():
                    for entry in profile.get("memories", []):
                        kind = entry.get("kind", "群内玩笑")
                        item_id = self.put(room, entity["id"], entry["key"], kind, entry["content"],
                                           tags=entry.get("tags", []), origin="seed:" + digest + ":" + entry["key"])
                        if not item_id:
                            continue
                        # 种子是管理员维护的内容，但不得覆盖 /忘记 或已归档的人工结果。
                        row = self._db().execute("SELECT manual,status FROM knowledge_items WHERE id=?",
                                                 (item_id,)).fetchone()
                        if row and not row["manual"] and row["status"] not in ("forgotten", "archived"):
                            self._db().execute("UPDATE knowledge_items SET manual=1,status='active' WHERE id=?",
                                               (item_id,))

    def cursor(self, room):
        self._room(room)
        with self._lock:
            row = self._db().execute("SELECT cursor_id FROM memory_cursors WHERE account=? AND room=?",
                                     (self.account, room)).fetchone()
            return row[0] if row else 0

    def batch(self, room, limit=50):
        with self._lock:
            result, effective = [], 0
            for row in self._db().execute("""SELECT * FROM messages
                WHERE account=? AND room=? AND id>? ORDER BY id LIMIT ?""",
                (self.account, room, self.cursor(room), max(1000, limit))):
                result.append(dict(row))
                effective += int(not row["bot_generated"] and effective_text(row["text"]))
                if effective >= limit:
                    break
            return result

    def advance(self, room, cursor):
        self._db().execute("""INSERT INTO memory_cursors(account,room,cursor_id,updated_at) VALUES(?,?,?,?)
            ON CONFLICT(account,room) DO UPDATE SET cursor_id=MAX(memory_cursors.cursor_id,excluded.cursor_id),
            updated_at=excluded.updated_at,last_error=NULL""", (self.account, room, cursor, self.now()))

    def error(self, room, text):
        with self._lock, self._db() as db:
            db.execute("""INSERT INTO memory_cursors(account,room,last_error) VALUES(?,?,?)
                ON CONFLICT(account,room) DO UPDATE SET last_error=excluded.last_error""",
                (self.account, room, text[:160]))

    def visible(self, entity):
        with self._lock:
            if entity.get("status") == "quarantined":
                return False
            if entity["pinned"] or entity["wxid"]:
                return True
            row = self._db().execute("SELECT COUNT(*),COUNT(DISTINCT speaker_id) FROM knowledge_mentions WHERE entity_id=? AND message_id>?",
                                     (entity["id"], entity["learning_floor"])).fetchone()
            return row[0] >= 3 and row[1] >= 2

    def clear_error(self, room):
        with self._lock, self._db() as db:
            db.execute("UPDATE memory_cursors SET last_error=NULL WHERE account=? AND room=?", (self.account, room))

    def search(self, room, query, *, limit=8, char_limit=4000, entity_id=None):
        self._room(room)
        with self._lock:
            entities = {e["id"]: e for e in self._entities(room)}
            query = query.casefold().strip()
            tokens = set(re.findall(r"[a-z0-9]{2,}", query))
            for phrase in re.findall(r"[\u4e00-\u9fff]{2,}", query):
                tokens.update(phrase[index:index+2] for index in range(len(phrase)-1))
            ranked = []
            for row in self._db().execute("SELECT * FROM knowledge_items WHERE account=? AND room=? AND status='active'",
                                         (self.account, room)):
                entity = entities.get(row["entity_id"])
                if entity is None or entity["status"] != "active" or row["epoch"] != entity["epoch"]:
                    continue
                if not self.visible(entity) or (entity_id is not None and entity["id"] != entity_id):
                    continue
                aliases = [name_key(entity["name"]), *parse_aliases(entity["aliases"])]
                score = 100 if entity_id == entity["id"] else 0
                score += 30 * sum(len(alias) >= 2 and alias in query for alias in aliases)
                score += 8 * sum(tag.casefold() in query for tag in parse_json_list(row["tags"]))
                score += sum(token in row["content"].casefold() for token in tokens)
                if query and query in row["content"].casefold():
                    score += 20
                if not query:
                    score += 1
                if score:
                    item = dict(row)
                    if entity["entity_key"].startswith(("person:", "group:")):
                        item["name"] = entity["name"]
                    else:
                        item["name"] = (self.members.display_name(entity["wxid"], room) if entity["wxid"] else None) or entity["name"]
                    item["scope"] = "group" if entity["entity_key"].startswith("group:") else "person"
                    item["sources"] = [dict(source) for source in self._db().execute(
                        "SELECT origin,message_id,source_time FROM knowledge_sources WHERE item_id=? ORDER BY source_time DESC LIMIT 3",
                        (row["id"],))]
                    ranked.append((score, item))
            ranked.sort(key=lambda pair: (pair[0], pair[1]["manual"], pair[1]["updated_at"], pair[1]["id"]), reverse=True)
            result, size = [], 0
            for _, item in ranked:
                length = len(item["content"]) + len(item["name"]) + 80
                if size + length <= char_limit:
                    result.append(item)
                    size += length
                if len(result) >= limit:
                    break
            return result

    def edit(self, room, item_id, editor, *, content=None, forget=False):
        with self._lock, self._db() as db:
            row = db.execute("""SELECT i.*,e.wxid FROM knowledge_items i JOIN knowledge_entities e ON e.id=i.entity_id
                WHERE i.id=? AND i.account=? AND i.room=?""", (item_id, self.account, room)).fetchone()
            if row is None:
                raise MemberError("本群没有这条记忆")
            self.require_editor(editor)
            current = db.execute("SELECT epoch FROM knowledge_entities WHERE id=?", (row["entity_id"],)).fetchone()[0]
            if row["epoch"] != current:
                raise MemberError("这是清空前的历史条目，请先使用记忆恢复命令")
            replacement = row["content"] if forget else sanitized(content)
            db.execute("""UPDATE knowledge_items SET content=?,status=?,manual=1,revision=revision+1,
                kind=CASE WHEN ? OR kind IN ('群内玩笑','语言习惯') THEN kind ELSE '人工确认' END,updated_at=? WHERE id=?""",
                (replacement, "forgotten" if forget else "active", int(forget), self.now(), item_id))
            # 同主题自动生成的争议变体一起停用，避免遗忘后又从变体中取回。
            db.execute("UPDATE knowledge_items SET status='forgotten',manual=1 WHERE entity_id=? AND epoch=? AND substr(slot,1,?)=? AND id<>?",
                       (row["entity_id"], current, len(row["slot"]) + 1, row["slot"] + "#", item_id))
            db.execute("INSERT INTO knowledge_edits(item_id,editor,action,previous,replacement,edited_at) VALUES(?,?,?,?,?,?)",
                       (item_id, editor_label(editor), "forget" if forget else "correct", row["content"], replacement, self.now()))
            db.execute("UPDATE chat_buffers SET generation=generation+1 WHERE account=? AND room=?", (self.account, room))

    def style(self, room, wxid):
        with self._lock:
            entity = self.member_entity(room, wxid)
            row = self._db().execute("SELECT * FROM member_styles WHERE entity_id=?", (entity["id"],)).fetchone()
            item = self._db().execute("SELECT content FROM knowledge_items WHERE entity_id=? AND epoch=? AND slot='表达习惯' AND status='active'",
                                      (entity["id"], entity["epoch"])).fetchone()
            return {"description": item[0], "sample_count": row["sample_count"]} if row and item else None

    def learning_context(self, room, speakers):
        """给提炼器现有主题名，帮助复用稳定key；不提供微信ID。"""
        with self._lock:
            context = []
            for entity in self._entities(room):
                if entity["status"] == "quarantined":
                    continue
                if entity["wxid"] and entity["wxid"] not in speakers:
                    if not entity["pinned"]:
                        continue
                if not entity["wxid"] and not entity["pinned"] and not self.visible(entity):
                    continue
                topics = [dict(row) for row in self._db().execute(
                    "SELECT slot AS key,status,manual FROM knowledge_items WHERE entity_id=? AND epoch=? ORDER BY manual DESC,id DESC LIMIT 8",
                    (entity["id"], entity["epoch"]))]
                context.append({"name": entity["name"], "aliases": parse_aliases(entity["aliases"]),
                                "speaker": speakers.get(entity["wxid"]), "topics": topics,
                                "learning_after_message_id": entity["learning_floor"]})
            return context[:40]

    def status(self):
        with self._lock:
            return {"active_memories": self._db().execute("SELECT COUNT(*) FROM knowledge_items WHERE account=? AND status='active'",
                                                         (self.account,)).fetchone()[0],
                    "candidate_memories": self._db().execute("SELECT COUNT(*) FROM knowledge_items WHERE account=? AND status='candidate'",
                                                            (self.account,)).fetchone()[0],
                    "quarantined_people": self._db().execute("""SELECT COUNT(*) FROM knowledge_entities
                        WHERE account=? AND status='quarantined'""", (self.account,)).fetchone()[0],
                    "learning": [dict(row) for row in self._db().execute(
                        "SELECT room,cursor_id,updated_at,last_error FROM memory_cursors WHERE account=?", (self.account,))]}
