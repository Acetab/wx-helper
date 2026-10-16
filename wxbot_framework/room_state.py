"""按群共享的插件状态；写进成员库，重启后仍能读回。

框架只负责一致读写，不解释内容：插件自己决定字段，别的插件按同一
（群，功能名）读回，用于跨插件协同（例如接龙进行中的人格对话）。
"""
from __future__ import annotations

import json
import re
from datetime import timezone

from .members import MemberError

FEATURE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
PAYLOAD_LIMIT = 20000


class RoomStateError(MemberError):
    """状态数据不合法或读写失败。"""


class RoomStateStore:
    """每群一份 JSON 状态，多个插件共用同一份。"""

    def __init__(self, members):
        self.members = members
        self._ready = False

    def initialize(self):
        with self.members._lock, self.members._connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS room_states (
                account TEXT NOT NULL, room TEXT NOT NULL, feature TEXT NOT NULL,
                payload TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(account, room, feature))""")
        self._ready = True

    def _connection(self):
        if not self._ready:
            self.initialize()
        return self.members._connection()

    def _prepare(self, room, feature):
        if not isinstance(feature, str) or not FEATURE.fullmatch(feature):
            raise RoomStateError("功能名应为小写字母、数字或下划线，最多 32 字符")
        if not isinstance(room, str) or not room.strip() or len(room) > 127 or "\0" in room:
            raise RoomStateError("群 ID 不合法")
        return room

    def get(self, room, feature):
        """读回状态；没有、被清空或内容已损坏时返回 None。"""
        self._prepare(room, feature)
        with self.members._lock, self._connection() as db:
            row = db.execute("""SELECT payload FROM room_states
                WHERE account=? AND room=? AND feature=?""",
                (self.members.account, room, feature)).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload"])
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    def put(self, room, feature, payload):
        """整体覆盖写入，并记录写入时间。"""
        if not isinstance(payload, dict):
            raise RoomStateError("状态内容应为 JSON 对象")
        text = json.dumps(payload, ensure_ascii=False)
        if len(text) > PAYLOAD_LIMIT:
            raise RoomStateError("状态内容过大")
        self._prepare(room, feature)
        now = self.members._now().astimezone(timezone.utc).isoformat()
        with self.members._lock, self._connection() as db:
            db.execute("""INSERT INTO room_states(account,room,feature,payload,updated_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(account,room,feature) DO UPDATE SET
                payload=excluded.payload, updated_at=excluded.updated_at""",
                (self.members.account, room, feature, text, now))

    def snapshot(self, room):
        """分发消息前读出本群各功能状态，避免插件按不同进度处理同一条消息。"""
        self._prepare(room, "snapshot")
        with self.members._lock, self._connection() as db:
            rows = db.execute("SELECT feature,payload FROM room_states WHERE account=? AND room=?",
                              (self.members.account, room)).fetchall()
        states = {}
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except ValueError:
                continue
            if isinstance(payload, dict):
                states[row["feature"]] = payload
        return states

    def clear(self, room, feature):
        self._prepare(room, feature)
        with self.members._lock, self._connection() as db:
            db.execute("DELETE FROM room_states WHERE account=? AND room=? AND feature=?",
                       (self.members.account, room, feature))
