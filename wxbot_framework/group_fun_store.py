"""群聊趣味功能的数据层：原话查询、出题偏好及有出处的群梗修订。"""
from __future__ import annotations

import json
import re
from datetime import timezone

from .commands import effective_text
from .members import MemberError


def short_text(value, limit):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit or
            any(ord(char) < 32 for char in value)):
        raise MemberError(f"内容应为 1–{limit} 字的单行文字")
    return value.strip()


def source_ids(value):
    parts = re.split(r"[,，\s]+", value.strip())
    if not 1 <= len(parts) <= 5 or any(not re.fullmatch(r"#?[1-9][0-9]{0,14}", part) for part in parts):
        raise MemberError("请填写 1–5 个原话编号，用逗号分隔，例如 #12,#18")
    return list(dict.fromkeys(int(part.lstrip("#")) for part in parts))


class GroupFunStore:
    def __init__(self, members):
        self.members = members
        self.account = members.account

    def initialize(self):
        with self.members._lock, self.members._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS quote_preferences (
                    account TEXT NOT NULL, room TEXT NOT NULL, sender TEXT NOT NULL,
                    excluded INTEGER NOT NULL, PRIMARY KEY(account,room,sender));
                CREATE TABLE IF NOT EXISTS group_memes (
                    id INTEGER PRIMARY KEY, account TEXT NOT NULL, room TEXT NOT NULL,
                    name TEXT NOT NULL, creator TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(account,room,name));
                CREATE TABLE IF NOT EXISTS group_meme_revisions (
                    id INTEGER PRIMARY KEY, meme_id INTEGER NOT NULL, editor TEXT NOT NULL,
                    kind TEXT NOT NULL, description TEXT NOT NULL, sources TEXT NOT NULL,
                    created_at TEXT NOT NULL);
            """)

    def _room(self, room):
        if room not in self.members.groups:
            raise MemberError("这个群尚未启用聊天记录")

    def excluded(self, room):
        self._room(room)
        with self.members._lock:
            return {row[0] for row in self.members._connection().execute(
                "SELECT sender FROM quote_preferences WHERE account=? AND room=? AND excluded=1",
                (self.account, room))}

    def set_excluded(self, room, sender, excluded):
        self._room(room)
        with self.members._lock, self.members._connection() as db:
            db.execute("""INSERT INTO quote_preferences VALUES(?,?,?,?)
                ON CONFLICT(account,room,sender) DO UPDATE SET excluded=excluded.excluded""",
                (self.account, room, sender, int(excluded)))

    def rows(self, room, ids, *, before_id=None):
        self._room(room)
        if not ids:
            return []
        if len(ids) > 100 or any(type(number) is not int or number <= 0 for number in ids):
            raise MemberError("原话编号不正确")
        placeholders = ",".join("?" for _ in ids)
        sql = f"SELECT * FROM messages WHERE account=? AND room=? AND bot_generated=0 AND id IN ({placeholders})"
        values = [self.account, room, *ids]
        if before_id is not None:
            sql += " AND id<?"
            values.append(before_id)
        with self.members._lock:
            rows = [dict(row) for row in self.members._connection().execute(sql + " ORDER BY id", values)
                    if effective_text(row["text"])]
            return self.members.label_speakers(rows, room)

    def search(self, room, term, *, before_id, limit=12, oldest=False):
        self._room(room)
        term = short_text(term, 40)
        order = "ASC" if oldest else "DESC"
        # instr 按字面匹配，% 与 _ 不扩展为通配符。
        sql = f"""SELECT * FROM messages WHERE account=? AND room=? AND bot_generated=0
            AND id<? AND instr(text,?)>0 ORDER BY id {order}"""
        with self.members._lock:
            result = []
            for row in self.members._connection().execute(sql, (self.account, room, before_id, term)):
                if effective_text(row["text"]):
                    result.append(dict(row))
                    if len(result) >= limit:
                        break
            return self.members.label_speakers(result if oldest else list(reversed(result)), room)

    def context(self, room, number, *, before_id, excluded=()):
        self._room(room)
        blocked = set(excluded)
        with self.members._lock:
            db = self.members._connection()
            values = (self.account, room, number, before_id)
            previous = db.execute("""SELECT * FROM messages WHERE account=? AND room=? AND id<? AND id<?
                AND bot_generated=0 ORDER BY id DESC LIMIT 5""", values).fetchall()
            following = db.execute("""SELECT * FROM messages WHERE account=? AND room=? AND id>? AND id<?
                AND bot_generated=0 ORDER BY id LIMIT 5""", values).fetchall()
            earlier = [dict(row) for row in previous if effective_text(row["text"]) and row["sender_id"] not in blocked][:1]
            later = [dict(row) for row in following if effective_text(row["text"]) and row["sender_id"] not in blocked][:1]
            return self.members.label_speakers(earlier + later, room)

    def memes(self, room, term=""):
        self._room(room)
        with self.members._lock:
            return [dict(row) for row in self.members._connection().execute("""SELECT * FROM group_memes
                WHERE account=? AND room=? AND (?='' OR instr(name,?)>0) ORDER BY id DESC LIMIT 100""",
                (self.account, room, term, term))]

    def meme(self, room, number):
        self._room(room)
        with self.members._lock:
            db = self.members._connection()
            row = db.execute("SELECT * FROM group_memes WHERE account=? AND room=? AND id=?",
                             (self.account, room, number)).fetchone()
            if row is None:
                raise MemberError("本群没有这个群梗编号")
            revisions = [dict(item) for item in db.execute(
                "SELECT * FROM group_meme_revisions WHERE meme_id=? ORDER BY id", (number,))]
            for item in revisions:
                item["sources"] = json.loads(item["sources"])
            return dict(row), revisions

    def revise(self, room, sender, name_or_id, description, ids, *, before_id, kind="收录"):
        description = short_text(description, 200)
        with self.members._lock, self.members._connection() as db:
            evidence = self.rows(room, ids, before_id=before_id)
            if len(evidence) != len(ids):
                raise MemberError("部分原话不存在、属于其他群或不是有效聊天，请先用 /原话 核对")
            now = self.members._now().astimezone(timezone.utc).isoformat()
            if kind == "收录":
                name = short_text(name_or_id, 40)
                if not any(name in row["text"] for row in evidence):
                    raise MemberError("至少一条出处原文需要包含这个群梗名称")
                if db.execute("SELECT 1 FROM group_memes WHERE account=? AND room=? AND name=?",
                              (self.account, room, name)).fetchone():
                    raise MemberError("这个群梗已经收录，请查询编号后补充或修订")
                number = db.execute("INSERT INTO group_memes(account,room,name,creator,created_at) VALUES(?,?,?,?,?)",
                                    (self.account, room, name, sender, now)).lastrowid
            else:
                meme, _ = self.meme(room, name_or_id)
                if kind == "修订" and sender != meme["creator"] and not self.members.is_admin(sender):
                    raise MemberError("只有收录者或管理员可以修订正文；其他群友可以 /群梗补充")
                number = meme["id"]
            db.execute("""INSERT INTO group_meme_revisions(meme_id,editor,kind,description,sources,created_at)
                VALUES(?,?,?,?,?,?)""", (number, sender, kind, description, json.dumps(ids), now))
            return number
