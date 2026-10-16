"""本机私有资料修订与启动公告；公开代码不包含实际群、人物及维护者资料。"""
from __future__ import annotations

import json
import hashlib
import logging
from pathlib import Path

from .database import backup_database
from .knowledge import KINDS, sanitized
from .members import MemberError, name_key, parse_aliases

logger = logging.getLogger("Upgrade")


def read_upgrade(knowledge, path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or data.get("account") != knowledge.account:
        raise MemberError("资料修订文件的机器人账号不匹配")
    upgrade_id, room = data.get("id"), data.get("room")
    sanitized(upgrade_id, 80)
    knowledge._room(room)
    profiles, moves, removed = data.get("profiles", []), data.get("moves", []), data.get("remove_aliases", {})
    if not isinstance(profiles, list) or not isinstance(moves, list) or not isinstance(removed, dict):
        raise MemberError("资料修订文件结构不正确")
    names = set()
    for profile in profiles:
        name = name_key(profile["name"])
        if name in names:
            raise MemberError("资料修订人物重复")
        names.add(name)
        for alias in profile.get("aliases", []):
            name_key(alias)
        for memory in profile.get("memories", []):
            memory["key"] = sanitized(memory["key"], 80)
            memory["content"] = sanitized(memory["content"])
            if memory.get("kind", "人工确认") not in KINDS:
                raise MemberError("资料修订类别不正确")
            tags = memory.get("tags", [])
            if not isinstance(tags, list) or len(tags) > 10:
                raise MemberError("资料修订标签应为不超过十项的列表")
            memory["tags"] = [sanitized(tag, 40) for tag in tags]
    for person, aliases in removed.items():
        name_key(person)
        if not isinstance(aliases, list):
            raise MemberError("移除别名必须为列表")
        for alias in aliases:
            name_key(alias)
    for move in moves:
        name_key(move["from"])
        name_key(move["to"])
        if not isinstance(move.get("slots"), list):
            raise MemberError("迁移主题必须为列表")
        for slot in move["slots"]:
            sanitized(slot, 80)
    return data


def apply_upgrade(knowledge, path):
    data = read_upgrade(knowledge, path)
    upgrade_id, room = data["id"], data["room"]
    profiles, moves, removed = data.get("profiles", []), data.get("moves", []), data.get("remove_aliases", {})
    with knowledge._lock:
        db = knowledge._db()
        if db.execute("SELECT 1 FROM applied_upgrades WHERE account=? AND upgrade_id=?",
                      (knowledge.account, upgrade_id)).fetchone():
            return False
        backup_database(db, knowledge.members.path, "before-profile-fix")
        with db:
            # 全部直接 SQL 在同一个事务内完成，失败不会留下半份人物修订。
            for person, aliases in removed.items():
                try:
                    entity = knowledge.find_person(room, person)
                except MemberError:
                    continue  # 全新数据库没有待清理的旧别名。
                retained = set(parse_aliases(entity["aliases"])) - {name_key(a) for a in aliases}
                db.execute("UPDATE knowledge_entities SET aliases=?,aliases_manual=1 WHERE id=?",
                           (json.dumps(sorted(retained), ensure_ascii=False), entity["id"]))
                for alias in aliases:
                    db.execute("INSERT OR IGNORE INTO knowledge_alias_blocks VALUES(?,?,?,?)",
                               (knowledge.account, room, entity["id"], name_key(alias)))
                knowledge.entity_audit(room, entity["id"], "local-upgrade", "remove-aliases", entity, sorted(retained))
            for profile in profiles:
                key = "person:" + name_key(profile["name"])
                aliases = sorted({name_key(a) for a in profile.get("aliases", [])})
                existing = db.execute("SELECT * FROM knowledge_entities WHERE account=? AND room=? AND entity_key=?",
                                      (knowledge.account, room, key)).fetchone()
                knowledge.assert_person_names(room, profile["name"], aliases, existing["id"] if existing else None)
                db.execute("""INSERT INTO knowledge_entities(account,room,entity_key,name,aliases,pinned,aliases_manual)
                    VALUES(?,?,?,?,?,1,1) ON CONFLICT(account,room,entity_key) DO UPDATE SET
                    aliases=excluded.aliases,pinned=1,aliases_manual=1""",
                    (knowledge.account, room, key, profile["name"], json.dumps(aliases, ensure_ascii=False)))
            for move in moves:
                source, target = knowledge.find_person(room, move["from"]), knowledge.find_person(room, move["to"])
                for slot in move["slots"]:
                    item = db.execute("SELECT * FROM knowledge_items WHERE entity_id=? AND epoch=? AND slot=?",
                                      (source["id"], source["epoch"], slot)).fetchone()
                    if not item:
                        continue
                    content = item["content"].replace(source["name"], target["name"])
                    db.execute("UPDATE knowledge_items SET entity_id=?,epoch=?,content=?,manual=1,revision=revision+1,updated_at=? WHERE id=?",
                               (target["id"], target["epoch"], content, knowledge.now(), item["id"]))
                    knowledge.entity_audit(room, target["id"], "local-upgrade", "move-memory", dict(item),
                                           {"item_id": item["id"], "content": content})
            for profile in profiles:
                entity = knowledge.find_person(room, profile["name"])
                for memory in profile.get("memories", []):
                    old = db.execute("SELECT * FROM knowledge_items WHERE entity_id=? AND epoch=? AND slot=?",
                                     (entity["id"], entity["epoch"], memory["key"])).fetchone()
                    db.execute("""INSERT INTO knowledge_items(account,room,entity_id,slot,kind,content,tags,manual,updated_at,epoch)
                        VALUES(?,?,?,?,?,?,?,1,?,?) ON CONFLICT(account,room,entity_id,epoch,slot) DO UPDATE SET
                        kind=excluded.kind,content=excluded.content,tags=excluded.tags,manual=1,
                        revision=knowledge_items.revision+1,updated_at=excluded.updated_at""",
                        (knowledge.account, room, entity["id"], memory["key"], memory.get("kind", "人工确认"),
                         memory["content"], json.dumps(memory["tags"], ensure_ascii=False), knowledge.now(), entity["epoch"]))
                    item_id = db.execute("SELECT id FROM knowledge_items WHERE entity_id=? AND epoch=? AND slot=?",
                                         (entity["id"], entity["epoch"], memory["key"])).fetchone()[0]
                    db.execute("INSERT OR IGNORE INTO knowledge_sources VALUES(?,?,NULL,NULL,?)",
                               (item_id, "owner-upgrade:" + upgrade_id, knowledge.now()))
                    knowledge.entity_audit(room, entity["id"], "local-upgrade", "owner-confirmed",
                                           dict(old) if old else {}, memory)
            db.execute("INSERT INTO applied_upgrades VALUES(?,?,?)", (knowledge.account, upgrade_id, knowledge.now()))
        return True


class StartupNotice:
    """首次启动公告：公告 ID 与正文哈希共同构成版本键，正文一变就重新公告一次。"""

    def __init__(self, knowledge, service, options):
        self.knowledge, self.service, self.options = knowledge, service, options
        digest = hashlib.sha256(options["text"].encode("utf-8")).hexdigest()[:12]
        self.notice_id = f"{options['id']}#{digest}"

    def status(self):
        k, o = self.knowledge, self.options
        with k._lock:
            row = k._db().execute("SELECT status FROM startup_notices WHERE account=? AND room=? AND notice_id=?",
                                  (k.account, o["room"], self.notice_id)).fetchone()
            return row[0] if row else "pending"

    def retry(self):
        k, o = self.knowledge, self.options
        with k._lock, k._db() as db:
            db.execute("DELETE FROM startup_notices WHERE account=? AND room=? AND notice_id=?",
                       (k.account, o["room"], self.notice_id))

    def send(self):
        k, o, session = self.knowledge, self.options, self.service.session_id
        if not session:
            return False
        k._room(o["room"])
        with k._lock, k._db() as db:
            claimed = db.execute("INSERT OR IGNORE INTO startup_notices VALUES(?,?,?,'sending',?)",
                                  (k.account, o["room"], self.notice_id, k.now())).rowcount
            if not claimed:
                return False
        status = "unknown"
        try:
            success = self.service.send_text(o["room"], o["text"], expected_session=session)
            status = "sent" if success else "unknown"
            return bool(success)
        except Exception as error:
            logger.warning("更新公告结果待确认：%s", type(error).__name__)
            return False
        finally:
            with k._lock, k._db() as db:
                db.execute("UPDATE startup_notices SET status=?,updated_at=? WHERE account=? AND room=? AND notice_id=?",
                           (status, k.now(), k.account, o["room"], self.notice_id))
