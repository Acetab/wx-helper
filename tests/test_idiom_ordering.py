"""真实分发队列的轮次边界测试：阻塞判定/发送，接收时间全部相同。"""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from plugins.idiom_chain.judge import Verdict
from wxbot_framework.backend import CAPABILITIES
from wxbot_framework.config import ConfigStore
from wxbot_framework.members import MemberStore
from wxbot_framework.ordering import MessageWatermarks
from wxbot_framework.plugins import PluginManager
from wxbot_framework.room_state import RoomStateStore
from wxbot_framework.runtime import BotApplication
from wxbot_framework.sdk import Message, ServiceFacade

ROOT = Path(__file__).resolve().parent.parent
ROOM, OTHER = "111@chatroom", "222@chatroom"
ROBOT, PLAYER = "wxid_order_robot", "wxid_order_player"


def wait_for(condition):
    # 仅测试线程同步用超时，不用时间精度判断消息轮次。
    deadline = time.monotonic() + 5
    while not condition():
        if time.monotonic() >= deadline:
            raise AssertionError("插件未完成预期处理")
        threading.Event().wait(0.01)


class Backend:
    session_id = "one"

    def __init__(self):
        self.before_send = lambda text: None
        self.sent = []

    def send_text(self, room, text):
        self.before_send(text)
        self.sent.append((room, text))
        return True

    def send_at_text(self, room, text, members):
        return self.send_text(room, text)

    def stop(self):
        return True


class OrderingTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / ".runtime/tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        path = self.root / "members.json"
        path.write_text(json.dumps({"database_file": "members.sqlite3", "history_enabled": True,
                                   "groups": {ROOM: [], OTHER: []}}), encoding="utf-8")
        self.members = MemberStore(path, ROBOT)
        self.members.initialize()
        self.addCleanup(self.members.close)
        self.members.register("玩家", ROOM, PLAYER)
        config = json.loads((ROOT / "bot_config.example.json").read_text(encoding="utf-8"))
        for entry in config["plugins"].values():
            entry["enabled"] = False
        config["plugins"]["idiom_chain"]["enabled"] = True
        config["text_send_delay"] = {"enabled": False}
        path = self.root / "bot.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        self.backend = Backend()
        self.service = ServiceFacade(text_delay={"enabled": False})
        self.service.bind(self.backend)
        self.addCleanup(self.service.close)
        capabilities = CAPABILITIES | {"group.text.receive", "group.text.send", "group.text.mention"}
        self.manager = PluginManager(ROOT / "plugins", ConfigStore(path), self.service, capabilities,
                                     member_store=self.members, room_state_store=RoomStateStore(self.members))
        self.addCleanup(self.manager.shutdown)
        self.manager.load_one("idiom_chain")
        self.record = self.manager.records["idiom_chain"]
        self.record.context._llm = Mock(complete_json=Mock(side_effect=AssertionError("不得请求实际模型")))
        def choose(words, used, minimum):
            return next((word for word in ("一心一意", "发扬光大", "心口如一") if word in words), words[0])
        self.record.instance.judge.lexicon._choose_sustainable = choose
        self.app = object.__new__(BotApplication)
        self.app.config = {"chat_allowlist": []}
        self.app.members, self.app.plugins = self.members, self.manager
        self.sequence = 0
        self.releases = []
        self.receivers = []

    def tearDown(self):
        for release in self.releases:
            release.set()
        for receiver in self.receivers:
            receiver.join(timeout=5)

    def emit(self, text, *, session="one", sequence=None):
        self.sequence += 1
        self.app._emit(Message(text, ROOM, sender_id=PLAYER, is_group=True,
                               session_id=session, sequence=sequence or self.sequence, received_at=100))

    def rounds(self):
        game = self.record.instance.games.get(ROOM)
        return game.rounds if game else None

    def idle(self):
        wait_for(lambda: self.record.queue.unfinished_tasks == 0)
        self.assertEqual(self.record.errors, 0)

    def start(self):
        self.emit("成语接龙")
        self.idle()
        self.assertEqual(self.rounds(), 0)

    def test_pending_during_judgment_all_drop_and_immediate_next_answer_counts(self):
        self.start()
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def evaluate(*args):
            entered.set()
            if not release.wait(5):
                raise AssertionError("判定测试同步超时")
            return Verdict(True, "有效", "发扬光大")
        judge = self.record.instance._handle.__globals__["IdiomJudge"]
        with patch.object(judge, "evaluate", side_effect=evaluate):
            self.emit("意气风发")
            self.assertTrue(entered.wait(5))
            for _ in range(5):
                self.emit("大快人心")
            release.set()
            self.idle()
        self.assertEqual(self.rounds(), 1)
        self.assertEqual(len(self.backend.sent), 2)  # 开场、第一轮；五条旧消息均无回复/加分。
        self.emit("大快人心")
        self.idle()
        self.assertEqual(self.rounds(), 2)

    def test_capture_precedes_blocked_database_write_during_send(self):
        self.start()
        entered, release, captured = threading.Event(), threading.Event(), threading.Event()
        self.releases.append(release)
        def before_send(text):
            if "我接：发扬光大" in text:
                entered.set()
                if not release.wait(5):
                    raise AssertionError("发送测试同步超时")
        self.backend.before_send = before_send
        self.emit("意气风发")
        self.assertTrue(entered.wait(5))
        original = self.manager.capture
        def capture(message):
            result = original(message)
            if message.text == "大快人心":
                captured.set()
            return result
        errors = []
        def receive():
            try:
                self.emit("大快人心")
            except BaseException as error:
                errors.append(error)
        with patch.object(self.manager, "capture", side_effect=capture):
            receiver = threading.Thread(target=receive)
            self.receivers.append(receiver)
            receiver.start()
            self.assertTrue(captured.wait(5))
            release.set()
            receiver.join(timeout=5)
            self.assertFalse(receiver.is_alive())
            self.idle()
        self.assertEqual(errors, [])
        self.assertEqual(self.rounds(), 1)
        self.emit("大快人心")
        self.idle()
        self.assertEqual(self.rounds(), 2)

    def test_watermark_survives_native_sequence_restart_and_plugin_reload(self):
        self.emit("成语接龙", session="one", sequence=900)
        self.idle()
        self.backend.session_id = "two"
        self.emit("意气风发", session="two", sequence=1)
        self.idle()
        self.assertEqual(self.rounds(), 1)
        self.manager.reload("idiom_chain")
        self.record = self.manager.records["idiom_chain"]
        self.record.context._llm = Mock()
        self.emit("大快人心", session="two", sequence=2)
        self.idle()
        self.assertEqual(self.rounds(), 2)

    def test_capture_is_idempotent_and_watermark_is_per_room(self):
        tracker = MessageWatermarks()
        first = tracker.capture(Message("一", ROOM, sequence=900, received_at=100))
        self.assertIs(tracker.capture(first), first)
        second = tracker.capture(Message("二", OTHER, sequence=1, session_id="two", received_at=100))
        self.assertEqual(tracker.latest(ROOM), first.dispatch_sequence)
        self.assertEqual(tracker.latest(OTHER), second.dispatch_sequence)
        self.assertGreater(second.dispatch_sequence, first.dispatch_sequence)
        restarted = MessageWatermarks()
        recaptured = restarted.capture(second)
        self.assertEqual(recaptured.dispatch_sequence, 1)
        self.assertNotEqual(recaptured.dispatch_epoch, second.dispatch_epoch)


if __name__ == "__main__":
    unittest.main()
