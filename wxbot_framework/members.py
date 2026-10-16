"""框架共用的成员身份和新收到的群文本记录，按账号、群及成员 ID 隔离。"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .account import validate_account
from .config import validate_name
from .text import validate_mentions
from .commands import effective_text
from .database import backup_before_migration


class MemberError(ValueError):
    pass


class SnapshotExpired(RuntimeError):
    """清空或跨天后，取消仍在生成/等待发送的总结。"""


BEIJING = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class ChatSnapshot:
    member: Member
    day: str
    generation: int
    messages: tuple
    scope: str = "today"
    anchor_id: int = 0

    @property
    def room(self):
        return self.member.room


@dataclass(frozen=True)
class GroupChatSnapshot:
    room: str
    day: str
    generation: int
    messages: tuple
    scope: str = "today"
    anchor_id: int = 0


@dataclass(frozen=True)
class ConversationSnapshot:
    room: str
    day: str
    generation: int
    speaker_name: str
    messages: tuple
    current: str
    group_messages: tuple = ()
    anchor_id: int = 0


@dataclass(frozen=True)
class Member:
    room: str
    name: str
    display_name: str
    wxid: str | None
    aliases: tuple[str, ...]
    self_named: bool = False


def name_key(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 64 or any(c in value for c in "\0\n\r"):
        raise MemberError("成员称呼应为 1–64 字符的单行文本")
    return value.strip().casefold()


class MemberStore:
    def __init__(self, config_file, account, *, now=None):
        self.account = validate_account(account)
        self.config_file = Path(config_file).resolve()
        options = json.loads(self.config_file.read_text(encoding="utf-8-sig"))
        if not isinstance(options, dict) or not isinstance(options.get("groups"), dict):
            raise MemberError("成员配置应包含 groups 对象")
        database = options.get("database_file", ".runtime/member_data.sqlite3")
        if not isinstance(database, str) or not database.strip() or "\0" in database:
            raise MemberError("database_file 应为数据库路径")
        self.path = (self.config_file.parent / database).resolve()
        self.history_enabled = options.get("history_enabled", False)
        if type(self.history_enabled) is not bool:
            raise MemberError("history_enabled 必须是布尔值")
        self.daily_clear = options.get("daily_clear", False)
        if type(self.daily_clear) is not bool:
            raise MemberError("daily_clear 必须是布尔值")
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.owner_wxid = options.get("owner_wxid")
        if self.owner_wxid is not None:
            validate_account(self.owner_wxid)
            if self.owner_wxid == self.account:
                raise MemberError("维护者必须是独立于机器人本号的微信账号")
        self._active_day = None
        self.groups = set(options["groups"])
        self.profiles = []
        for room, profiles in options["groups"].items():
            validate_mentions(room, [self.account])
            if not isinstance(profiles, list):
                raise MemberError("每个群的成员配置应为列表")
            keys, names, ids = set(), set(), set()
            for profile in profiles:
                if not isinstance(profile, dict):
                    raise MemberError("成员资料应为 JSON 对象")
                key = validate_name(profile.get("key"))
                name = profile.get("name")
                display = profile.get("display_name", name)
                aliases = profile.get("aliases", [])
                if not isinstance(aliases, list):
                    raise MemberError("aliases 应为称呼列表")
                normalized = {name_key(value) for value in [name, display, *aliases]}
                wxid = profile.get("wxid")
                if wxid is not None:
                    validate_mentions(room, [wxid])
                if key in keys or normalized & names or (wxid and wxid in ids):
                    raise MemberError("同一群的成员标识、称呼和微信 ID 不能重复")
                keys.add(key)
                names.update(normalized)
                if wxid:
                    ids.add(wxid)
                self.profiles.append((room, key, name.strip(), display.strip(), wxid, normalized))
        self._lock = threading.RLock()
        self._db = None
        self._closed = False

    @classmethod
    def from_config(cls, config, directory):
        filename = config.get("members_config_file")
        if filename is None:
            return None
        if not isinstance(filename, str) or not filename.strip() or "\0" in filename:
            raise MemberError("members_config_file 应为成员配置文件路径或 null")
        if not config.get("wechat_account"):
            raise MemberError("启用成员目录需要固定 wechat_account，以隔离各机器人账号的数据")
        return cls(Path(directory) / filename, config["wechat_account"])

    def _connection(self):
        if self._closed:
            raise RuntimeError("成员目录已关闭")
        if self._db is not None:
            return self._db
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        db.row_factory = sqlite3.Row
        try:
            backup_before_migration(db, self.path)
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS people (
                    account TEXT NOT NULL, room TEXT NOT NULL, person_key TEXT NOT NULL,
                    name TEXT NOT NULL, display_name TEXT NOT NULL, wxid TEXT,
                    PRIMARY KEY(account, room, person_key), UNIQUE(account, room, wxid));
                CREATE TABLE IF NOT EXISTS aliases (
                    account TEXT NOT NULL, room TEXT NOT NULL, alias TEXT NOT NULL, person_key TEXT NOT NULL,
                    PRIMARY KEY(account, room, alias),
                    FOREIGN KEY(account, room, person_key) REFERENCES people(account, room, person_key));
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY, account TEXT NOT NULL, room TEXT NOT NULL, sender_id TEXT NOT NULL,
                    text TEXT NOT NULL, received_at TEXT NOT NULL, session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL, is_self INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS messages_by_member ON messages(account, room, sender_id, id);
                CREATE UNIQUE INDEX IF NOT EXISTS messages_by_sequence
                    ON messages(account, session_id, sequence) WHERE sequence > 0;
                CREATE TABLE IF NOT EXISTS chat_buffers (
                    account TEXT NOT NULL, room TEXT NOT NULL, local_day TEXT NOT NULL,
                    generation INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(account,room));
                CREATE TABLE IF NOT EXISTS room_personalities (
                    account TEXT NOT NULL, room TEXT NOT NULL, name TEXT NOT NULL,
                    generation INTEGER NOT NULL, PRIMARY KEY(account,room));
                CREATE TABLE IF NOT EXISTS wechat_names (
                    account TEXT NOT NULL, wxid TEXT NOT NULL, name TEXT NOT NULL, name_key TEXT NOT NULL,
                    PRIMARY KEY(account,wxid));
                CREATE TABLE IF NOT EXISTS room_members (
                    account TEXT NOT NULL, room TEXT NOT NULL, wxid TEXT NOT NULL,
                    PRIMARY KEY(account,room,wxid));
                INSERT OR IGNORE INTO room_members(account,room,wxid) SELECT DISTINCT account,room,sender_id FROM messages;
            """)
            if "self_named" not in {row["name"] for row in db.execute("PRAGMA table_info(people)")}:
                db.execute("ALTER TABLE people ADD COLUMN self_named INTEGER NOT NULL DEFAULT 0")
            if "bot_generated" not in {row["name"] for row in db.execute("PRAGMA table_info(messages)")}:
                db.execute("ALTER TABLE messages ADD COLUMN bot_generated INTEGER NOT NULL DEFAULT 0")
            if "context_floor" not in {row["name"] for row in db.execute("PRAGMA table_info(chat_buffers)")}:
                db.execute("ALTER TABLE chat_buffers ADD COLUMN context_floor INTEGER NOT NULL DEFAULT 0")
            for column in ("first_seen", "last_seen"):
                if column not in {row["name"] for row in db.execute("PRAGMA table_info(room_members)")}:
                    db.execute(f"ALTER TABLE room_members ADD COLUMN {column} TEXT")
            db.execute("""CREATE TABLE IF NOT EXISTS binding_reminders (
                account TEXT, room TEXT, wxid TEXT, reminded_at TEXT,
                PRIMARY KEY(account,room,wxid))""")
            with db:
                for room, key, name, display, wxid, aliases in self.profiles:
                    existing = db.execute("SELECT wxid FROM people WHERE account=? AND room=? AND person_key=?",
                                          (self.account, room, key)).fetchone()
                    if existing and existing["wxid"] and wxid and existing["wxid"] != wxid:
                        raise MemberError(f"{name} 的配置 ID 与已保存绑定不同，请先核对")
                    db.execute("""INSERT INTO people(account,room,person_key,name,display_name,wxid) VALUES(?,?,?,?,?,?)
                        ON CONFLICT(account,room,person_key) DO UPDATE SET
                            name=CASE WHEN people.self_named=0 THEN excluded.name ELSE people.name END,
                            display_name=CASE WHEN people.self_named=0 THEN excluded.display_name ELSE people.display_name END,
                            wxid=COALESCE(people.wxid,excluded.wxid)""", (self.account, room, key, name, display, wxid))
                    for alias in aliases:
                        old = db.execute("SELECT person_key FROM aliases WHERE account=? AND room=? AND alias=?",
                                         (self.account, room, alias)).fetchone()
                        if old and old["person_key"] != key:
                            raise MemberError(f"称呼 {alias} 已属于另一位成员")
                        db.execute("INSERT OR IGNORE INTO aliases VALUES(?,?,?,?)", (self.account, room, alias, key))
            self._db = db
            return db
        except BaseException:
            db.close()
            raise

    def initialize(self):
        with self._lock:
            self._connection()
            self.rollover()

    def _day_bounds(self):
        local = self._now().astimezone(BEIJING)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return (start.date().isoformat(), start.astimezone(timezone.utc).isoformat(),
                (start + timedelta(days=1)).astimezone(timezone.utc).isoformat())

    def rollover(self):
        """即使没有新消息也可调用；停机期间跨天则在启动时补清理。"""
        with self._lock:
            day, start, _ = self._day_bounds()
            if day == self._active_day:
                return False
            with self._connection() as db:
                for room in self.groups:
                    db.execute("""INSERT INTO chat_buffers(account,room,local_day) VALUES(?,?,?)
                        ON CONFLICT(account,room) DO UPDATE SET local_day=excluded.local_day,
                        generation=chat_buffers.generation+? WHERE chat_buffers.local_day<>excluded.local_day""",
                               (self.account, room, day, int(self.daily_clear)))
                    if self.daily_clear:
                        db.execute("DELETE FROM messages WHERE account=? AND room=? AND received_at<?",
                                   (self.account, room, start))
            self._active_day = day
            return True

    def clear(self, room):
        if room not in self.groups:
            raise MemberError("这个群尚未启用聊天记录")
        with self._lock:
            self.rollover()
            with self._connection() as db:
                count = db.execute("DELETE FROM messages WHERE account=? AND room=?", (self.account, room)).rowcount
                db.execute("UPDATE chat_buffers SET generation=generation+1 WHERE account=? AND room=?",
                           (self.account, room))
            return count

    def today_snapshot(self, name, room):
        """完整读取当天的成员文本，数量不受控制台 history 的条数限制。"""
        with self._lock:
            self.rollover()
            member = self.resolve(name, room)
            if member.wxid is None:
                raise MemberError(f"{member.name} 尚未绑定微信 ID")
            day, start, end = self._day_bounds()
            db = self._connection()
            state = db.execute("SELECT generation FROM chat_buffers WHERE account=? AND room=?",
                               (self.account, room)).fetchone()
            if state is None:
                raise MemberError("这个群尚未启用聊天记录")
            rows = db.execute("""SELECT * FROM messages WHERE account=? AND room=? AND sender_id=?
                AND received_at>=? AND received_at<? AND bot_generated=0 ORDER BY id""",
                              (self.account, room, member.wxid, start, end)).fetchall()
            return ChatSnapshot(member, day, state[0], tuple(dict(row) for row in rows))

    def today_group_snapshot(self, room):
        """完整读取本群今天所有成员的文本，未绑定者也保留身份区分。"""
        if room not in self.groups:
            raise MemberError("这个群尚未启用聊天记录")
        with self._lock:
            self.rollover()
            day, start, end = self._day_bounds()
            db = self._connection()
            state = db.execute("SELECT generation FROM chat_buffers WHERE account=? AND room=?",
                               (self.account, room)).fetchone()
            rows = db.execute("""SELECT m.*, p.name AS speaker_name FROM messages m
                LEFT JOIN people p ON p.account=m.account AND p.room=m.room AND p.wxid=m.sender_id
                WHERE m.account=? AND m.room=? AND m.received_at>=? AND m.received_at<?
                AND m.bot_generated=0 ORDER BY m.id""", (self.account, room, start, end)).fetchall()
            speakers, messages = {}, []
            for row in rows:
                item = dict(row)
                number = speakers.setdefault(item["sender_id"], len(speakers) + 1)
                # 发给模型的稳定编号替代真实微信 ID；昵称只用于可读性。
                item["speaker"] = f"P{number}"
                item["speaker_name"] = item["speaker_name"] or self.wechat_name(item["sender_id"]) or f"未绑定成员{number}"
                messages.append(item)
            return GroupChatSnapshot(room, day, state[0], tuple(messages))

    def snapshot_is_current(self, snapshot):
        with self._lock:
            self.rollover()
            state = self._connection().execute("SELECT local_day,generation FROM chat_buffers WHERE account=? AND room=?",
                                               (self.account, snapshot.room)).fetchone()
            return bool(state and (not self.daily_clear or state[0] == snapshot.day)
                        and state[1] == snapshot.generation)

    def personality(self, room):
        from .personality import PersonalityState
        return PersonalityState(room)

    def set_personality(self, room, name):
        raise MemberError("已固定为熟人损友风格，不再切换人格")

    def personality_is_current(self, state):
        return self.personality(state.room) == state

    @contextmanager
    def personality_delivery_guard(self, state):
        from .personality import PersonalityExpired
        with self._lock:
            if not self.personality_is_current(state):
                raise PersonalityExpired("人格已切换")
            yield

    def conversation_snapshot(self, message):
        """以当前消息为锚点，读取跨天的本人 10 条与全群 50 条。"""
        if message.chat_id not in self.groups:
            raise MemberError("这个群尚未启用聊天记录")
        with self._lock:
            self.rollover()
            day = self._day_bounds()[0]
            db = self._connection()
            anchor = self.message_id(message)
            state = db.execute("SELECT generation,context_floor FROM chat_buffers WHERE account=? AND room=?",
                               (self.account, message.chat_id)).fetchone()
            if anchor is None or anchor <= state[1]:
                raise SnapshotExpired("当前消息已被重置")
            previous = self.recent_rows(message.chat_id, limit=10, sender_id=message.sender_id,
                                        before_id=anchor, context=True)
            group = self.recent_rows(message.chat_id, limit=50, before_id=anchor, context=True)
            previous_ids = {row["id"] for row in previous}
            group = self.label_speakers([row for row in group if row["id"] not in previous_ids], message.chat_id)
            member = self.find_by_wxid(message.sender_id, message.chat_id)
            return ConversationSnapshot(message.chat_id, day, state[0], member.name if member else
                                        self.wechat_name(message.sender_id) or "群友",
                                        tuple(previous), message.text, tuple(group), anchor)

    @contextmanager
    def delivery_guard(self, snapshot):
        # 仅原生发送期间持锁，随机等待和大模型调用不会阻塞 /清空。
        with self._lock:
            if not self.snapshot_is_current(snapshot):
                raise SnapshotExpired("聊天记录已清空或已跨天")
            yield

    def _member(self, row):
        if row["person_key"] is None:
            return Member(row["room"], row["name"], row["name"], row["wxid"], (), False)
        aliases = self._connection().execute(
            "SELECT alias FROM aliases WHERE account=? AND room=? AND person_key=? ORDER BY alias",
            (self.account, row["room"], row["person_key"])).fetchall()
        return Member(row["room"], row["name"], row["display_name"], row["wxid"], tuple(r[0] for r in aliases),
                      bool(row["self_named"]))

    def remember_wechat_name(self, wxid, name):
        """微信原始名字按账号 ID 缓存，不产生任何群内绑定或别名。"""
        validate_account(wxid)
        normalized = name_key(name)
        if any(ord(ch) < 32 for ch in name):
            raise MemberError("微信名不能包含控制字符")
        with self._lock, self._connection() as db:
            db.execute("""INSERT INTO wechat_names VALUES(?,?,?,?) ON CONFLICT(account,wxid)
                DO UPDATE SET name=excluded.name,name_key=excluded.name_key""",
                (self.account, wxid, name.strip(), normalized))

    def wechat_name(self, wxid):
        with self._lock:
            row = self._connection().execute("SELECT name FROM wechat_names WHERE account=? AND wxid=?",
                                             (self.account, wxid)).fetchone()
            return row[0] if row else None

    def display_name(self, wxid, room):
        """群内绑定优先；其他群的绑定永远不作为默认名字。"""
        member = self.find_by_wxid(wxid, room)
        return member.display_name if member else self.wechat_name(wxid)

    def _row(self, name, room=None):
        sql = """SELECT p.* FROM people p JOIN aliases a USING(account,room,person_key)
                 WHERE a.account=? AND a.alias=?"""
        values = [self.account, name_key(name)]
        if room is not None:
            sql += " AND a.room=?"
            values.append(room)
        rows = self._connection().execute(sql, values).fetchall()
        if not rows:
            # 未绑定也可按已核对的微信名查询，但必须确实在这个群说过话。
            query = """SELECT DISTINCT m.room,n.name,n.wxid FROM wechat_names n JOIN room_members m
                ON m.account=n.account AND m.wxid=n.wxid WHERE n.account=? AND n.name_key=?"""
            parameters = [self.account, name_key(name)]
            if room is not None:
                query += " AND m.room=?"
                parameters.append(room)
            names = self._connection().execute(query, parameters).fetchall()
            if len(names) > 1:
                raise MemberError(f"微信名 {name} 对应多个成员或群，请用本群绑定称呼查询")
            if not names:
                raise MemberError(f"未登记称呼：{name}")
            bound = self._connection().execute("SELECT * FROM people WHERE account=? AND room=? AND wxid=?",
                (self.account, names[0]["room"], names[0]["wxid"])).fetchone()
            if bound:
                return bound
            return {"room": names[0]["room"], "name": names[0]["name"], "wxid": names[0]["wxid"],
                    "person_key": None, "display_name": names[0]["name"], "self_named": 0}
        if len(rows) != 1:
            raise MemberError(f"多个群都有 {name}，请指定群 ID")
        return rows[0]

    def resolve(self, name, room=None):
        with self._lock:
            return self._member(self._row(name, room))

    def list_members(self, room=None):
        with self._lock:
            sql, values = "SELECT * FROM people WHERE account=?", [self.account]
            if room is not None:
                sql += " AND room=?"
                values.append(room)
            return [self._member(row) for row in self._connection().execute(sql + " ORDER BY room,person_key", values).fetchall()]

    def find_by_wxid(self, wxid, room):
        with self._lock:
            row = self._connection().execute("SELECT * FROM people WHERE account=? AND room=? AND wxid=?",
                                             (self.account, room, wxid)).fetchone()
            return self._member(row) if row else None

    def bind(self, name, room, wxid):
        validate_mentions(room, [wxid])
        with self._lock:
            row = self._row(name, room)
            if row["person_key"] is None:
                raise MemberError("请本人发送 /绑定 昵称 来设置本群称呼")
            if row["wxid"] and row["wxid"] != wxid:
                raise MemberError(f"{row['name']} 已绑定其他账号，未修改原绑定")
            other = self._connection().execute("SELECT name FROM people WHERE account=? AND room=? AND wxid=?",
                                                (self.account, room, wxid)).fetchone()
            if other and other["name"] != row["name"]:
                raise MemberError(f"这个微信账号已绑定为 {other['name']}，未修改原绑定")
            with self._connection() as db:
                db.execute("UPDATE people SET wxid=? WHERE account=? AND room=? AND person_key=?",
                           (wxid, self.account, room, row["person_key"]))
            return self._member(self._row(name, room))

    def register(self, name, room, wxid):
        """本人注册新称呼或改称呼；身份、旧称呼和记录仍由同一个 ID 关联。"""
        alias = name_key(name)
        name = name.strip()
        validate_mentions(room, [wxid])
        if room not in self.groups:
            raise MemberError("这个群尚未启用成员目录")
        with self._lock:
            db = self._connection()
            by_name = db.execute("""SELECT p.* FROM people p JOIN aliases a USING(account,room,person_key)
                WHERE a.account=? AND a.room=? AND a.alias=?""", (self.account, room, alias)).fetchone()
            by_id = db.execute("SELECT * FROM people WHERE account=? AND room=? AND wxid=?",
                               (self.account, room, wxid)).fetchone()
            if by_name and by_name["wxid"] and by_name["wxid"] != wxid:
                raise MemberError(f"称呼 {name} 已被其他成员绑定，请换一个昵称")
            if by_name and by_id and by_name["person_key"] != by_id["person_key"]:
                raise MemberError(f"称呼 {name} 已属于另一份成员资料，请换一个昵称")
            row = by_id or by_name
            key = row["person_key"] if row else "member_" + uuid.uuid4().hex
            display = name  # 本人 /绑定 的名字同时用于本群查询和艾特显示。
            with db:
                if row:
                    db.execute("UPDATE people SET name=?,display_name=?,wxid=?,self_named=1 WHERE account=? AND room=? AND person_key=?",
                               (name, display, wxid, self.account, room, key))
                else:
                    db.execute("""INSERT INTO people(account,room,person_key,name,display_name,wxid,self_named)
                        VALUES(?,?,?,?,?,?,1)""", (self.account, room, key, name, name, wxid))
                db.execute("INSERT OR IGNORE INTO aliases VALUES(?,?,?,?)", (self.account, room, alias, key))
            return self._member(self._row(name, room))

    def record(self, message):
        if not self.history_enabled or message.type != 1 or not message.is_group or message.chat_id not in self.groups:
            return False
        if message.data.get("self_wxid", self.account) != self.account:
            return False
        validate_mentions(message.chat_id, [message.sender_id])
        if not isinstance(message.text, str) or not message.session_id:
            raise MemberError("群记录缺少正文或连接标识")
        native_name = message.data.get("sender_wechat_name")
        if native_name:
            self.remember_wechat_name(message.sender_id, native_name)
        with self._lock, self._connection() as db:
            self.rollover()
            db.execute("INSERT OR IGNORE INTO room_members(account,room,wxid) VALUES(?,?,?)",
                       (self.account, message.chat_id, message.sender_id))
            now = self._now().astimezone(timezone.utc).isoformat()
            db.execute("""UPDATE room_members SET first_seen=COALESCE(first_seen,?),last_seen=?
                WHERE account=? AND room=? AND wxid=?""",
                       (now, now, self.account, message.chat_id, message.sender_id))
            result = db.execute("""INSERT OR IGNORE INTO messages
                (account,room,sender_id,text,received_at,session_id,sequence,is_self,bot_generated) VALUES(?,?,?,?,?,?,?,?,?)""",
                (self.account, message.chat_id, message.sender_id, message.text,
                 self._now().astimezone(timezone.utc).isoformat(), message.session_id, message.sequence,
                 int(message.is_self), int(bool(message.data.get("bot_generated")))))
            return result.rowcount > 0

    def is_admin(self, wxid):
        from .authorization import LOCAL_EDITOR
        return wxid is LOCAL_EDITOR or bool(self.owner_wxid and wxid == self.owner_wxid)

    def message_id(self, message):
        with self._lock:
            row = self._connection().execute("""SELECT id FROM messages WHERE account=? AND room=?
                AND sender_id=? AND session_id=? AND sequence=? AND text=? ORDER BY id DESC LIMIT 1""",
                (self.account, message.chat_id, message.sender_id, message.session_id,
                 message.sequence, message.text)).fetchone()
            return row[0] if row else None

    def latest_id(self, room):
        with self._lock:
            return self._connection().execute("SELECT COALESCE(MAX(id),0) FROM messages WHERE account=? AND room=?",
                                               (self.account, room)).fetchone()[0]

    def recent_rows(self, room, *, limit=200, sender_id=None, before_id=None, since=None, until=None, context=False):
        if room not in self.groups or type(limit) is not int or not 1 <= limit <= 5000:
            raise MemberError("查询需要已启用的群，条数应为 1—5000")
        with self._lock:
            self.rollover()
            sql = "SELECT * FROM messages WHERE account=? AND room=? AND bot_generated=0"
            values = [self.account, room]
            for clause, value in (("sender_id=?", sender_id), ("id<?", before_id),
                                  ("received_at>=?", since), ("received_at<?", until)):
                if value is not None:
                    sql += " AND " + clause
                    values.append(value)
            if context:
                floor = self._connection().execute("SELECT context_floor FROM chat_buffers WHERE account=? AND room=?",
                                                   (self.account, room)).fetchone()[0]
                sql += " AND id>?"
                values.append(floor)
            rows = []
            for row in self._connection().execute(sql + " ORDER BY id DESC", values):
                if effective_text(row["text"]):
                    rows.append(dict(row))
                    if len(rows) >= limit:
                        break
            return list(reversed(rows))

    def label_speakers(self, rows, room):
        speakers, result = {}, []
        for row in rows:
            item = dict(row)
            number = speakers.setdefault(item["sender_id"], len(speakers) + 1)
            item.update(speaker=f"P{number}", speaker_name=self.display_name(item["sender_id"], room)
                        or f"未绑定成员{number}")
            result.append(item)
        return result

    def recent_snapshot(self, room, *, name=None, limit=200, today=False, before_id=None, context=False):
        with self._lock:
            self.rollover()
            member = self.resolve(name, room) if name is not None else None
            if member and not member.wxid:
                raise MemberError("该成员尚未绑定微信 ID")
            day, start, _ = self._day_bounds()
            rows = self.recent_rows(room, limit=limit, sender_id=member.wxid if member else None,
                                    before_id=before_id, since=start if today else None, context=context)
            state = self._connection().execute("SELECT generation FROM chat_buffers WHERE account=? AND room=?",
                                               (self.account, room)).fetchone()
            scope = "today" if today else "recent"
            if member:
                return ChatSnapshot(member, day, state[0], tuple(rows), scope, before_id or 0)
            return GroupChatSnapshot(room, day, state[0], tuple(self.label_speakers(rows, room)), scope, before_id or 0)

    def reset_context(self, room):
        if room not in self.groups:
            raise MemberError("这个群尚未启用聊天记录")
        with self._lock:
            self.rollover()
            with self._connection() as db:
                db.execute("UPDATE chat_buffers SET generation=generation+1,context_floor=? WHERE account=? AND room=?",
                           (self.latest_id(room), self.account, room))

    def binding_reminder_due(self, room, wxid):
        if self.find_by_wxid(wxid, room) is not None:
            return False
        with self._lock, self._connection() as db:
            now = self._now().astimezone(timezone.utc)
            row = db.execute("SELECT reminded_at FROM binding_reminders WHERE account=? AND room=? AND wxid=?",
                             (self.account, room, wxid)).fetchone()
            if row and now - datetime.fromisoformat(row[0]) < timedelta(days=7):
                return False
            db.execute("INSERT OR REPLACE INTO binding_reminders VALUES(?,?,?,?)",
                       (self.account, room, wxid, now.isoformat()))
            return True

    def history(self, name, room=None, *, limit=100, since=None):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise MemberError("历史查询 limit 应为 1–1000 的整数")
        if since is not None:
            if not isinstance(since, datetime) or since.tzinfo is None or since.utcoffset() is None:
                raise MemberError("since 应为带时区的 datetime")
            since = since.astimezone(timezone.utc).isoformat()
        with self._lock:
            self.rollover()
            member = self.resolve(name, room)
            if member.wxid is None:
                raise MemberError(f"{member.name} 尚未绑定微信 ID")
            sql = "SELECT * FROM messages WHERE account=? AND room=? AND sender_id=?"
            values = [self.account, member.room, member.wxid]
            if since is not None:
                sql += " AND received_at>=?"
                values.append(since)
            rows = self._connection().execute(sql + " ORDER BY id DESC LIMIT ?", [*values, limit]).fetchall()
            return [dict(row) for row in reversed(rows)]

    def close(self):
        with self._lock:
            if self._db:
                self._db.close()
                self._db = None
            self._closed = True
