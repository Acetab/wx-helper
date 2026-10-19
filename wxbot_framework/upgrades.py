"""本机私有资料修订与启动公告；公开代码不包含实际群、人物及维护者资料。"""
from __future__ import annotations

import json
import hashlib
import logging
import uuid
from pathlib import Path

from .database import backup_database
from .knowledge import KINDS, sanitized
from .members import MemberError, name_key, parse_aliases
from .timeutil import iso_utc, parse_utc

logger = logging.getLogger("Upgrade")
CONTRIBUTOR_FALLBACK = "Inmata"


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
    """旧版单公告兼容入口；运行时使用 AnnouncementQueue。"""

    def __init__(self, knowledge, service, options):
        self.knowledge, self.service, self.options = knowledge, service, options
        digest = hashlib.sha256(options["text"].encode("utf-8")).hexdigest()[:12]
        self.notice_id = f"{options['id']}#{digest}"
        # 单次运行只尝试一次；上次结果未确认时交给下次启动重试。
        self._attempted = False

    def status(self):
        k, o = self.knowledge, self.options
        with k._lock:
            row = k._db().execute("SELECT status FROM startup_notices WHERE account=? AND room=? AND notice_id=?",
                                  (k.account, o["room"], self.notice_id)).fetchone()
            return row[0] if row else "pending"

    def describe(self):
        """维护命令用的可读状态：本版本公告 ID、目标群、结果与最近尝试时间。"""
        k, o = self.knowledge, self.options
        with k._lock:
            row = k._db().execute("SELECT status,updated_at FROM startup_notices "
                                  "WHERE account=? AND room=? AND notice_id=?",
                                  (k.account, o["room"], self.notice_id)).fetchone()
        status, updated = (row["status"], row["updated_at"]) if row else ("pending", "")
        return {"id": self.notice_id, "room": o["room"], "status": status, "updated_at": updated}

    def retry(self):
        k, o = self.knowledge, self.options
        # 手动重发时允许同一次运行内再次尝试。
        self._attempted = False
        with k._lock, k._db() as db:
            db.execute("DELETE FROM startup_notices WHERE account=? AND room=? AND notice_id=?",
                       (k.account, o["room"], self.notice_id))

    def send(self):
        k, o, session = self.knowledge, self.options, self.service.session_id
        if self._attempted:
            return False
        if not session:
            logger.info("启动公告暂缓：微信会话尚未就绪（%s）", self.notice_id)
            return False
        k._room(o["room"])
        with k._lock, k._db() as db:
            # 已确认发送成功的版本不再重复；结果未确认的允许在下次启动重试。
            claimed = db.execute(
                "INSERT INTO startup_notices(account,room,notice_id,status,updated_at) VALUES(?,?,?,'sending',?) "
                "ON CONFLICT(account,room,notice_id) DO UPDATE SET status='sending',updated_at=excluded.updated_at "
                "WHERE startup_notices.status<>'sent'",
                (k.account, o["room"], self.notice_id, k.now())).rowcount
            if not claimed:
                logger.info("启动公告跳过：本版本已公告过（%s）", self.notice_id)
                return False
        self._attempted = True
        status = "unknown"
        logger.info("启动公告开始发送：%s -> %s", self.notice_id, o["room"])
        try:
            success = self.service.send_text(o["room"], o["text"], expected_session=session)
            status = "sent" if success else "unknown"
            if success:
                logger.info("启动公告已发送：%s -> %s", self.notice_id, o["room"])
            else:
                logger.warning("启动公告结果待确认，下次启动会重试：%s", self.notice_id)
            return bool(success)
        except Exception as error:
            logger.warning("启动公告发送失败，下次启动会重试：%s（%s）", self.notice_id, type(error).__name__)
            return False
        finally:
            with k._lock, k._db() as db:
                db.execute("UPDATE startup_notices SET status=?,updated_at=? WHERE account=? AND room=? AND notice_id=?",
                           (status, k.now(), k.account, o["room"], self.notice_id))


class AnnouncementQueue:
    """按配置顺序尝试本次运行的公告；每项本运行只自动尝试一次。"""

    def __init__(self, knowledge, service, options, *, run_id=None, contributors=None):
        self.knowledge, self.service, self.run_id = knowledge, service, run_id or uuid.uuid4().hex
        self.notices = tuple(options or ())
        self.contributors = contributors
        self._successful = set()
        self._attempted = set()
        for notice in self.notices:
            knowledge._room(notice["room"])

    def notice(self, notice_id=None):
        if notice_id is None:
            if len(self.notices) != 1:
                raise ValueError("配置了多份公告时必须指定公告 ID")
            return self.notices[0]
        matched = [notice for notice in self.notices if notice["id"] == notice_id]
        if not matched:
            raise ValueError(f"未找到公告：{notice_id}")
        return matched[0]

    def status(self, notice_id=None):
        """返回本运行与最近一次历史尝试，不把旧版本的 sent 当成本次已发送。"""
        notice = self.notice(notice_id)
        k = self.knowledge
        with k._lock:
            db = k._db()
            latest = db.execute("""SELECT * FROM startup_notice_attempts
                WHERE account=? AND room=? AND notice_id=? ORDER BY id DESC LIMIT 1""",
                (k.account, notice["room"], notice["id"])).fetchone()
            current = db.execute("""SELECT * FROM startup_notice_attempts
                WHERE account=? AND room=? AND notice_id=? AND run_id=? ORDER BY id DESC LIMIT 1""",
                (k.account, notice["room"], notice["id"], self.run_id)).fetchone()
        local = self.service.session_id is not None
        status = current["status"] if current else ("sent" if notice["id"] in self._successful
                                                     else "pending" if local else "waiting")
        return {
            "id": notice["id"],
            "room": notice["room"],
            "plugins": tuple(notice.get("plugins", ())),
            "status": status,
            "attempted_at": current["attempted_at"] if current else None,
            "updated_at": current["updated_at"] if current else None,
            "last_attempt": dict(latest) if latest else None,
            "run_id": self.run_id,
        }

    def describe(self, notice_id=None):
        return self.status(notice_id)

    def all_status(self):
        return [self.status(notice["id"]) for notice in self.notices]

    def _record_attempt(self, notice, status, attempted_at):
        k = self.knowledge
        timestamp = iso_utc(attempted_at)
        with k._lock, k._db() as db:
            row = db.execute("""INSERT INTO startup_notice_attempts
                (account,room,notice_id,run_id,status,attempted_at,updated_at)
                VALUES(?,?,?,?,?,?,?)""",
                (k.account, notice["room"], notice["id"], self.run_id, status,
                 timestamp, timestamp))
            db.execute("""INSERT INTO startup_notices(account,room,notice_id,status,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(account,room,notice_id) DO UPDATE SET
                status=excluded.status,updated_at=excluded.updated_at""",
                (k.account, notice["room"], notice["id"], status, timestamp))
            return row.lastrowid

    def _send_one(self, notice):
        k, session = self.knowledge, self.service.session_id
        if notice["id"] in self._successful or notice["id"] in self._attempted:
            return False
        if not session:
            logger.info("公告暂缓：微信会话尚未就绪（%s）", notice["id"])
            return False
        k._room(notice["room"])
        self._attempted.add(notice["id"])
        attempted_at = iso_utc(k.members._now())
        attempt_id = self._record_attempt(notice, "sending", attempted_at)
        status = "unknown"
        logger.info("公告开始发送：%s -> %s（run=%s）", notice["id"], notice["room"], self.run_id)
        try:
            success = self.service.send_text(
                notice["room"], announcement_text(notice, self.contributors), expected_session=session)
            status = "sent" if success else "unknown"
            if success:
                self._successful.add(notice["id"])
                logger.info("公告已发送：%s -> %s", notice["id"], notice["room"])
            else:
                logger.warning("公告结果待确认：%s", notice["id"])
            return bool(success)
        except Exception as error:
            logger.warning("公告发送失败：%s（%s）", notice["id"], type(error).__name__)
            return False
        finally:
            completed = iso_utc(k.members._now())
            with k._lock, k._db() as db:
                db.execute("""UPDATE startup_notice_attempts SET status=?,updated_at=?
                    WHERE id=?""", (status, completed, attempt_id))
                db.execute("""UPDATE startup_notices SET status=?,updated_at=?
                    WHERE account=? AND room=? AND notice_id=?""",
                    (status, completed, k.account, notice["room"], notice["id"]))

    def send(self, notice_id=None):
        """一次调用只发送指定公告，或依次发送所有尚未成功的公告。"""
        targets = (self.notice(notice_id),) if notice_id is not None else self.notices
        sent = False
        for notice in targets:
            if self.service.session_id is None:
                break
            sent = self._send_one(notice) or sent
        return sent

    def resend(self, notice_id=None):
        """维护员明确重发时，允许本次运行追加一条独立发送记录。"""
        notice = self.notice(notice_id)
        self._attempted.discard(notice["id"])
        self._successful.discard(notice["id"])
        return self._send_one(notice)


def announcement_text(notice, contributors=None):
    """人工正文在前，插件贡献由配置声明的顺序确定性追加。"""
    text = (notice.get("text") or "").strip()
    return text + ("\n" if text else "") + announcement_contributions(
        notice.get("plugins", ()), contributors)


def _contribution_rows(contributors):
    """读取同一份运行时插件清单；缺项再从仓库清单兼容回退。"""
    if contributors is not None:
        rows = contributors.values() if isinstance(contributors, dict) else contributors
        result = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("插件贡献配置不正确")
            name = row.get("plugin")
            if not isinstance(name, str) or not name:
                raise ValueError("插件贡献配置缺少插件名")
            result[name] = row
        return result

    root = Path(__file__).resolve().parent.parent
    result = {}
    plugins_dir = root / "plugins"
    if plugins_dir.is_dir():
        for directory in sorted(plugins_dir.iterdir()):
            manifest_path = directory / "plugin.json"
            if not directory.is_dir() or not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if not isinstance(manifest, dict):
                continue
            result[directory.name] = {
                "plugin": directory.name,
                "author": manifest.get("author", ""),
                "pull_request_author": manifest.get("pull_request_author", ""),
                "contributors": manifest.get("contributors", []),
            }
    fallback_path = root / "plugin_contributors.json"
    if fallback_path.is_file():
        data = json.loads(fallback_path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("plugin_contributors.json 应为对象")
        for name, row in data.items():
            if not isinstance(row, dict):
                raise ValueError(f"插件贡献配置不正确：{name}")
            result.setdefault(name, {"plugin": name, **row})
    return result


def announcement_contributions(plugins, contributors=None):
    """按公告声明顺序生成署名，任一字段缺失都回退默认维护者。"""
    data = _contribution_rows(contributors)
    lines = []
    for name in plugins:
        row = data.get(name, {"plugin": name})
        author = row.get("author") or CONTRIBUTOR_FALLBACK
        pr_author = row.get("pull_request_author") or CONTRIBUTOR_FALLBACK
        actual = row.get("contributors") or [CONTRIBUTOR_FALLBACK]
        if isinstance(actual, str):
            actual = [actual]
        if (not isinstance(author, str) or not author.strip() or
                not isinstance(pr_author, str) or not pr_author.strip() or
                not isinstance(actual, (list, tuple)) or
                any(not isinstance(item, str) or not item.strip() for item in actual)):
            raise ValueError(f"插件贡献配置不正确：{name}")
        lines.append(
            f"{name}：作者 {author.strip()}；PR 提交者 {pr_author.strip()}；"
            f"实际贡献者 {'、'.join(item.strip() for item in actual)}")
    return "\n".join(lines)


def migrate_legacy_notice(notice):
    if notice is None:
        return None
    return {"id": notice["id"], "room": notice["room"], "plugins": [], "text": notice["text"]}
