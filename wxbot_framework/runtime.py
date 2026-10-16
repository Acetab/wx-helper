"""长期运行、连接监督与本地控制台命令。"""
from __future__ import annotations

import json
import logging
import queue
import shlex
import signal
import sys
import threading
import time

from .backend import CAPABILITIES, TEXT_CAPABILITIES, NativeFileHelperBackend, NativeTextBackend
from .plugins import PluginManager
from .sdk import ServiceFacade
from .members import MemberStore
from .personality import prepare_switch
from .knowledge import KnowledgeStore
from .feature_config import feature_options
from .controls import prepare_reset
from .maintenance import MaintenanceRouter, MaintenanceService, maintenance_options
from .authorization import LOCAL_EDITOR

logger = logging.getLogger("Bot")


class BotApplication:
    def __init__(self, root, store, *, backend_factory=None):
        self.root, self.store = root, store
        self.config = store.load()
        self.service = ServiceFacade(text_delay=self.config.get("text_send_delay"))
        self.members = MemberStore.from_config(self.config, self.store.path.parent)
        self.knowledge = KnowledgeStore(self.members) if self.members is not None else None
        self.memory_options, self.hourly_options = feature_options(self.config)
        self.memory_worker = self.hourly_scheduler = None
        self.maintenance_options = maintenance_options(self.config)
        self.maintenance = (MaintenanceService(self.knowledge, app=self, options=self.maintenance_options)
                            if self.knowledge else None)
        self.maintenance_router = MaintenanceRouter(self.maintenance, self.service) if self.maintenance else None
        self.startup_notice = None
        self._background_lock = threading.RLock()
        generic = self.config.get("backend") == "native_text"
        capabilities = TEXT_CAPABILITIES if generic else CAPABILITIES
        self.plugins = PluginManager(root / "plugins", store, self.service, capabilities,
                                     self.config.get("plugin_queue_size", 128), member_store=self.members,
                                     knowledge_store=self.knowledge)
        backend_type = NativeTextBackend if generic else NativeFileHelperBackend
        self.backend_factory = backend_factory or (lambda: backend_type(root, self.config))
        self.backend = None
        self.stop_event = threading.Event()
        self.commands = queue.Queue()
        self.last_cleanup = None
        self._connection_ready = False
        self._last_connection_log = 0
        self._last_connection_error = ""

    def _log_connection_error(self, error):
        text = f"{type(error).__name__}: {error}"
        now = time.monotonic()
        if text != self._last_connection_error or now - self._last_connection_log >= 30:
            logger.warning("等待微信连接：%s", text)
            self._last_connection_error, self._last_connection_log = text, now

    def _connect(self):
        try:
            candidate = self.backend_factory()
            self.backend = candidate
            self.service.bind(candidate)
            candidate.start(self._emit)
            self._connection_ready = True
            self._last_connection_error = ""
            logger.info("微信已连接，PID=%s；收发范围：%s", candidate.pid,
                        getattr(candidate, "scope", "文本"))
            self.sync_wechat_directory()
        except Exception as error:
            self._connection_ready = False
            self._log_connection_error(error)
            self._disconnect()
        if self._connection_ready and self.startup_notice:
            # 公告失败不影响微信连接；已持久化的发送标记避免重连重复。
            self.startup_notice.send()

    def sync_wechat_directory(self, room=None):
        if not self._connection_ready or self.backend is None or self.members is None:
            return {"members": 0, "names": 0}
        reader = getattr(self.backend, "read_room_directory", None)
        if not callable(reader):
            return {"members": 0, "names": 0}
        rooms = [room] if room else sorted(self.members.groups)
        total_members, total_names = 0, 0
        for target_room in rooms:
            try:
                members, names = reader(target_room)
                if members or names:
                    self.members.sync_room_directory(target_room, members, names)
                    total_members += len(members)
                    total_names += len(names)
            except Exception:
                logger.warning("同步群成员缓存暂缓：room=%s", target_room)
        return {"members": total_members, "names": total_names}

    def _emit(self, message):
        allowed = self.config.get("chat_allowlist", [])
        router = getattr(self, "maintenance_router", None)
        # 唯一维护者私聊是明确的管理通道，不要求把其个人会话加入群收发名单。
        if (router and not message.is_group and self.members.is_admin(message.sender_id) and
                message.chat_id == message.sender_id):
            router.submit(message)
            return
        if not allowed or message.chat_id in allowed:
            if self.members is not None:
                try:
                    inserted = self.members.record(message)
                    worker = getattr(self, "memory_worker", None)
                    if inserted and worker:
                        worker.notify()
                except Exception:
                    logger.exception("群文本保存失败，消息继续分发到插件")
            if message.data.get("dispatch_to_plugins", True):
                message = prepare_reset(message, self.members)
                if router and router.submit(message):
                    return
                if self.plugins.is_active("personality"):
                    message = prepare_switch(message, self.members)
                self.plugins.dispatch(message)

    def _disconnect(self):
        self._connection_ready = False
        try:
            self.last_cleanup = self.service.close()
            self.backend = None
            return True
        except Exception as error:
            # 清理忙时保留实例，只重试清理，不覆盖 DLL、不再次注入。
            self._log_connection_error(error)
            return False

    def execute(self, command):
        words = shlex.split(command)
        if not words:
            return
        action = words[0].lower()
        if action == "manage":
            if not self.maintenance:
                raise ValueError("尚未配置成员目录与知识库")
            arguments = words[1:]
            if arguments[:1] == ["--room"]:
                arguments[0] = "--群"
            result = self.maintenance.execute(shlex.join(arguments), LOCAL_EDITOR)
            logger.info("维护结果：%s", result)
        elif action in ("quit", "exit", "stop") and len(words) == 1:
            self.stop_event.set()
        elif action == "help" and len(words) == 1:
            logger.info("命令：manage [--room 群ID] 帮助 / status / chats / members [群ID] / history 称呼 [条数] / plugins / enable 名称 / disable 名称 / reload [名称] / quit")
        elif action == "status" and len(words) == 1:
            logger.info("状态：%s", json.dumps({"connected": self._connection_ready,
                                               "pid": self.backend.pid if self.backend else None,
                                               "native": self.backend.diagnostics() if self.backend and hasattr(self.backend, "diagnostics") else {},
                                               "plugins": self.plugins.status(),
                                               "knowledge": self.knowledge.status() if self.knowledge else None}, ensure_ascii=False))
        elif action == "chats" and len(words) == 1:
            rows = self.backend.chats() if self.backend and hasattr(self.backend, "chats") else []
            logger.info("本次连接已接收的会话：%s", json.dumps(rows, ensure_ascii=False))
        elif action == "members" and len(words) in (1, 2):
            if self.members is None:
                raise ValueError("尚未配置成员目录")
            rows = [{"name": m.name, "display_name": m.display_name, "wxid": m.wxid,
                     "room": m.room, "bound": bool(m.wxid)} for m in self.members.list_members(words[1] if len(words) == 2 else None)]
            logger.info("成员目录：%s", json.dumps(rows, ensure_ascii=False))
        elif action == "history" and len(words) in (2, 3):
            if self.members is None:
                raise ValueError("尚未配置成员目录")
            rows = self.members.history(words[1], limit=int(words[2]) if len(words) == 3 else 10)
            logger.info("成员聊天记录：%s", json.dumps(rows, ensure_ascii=False))
        elif action == "plugins" and len(words) == 1:
            for row in self.plugins.status():
                state = "运行" if row["active"] else "停用"
                logger.info("%s [%s] 已处理=%s 异常=%s 丢弃=%s %s", row["name"], state,
                            row["handled"], row["errors"], row["dropped"], row["error"])
        elif action in ("enable", "disable") and len(words) == 2:
            getattr(self.plugins, action)(words[1])
        elif action == "reload" and len(words) in (1, 2):
            self.plugins.reload(words[1] if len(words) == 2 else None)
        else:
            raise ValueError("未知命令；输入 help 查看用法")

    def _read_console(self):
        # stdin 关闭时仍继续运行，适合后台启动。
        try:
            for line in sys.stdin:
                if self.stop_event.is_set():
                    break
                self.commands.put(line.strip())
        except (OSError, ValueError):
            pass

    def _start_background(self):
        if self.stop_event.is_set():
            return
        if not (self.memory_options["enabled"] or self.hourly_options["enabled"]):
            return
        if self.members is None or not self.members.history_enabled or self.members.daily_clear:
            raise ValueError("长期记忆和定时聊天需要成员记录，history_enabled=true、daily_clear=false")
        from .llm import LLMClient
        client = LLMClient.from_file(self.store.path.parent / self.config.get("llm_config_file", "llm_config.json"))
        if self.memory_options["enabled"] and not self.memory_worker:
            from .memory_worker import MemoryWorker
            self.memory_worker = MemoryWorker(self.knowledge, client, batch_size=self.memory_options["batch_size"],
                                              flush_seconds=self.memory_options["flush_seconds"])
            self.memory_worker.start()
        if self.hourly_options["enabled"] and not self.hourly_scheduler:
            from .hourly import HourlyScheduler
            self.hourly_scheduler = HourlyScheduler(self.knowledge, self.service, client,
                start_hour=self.hourly_options["start_hour"], end_hour=self.hourly_options["end_hour"],
                context_limit=self.hourly_options["context_limit"], allowlist=self.config.get("chat_allowlist", []),
                ready=lambda: self._connection_ready)
            self.hourly_scheduler.start()

    def update_feature(self, section, changes):
        with self._background_lock:
            if self.stop_event.is_set():
                raise ValueError("机器人正在退出，未修改后台设置")
            candidate = dict(self.config)
            candidate[section] = {**candidate.get(section, {}), **changes}
            self.store.validate(candidate)
            memory, hourly = feature_options(candidate)
            if memory["enabled"] or hourly["enabled"]:
                if not self.members or not self.members.history_enabled or self.members.daily_clear:
                    raise ValueError("后台功能需要持久化的群聊记录")
                from .llm import LLMClient
                LLMClient.from_file(self.store.path.parent / candidate.get("llm_config_file", "llm_config.json"))
            if section == "memory" and self.memory_worker:
                self.memory_worker.close()
                self.memory_worker = None
            if section == "hourly_chat" and self.hourly_scheduler:
                self.hourly_scheduler.close()
                self.hourly_scheduler = None
            self.config = self.store.update_section(section, changes)
            self.memory_options, self.hourly_options = feature_options(self.config)
            self._start_background()

    def run(self, *, seconds=0):
        started = time.monotonic()
        next_connect = 0
        next_rollover = 0
        try:
            if self.members is not None:
                self.members.initialize()
                upgrade = self.maintenance_options["upgrade_file"]
                self.knowledge.initialize(self.store.path.parent / self.memory_options["seed_file"],
                                          upgrade_file=self.store.path.parent / upgrade if upgrade else None)
                if self.maintenance_options["announcement"]:
                    from .upgrades import StartupNotice
                    notice = self.maintenance_options["announcement"]
                    self.knowledge._room(notice["room"])
                    if self.config.get("chat_allowlist") and notice["room"] not in self.config["chat_allowlist"]:
                        raise ValueError("更新公告群不在当前收发范围内")
                    self.startup_notice = StartupNotice(self.knowledge, self.service, notice)
            self._start_background()
            self.plugins.reload()
            if self.maintenance_router:
                self.maintenance_router.start()
            if self.config.get("console", True):
                threading.Thread(target=self._read_console, name="BotConsole", daemon=True).start()
            logger.info("框架已启动；输入 help 查看插件管理命令，Ctrl+C 退出")
            while not self.stop_event.is_set():
                now = time.monotonic()
                if self.members is not None and now >= next_rollover:
                    self.members.rollover()
                    next_rollover = now + 0.5
                if seconds and now - started >= seconds:
                    break
                while True:
                    try:
                        command = self.commands.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        self.execute(command)
                    except Exception:
                        logger.exception("控制台命令执行失败")
                if self.stop_event.is_set():
                    break
                if self._connection_ready and self.backend.last_error:
                    self._log_connection_error(self.backend.last_error)
                    self._disconnect()
                    next_connect = now + self.config.get("reconnect_seconds", 5)
                elif not self._connection_ready and now >= next_connect:
                    if self.backend is None or self._disconnect():
                        self._connect()
                    next_connect = time.monotonic() + self.config.get("reconnect_seconds", 5)
                self.stop_event.wait(0.1)
        except KeyboardInterrupt:
            logger.info("收到退出指令")
        finally:
            self.stop()

    def stop(self):
        previous_interrupt = None
        if threading.current_thread() is threading.main_thread():
            previous_interrupt = signal.signal(signal.SIGINT, lambda *_: logger.warning("正在清理退出，请等待，不要关闭窗口"))
        try:
            self.stop_event.set()
            self.service.cancel_pending()
            # 先撤销微信 Hook，后台线程等待或退出失败也不会跳过原生清理。
            logger.info("正在退出：先停止微信接收并清理桥接，再等待后台任务结束")
            cleaned = self._disconnect()
            failures = []
            for name, attribute in (("维护", "maintenance_router"), ("定时", "hourly_scheduler"), ("记忆", "memory_worker")):
                worker = getattr(self, attribute)
                if worker:
                    try:
                        worker.close()
                    except Exception as error:
                        failures.append(name)
                        logger.error("%s线程退出未完成：%s", name, type(error).__name__)
            try:
                self.plugins.shutdown()
            except Exception as error:
                failures.append("插件")
                logger.error("插件退出未完成：%s", type(error).__name__)
            # 尚存后台线程可能仍用数据库，不能强行关闭其连接。
            if self.members is not None and not failures and cleaned:
                self.members.close()
            if not cleaned:
                raise RuntimeError("原生连接未能清理；待机器人退出后运行 python tools/recover_bridge.py")
            if failures:
                raise RuntimeError("桥接已清理，但这些后台任务尚未退出：" + "、".join(failures))
            logger.info("框架已停止；连接清理：%s", self.last_cleanup)
        finally:
            if previous_interrupt is not None:
                signal.signal(signal.SIGINT, previous_interrupt)
