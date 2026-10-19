"""框架接收水位：用整数记录消息顺序，不依赖时钟、数据库 ID 或桥接重连计数。"""
from dataclasses import replace
import threading
import uuid


class MessageWatermarks:
    def __init__(self):
        self.epoch = uuid.uuid4().hex
        self._lock = threading.Lock()
        self._sequence = 0
        self._rooms = {}

    def capture(self, message):
        """在慢速落库/插件排队之前调用；dispatch 的二次调用不重新编号。"""
        if message.dispatch_sequence and message.dispatch_epoch == self.epoch:
            return message
        with self._lock:
            self._sequence += 1
            self._rooms[message.chat_id] = self._sequence
            return replace(message, dispatch_sequence=self._sequence, dispatch_epoch=self.epoch)

    def latest(self, room):
        with self._lock:
            return self._rooms.get(room, 0)
