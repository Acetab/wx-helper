"""桥接残留恢复与退出保护；不调用真实微信或原生卸载。"""
import signal
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from tools.recover_bridge import cleanup_loaded
from wxbot_framework.runtime import BotApplication


class BridgeRecoveryTests(unittest.TestCase):
    def test_no_bridge_is_noop(self):
        remove = Mock()
        result = cleanup_loaded(123, (Path("example/text_bridge.dll"),), lambda _: {}, remove)
        self.assertTrue(result["clean"])
        remove.assert_not_called()

    def test_exact_owned_path_uses_guarded_remove_and_checks_absence(self):
        dll = Path("example/text_bridge.dll").resolve()
        state = {dll.name: (100, str(dll))}
        def remove(mode, **options):
            self.assertEqual(mode, "--remove")
            self.assertEqual(options["bridge_path"], dll)
            state.clear()
            return {"code": 0, "dll_unloaded": True}
        result = cleanup_loaded(123, (dll,), lambda _: dict(state), remove)
        self.assertEqual(result["removed"], ["text_bridge.dll"])
        self.assertTrue(result["clean"])

    def test_foreign_module_blocks_all_cleanup_before_first_remove(self):
        own, other = Path("example/text_bridge.dll"), Path("example/selftest_bridge.dll")
        modules = {own.name: (100, str(own.resolve())), other.name: (200, str(Path("another/selftest_bridge.dll").resolve()))}
        remove = Mock()
        with self.assertRaisesRegex(RuntimeError, "其他目录"):
            cleanup_loaded(123, (own, other), lambda _: modules, remove)
        remove.assert_not_called()

    def test_native_busy_is_preserved_and_not_retried_in_a_loop(self):
        dll = Path("example/text_bridge.dll").resolve()
        remove = Mock(return_value={"code": 170, "dll_unloaded": False})
        with self.assertRaisesRegex(RuntimeError, "返回码 170"):
            cleanup_loaded(123, (dll,), lambda _: {dll.name: (100, str(dll))}, remove)
        self.assertEqual(remove.call_count, 1)

    def test_remaining_module_is_not_reported_clean(self):
        dll = Path("example/text_bridge.dll").resolve()
        result = cleanup_loaded(123, (dll,), lambda _: {dll.name: (100, str(dll))},
                                Mock(return_value={"code": 0, "dll_unloaded": True}))
        self.assertFalse(result["clean"])


class ShutdownTests(unittest.TestCase):
    def app(self):
        app = object.__new__(BotApplication)
        app.stop_event = threading.Event()
        app.service, app.members, app.plugins = Mock(), Mock(), Mock()
        app.maintenance_router, app.hourly_scheduler, app.memory_worker = Mock(), Mock(), Mock()
        app.last_cleanup = {"dll_unloaded": True}
        app._disconnect = Mock(return_value=True)
        return app

    def test_worker_failure_does_not_skip_native_cleanup_or_other_workers(self):
        app, order = self.app(), []
        app._disconnect.side_effect = lambda: order.append("native") or True
        def fail():
            order.append("memory")
            raise RuntimeError("模型仍在退出")
        app.memory_worker.close.side_effect = fail
        with self.assertRaisesRegex(RuntimeError, "桥接已清理"):
            app.stop()
        self.assertEqual(order, ["native", "memory"])
        app.maintenance_router.close.assert_called_once()
        app.hourly_scheduler.close.assert_called_once()
        app.plugins.shutdown.assert_called_once()
        app.members.close.assert_not_called()

    def test_native_cleanup_failure_still_closes_workers_and_preserves_database(self):
        app = self.app()
        app._disconnect.return_value = False
        with self.assertRaisesRegex(RuntimeError, "recover_bridge.py"):
            app.stop()
        app.memory_worker.close.assert_called_once()
        app.plugins.shutdown.assert_called_once()
        app.members.close.assert_not_called()

    def test_repeated_ctrl_c_during_cleanup_is_guarded_and_handler_restored(self):
        app = self.app()
        previous = signal.getsignal(signal.SIGINT)
        def close():
            handler = signal.getsignal(signal.SIGINT)
            self.assertNotEqual(handler, previous)
            handler(signal.SIGINT, None)
        app.memory_worker.close.side_effect = close
        app.stop()
        self.assertEqual(signal.getsignal(signal.SIGINT), previous)
        app.members.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
