"""确定性的维护命令；独立队列不依赖可停用的聊天插件。"""
from __future__ import annotations

import json
import logging
import queue
import shlex
import threading

from .authorization import LOCAL_EDITOR
from .commands import command_text
from .members import MemberError
from .summary import split_text
from .text import validate_target

logger = logging.getLogger("Maintenance")
HELP = """维护命令（名字或正文含空格时使用英文双引号）：
/管理 状态
/管理 群 列表
/管理 [--群 群ID] 人物 列表 [页码]
/管理 人物 查看 人物
/管理 人物 新建 人物
/管理 人物 改名 人物 新名字
/管理 人物 关联 人物 微信ID
/管理 人物 解绑 人物
/管理 人物 清空 人物
/管理 确认 操作编号
/管理 别名 添加|删除 人物 别名
/管理 成员 列表
/管理 成员 绑定 微信ID 昵称
/管理 记忆 列表|历史 人物 [页码]
/管理 记忆 新增 人物 主题 类别 正文
/管理 记忆 修改 编号 正文
/管理 记忆 移动 编号 目标人物
/管理 记忆 停用|恢复|来源|修订 编号 [页码]
/管理 记忆 分类 编号 类别
/管理 人物 修订 人物 [页码]
/管理 学习 开启|关闭|立即处理
/管理 定时 开启|关闭
/管理 定时 设置 开始小时 结束小时 最近条数
/管理 上下文 清空
/管理 插件 列表|启用 名称|停用 名称|重载 [名称]
/管理 公告 状态|重发
清空人物资料后从新消息继续学习；/清空仅重置对话上下文。"""


def maintenance_options(config):
    options = config.get("maintenance", {})
    if not isinstance(options, dict):
        raise ValueError("maintenance 应为 JSON 对象")
    options = {"default_room": None, "upgrade_file": None, "announcement": None, **options}
    if options["default_room"] is not None:
        validate_target(options["default_room"])
        if not options["default_room"].endswith("@chatroom"):
            raise ValueError("维护默认目标必须是群 ID")
    path = options["upgrade_file"]
    if path is not None and (not isinstance(path, str) or not path.strip() or "\0" in path):
        raise ValueError("upgrade_file 应为本地文件路径或 null")
    notice = options["announcement"]
    if notice is not None:
        if not isinstance(notice, dict) or not all(isinstance(notice.get(k), str) and notice[k].strip() for k in ("id", "room", "text")):
            raise ValueError("announcement 需要 id、room、text")
        validate_target(notice["room"])
        if not notice["room"].endswith("@chatroom") or len(notice["id"]) > 80 or len(notice["text"].encode("utf-8")) > 900 or "\0" in notice["text"]:
            raise ValueError("更新公告目标或正文不正确")
    return options


class MaintenanceService:
    def __init__(self, knowledge, *, app=None, options=None):
        self.knowledge, self.members, self.app = knowledge, knowledge.members, app
        self.options = options or {}
        self._lock = threading.RLock()

    def room(self, explicit=None, current=None):
        target = explicit or current or self.options.get("default_room")
        if not target and len(self.members.groups) == 1:
            target = next(iter(self.members.groups))
        if not target:
            raise MemberError("请使用 --群 群ID 指定要维护的群")
        if current and explicit and current != explicit:
            raise MemberError("群聊命令只能维护当前群，跨群操作请私聊")
        self.knowledge._room(target)
        if self.app and self.app.config.get("chat_allowlist") and target not in self.app.config["chat_allowlist"]:
            raise MemberError("目标群不在当前收发范围内")
        return target

    def execute(self, command, editor, *, current_room=None, explicit_room=None):
        self.knowledge.require_editor(editor)
        with self._lock, self.knowledge._lock:
            # 仅解析和快速数据库操作持锁，运行操作由下方独立分支处理。
            words = shlex.split(command)
            if words[:1] == ["--群"] and len(words) >= 3:
                explicit_room, words = words[1], words[2:]
            if words in ([], ["帮助"], ["help"]):
                return HELP
            if words == ["群", "列表"]:
                return "已配置群：\n" + "\n".join(sorted(self.members.groups))
            room = self.room(explicit_room, current_room)
            if words == ["状态"]:
                state = {"目标群": room, "知识库": self.knowledge.status(), "已连接": bool(self.app and self.app._connection_ready)}
                if self.app:
                    state.update(学习=self.app.memory_options, 定时=self.app.hourly_options)
                return json.dumps(state, ensure_ascii=False, indent=2)
            if words[:1] == ["确认"] and len(words) == 2:
                name = self.knowledge.confirm_clear(room, words[1], editor)
                return f"已清空{name}的现行资料；将从此后的新聊天继续学习，原文和旧资料历史保留。"
            if words == ["上下文", "清空"]:
                self.members.reset_context(room)
                return "已重置本群近期对话上下文，聊天原文和长期知识保留。"
            if words[:1] == ["人物"]:
                return self._people(room, words[1:], editor)
            if words[:1] == ["别名"] and len(words) == 4 and words[1] in ("添加", "删除"):
                self.knowledge.change_identity(room, words[2], editor, alias=words[3], remove=words[1] == "删除")
                return "已更新人物别名并保存操作记录。"
            if words[:1] == ["记忆"]:
                return self._memory(room, words[1:], editor)
            if words[:1] == ["成员"]:
                return self._members(room, words[1:], editor)
        # 启停线程、等待插件退出与发送公告均不能占数据库锁。
        with self._lock:
            return self._runtime(room, words)

    @staticmethod
    def page(value):
        page = int(value)
        if not 1 <= page <= 100000:
            raise MemberError("页码必须为正整数")
        return page

    def _people(self, room, words, editor):
        k = self.knowledge
        if words[:1] == ["列表"] and len(words) in (1, 2):
            page = self.page(words[1]) if len(words) == 2 else 1
            entities = k._entities(room)
            return f"人物共{len(entities)}份，第{page}页：\n" + "\n".join(
                f"#{e['id']} {e['name']}｜别名：{'、'.join(json.loads(e['aliases'])) or '无'}｜{'已关联成员' if e['wxid'] else '独立人物'}"
                for e in entities[(page-1)*20:page*20])
        if len(words) < 2:
            raise MemberError("用法：/管理 人物 查看|新建|改名|关联|解绑|清空|修订 人物")
        action, value = words[:2]
        if action == "新建" and len(words) == 2:
            entity = k.entity(room, value, pinned=True)
            with k._db():
                k.entity_audit(room, entity["id"], editor, "create", {}, entity)
            return f"人物已就绪：#{entity['id']} {entity['name']}。"
        entity = k.admin_entity(room, value)
        if action == "查看" and len(words) == 2:
            _, count, items = k.admin_items(room, entity["id"])
            return (f"#{entity['id']} {entity['name']}\n别名：{'、'.join(json.loads(entity['aliases'])) or '无'}\n"
                    f"关联ID：{entity['wxid'] or '未关联'}\n现行资料{count}条，清空后学习起点 #{entity['learning_floor']}\n" + self.format_items(items))
        if action == "改名" and len(words) == 3:
            k.change_identity(room, value, editor, name=words[2])
            return "已改名，原名字保留为别名；可另行删除旧别名。"
        if action == "关联" and len(words) == 3:
            k.link(room, words[2], entity["name"], editor)
            return "已关联真实微信成员并保存记录。"
        if action == "解绑" and len(words) == 2:
            k.change_identity(room, value, editor, unlink=True)
            return "已解除微信身份关联，人物资料保留。"
        if action == "清空" and len(words) == 2:
            _, count, code = k.prepare_clear(room, value, editor)
            return f"将停用{entity['name']}的现行知识（有效{count}条），之后从新聊天继续学习。\n五分钟内发送：/管理 确认 {code}"
        if action == "修订" and len(words) in (2, 3):
            page = self.page(words[2]) if len(words) == 3 else 1
            rows = k._db().execute("SELECT action,previous,replacement,edited_at FROM knowledge_entity_edits WHERE account=? AND room=? AND entity_id=? ORDER BY id DESC LIMIT 5 OFFSET ?",
                                   (k.account, room, entity["id"], (page-1)*5)).fetchall()
            return "\n".join(json.dumps(dict(r), ensure_ascii=False) for r in rows) or "本页没有操作记录。"
        raise MemberError("人物命令参数不正确，请发送 /管理 帮助")

    @staticmethod
    def format_items(items):
        return "\n".join(f"#{i['id']} {i['slot']}｜{i['kind']}｜{i['status']}\n{i['content']}" for i in items) or "本页没有条目。"

    def _memory(self, room, words, editor):
        k = self.knowledge
        if len(words) >= 2 and words[0] in ("列表", "历史") and len(words) in (2, 3):
            page = self.page(words[2]) if len(words) == 3 else 1
            entity, count, items = k.admin_items(room, words[1], page=page, history=words[0] == "历史")
            return f"{entity['name']} 共{count}条，第{page}页：\n" + self.format_items(items)
        if words[:1] == ["新增"] and len(words) >= 5:
            item = k.add_memory(room, words[1], words[2], words[3], " ".join(words[4:]), editor)
            return f"已新增人工维护条目 #{item}。"
        if len(words) < 2:
            raise MemberError("记忆命令参数不正确，请发送 /管理 帮助")
        action, item_id = words[0], int(words[1].lstrip("#"))
        item = k.admin_item(room, item_id)
        if (action == "修改" and len(words) >= 3) or (action in ("分类", "移动") and len(words) == 3):
            if action == "修改":
                k.edit(room, item_id, editor, content=" ".join(words[2:]))
            elif action == "分类":
                k.classify_memory(room, item_id, words[2], editor)
            else:
                k.move_memory(room, item_id, words[2], editor)
            return "已更新并保存修订记录。"
        if action in ("停用", "恢复") and len(words) == 2:
            if action == "停用":
                k.edit(room, item_id, editor, forget=True)
                return "已停用该条目，原文与来源保留。"
            restored = k.restore_memory(room, item_id, editor)
            return f"已恢复为现行条目 #{restored}。"
        if action in ("来源", "修订") and len(words) in (2, 3):
            page = self.page(words[2]) if len(words) == 3 else 1
            if action == "来源":
                rows = k._db().execute("SELECT origin,message_id,source_time FROM knowledge_sources WHERE item_id=? ORDER BY source_time DESC LIMIT 10 OFFSET ?",
                                       (item["id"], (page-1)*10)).fetchall()
            else:
                rows = k._db().execute("SELECT action,previous,replacement,edited_at FROM knowledge_edits WHERE item_id=? ORDER BY id DESC LIMIT 10 OFFSET ?",
                                       (item["id"], (page-1)*10)).fetchall()
            return "\n".join(json.dumps(dict(r), ensure_ascii=False) for r in rows) or "本页没有记录。"
        raise MemberError("记忆命令参数不正确，请发送 /管理 帮助")

    def _members(self, room, words, editor):
        if words == ["列表"]:
            db = self.knowledge._db()
            rows = db.execute("SELECT wxid FROM room_members WHERE account=? AND room=? ORDER BY wxid", (self.members.account, room))
            return "\n".join(f"{r[0]} {self.members.display_name(r[0], room) or '未绑定'}" for r in rows) or "尚无成员记录。"
        if words[:1] == ["绑定"] and len(words) == 3:
            self.knowledge.require_editor(editor)
            seen = self.knowledge._db().execute("SELECT 1 FROM room_members WHERE account=? AND room=? AND wxid=?",
                                               (self.members.account, room, words[1])).fetchone()
            if not seen:
                raise MemberError("该微信 ID 尚未在此群留下记录")
            member = self.members.register(words[2], room, words[1])
            with self.knowledge._db():
                self.knowledge.entity_audit(room, 0, editor, "bind-member", {}, {"wxid": member.wxid, "name": member.name})
            return "已更新该成员的称呼。"
        raise MemberError("用法：/管理 成员 列表 或 /管理 成员 绑定 微信ID 昵称")

    def _runtime(self, room, words):
        app = self.app
        if not app:
            raise MemberError("此命令需要运行中的机器人，请使用运行控制台或微信维护入口")
        if words[:1] == ["学习"] and len(words) == 2:
            if words[1] == "立即处理":
                if not app.memory_worker:
                    raise MemberError("请先开启自动学习")
                app.memory_worker.last_flush.clear()
                app.memory_worker.retry_after.clear()
                app.memory_worker.notify()
                return "已请求后台立即处理积压消息。"
            if words[1] in ("开启", "关闭"):
                app.update_feature("memory", {"enabled": words[1] == "开启"})
                return "已更新自动学习开关（作用于此机器人的全部配置群）。"
        if words[:1] == ["定时"]:
            if len(words) == 2 and words[1] in ("开启", "关闭"):
                changes = {"enabled": words[1] == "开启"}
            elif len(words) == 5 and words[1] == "设置":
                changes = dict(zip(("start_hour", "end_hour", "context_limit"), map(int, words[2:])))
            else:
                raise MemberError("用法：/管理 定时 开启|关闭 或 /管理 定时 设置 9 23 200")
            app.update_feature("hourly_chat", changes)
            return "已更新整点发言设置（作用于全部配置群），不补发已过时段。"
        if words[:1] == ["插件"]:
            if words == ["插件", "列表"]:
                return json.dumps(app.plugins.status(), ensure_ascii=False)
            if len(words) == 3 and words[1] in ("启用", "停用"):
                getattr(app.plugins, "enable" if words[1] == "启用" else "disable")(words[2])
                return "已更新插件状态，维护入口继续可用。"
            if words[1:2] == ["重载"] and len(words) in (2, 3):
                app.plugins.reload(words[2] if len(words) == 3 else None)
                return "已重载插件。"
        if words[:1] == ["公告"] and len(words) == 2:
            if not app.startup_notice:
                raise MemberError("尚未配置本次更新公告")
            if words[1] == "状态":
                return "本版本公告状态：" + app.startup_notice.status()
            if words[1] == "重发":
                if not app._connection_ready:
                    raise MemberError("微信尚未连接，未重发公告")
                app.startup_notice.retry()
                return "已发送更新公告。" if app.startup_notice.send() else "公告结果待确认，请检查群聊与公告状态。"
        raise MemberError("未知维护命令，请发送 /管理 帮助")


class MaintenanceRouter:
    def __init__(self, service, facade):
        self.service, self.facade = service, facade
        self.queue = queue.Queue(maxsize=128)
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._run, name="Maintenance", daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=70)
            if self.thread.is_alive():
                raise RuntimeError("维护线程尚未退出，暂不关闭数据库")

    def submit(self, message):
        text = command_text(message.text)
        root = text.split(maxsplit=1)[0] if text else ""
        if root not in ("/管理", "/纠正", "/忘记", "/关联人物", "/清空") or message.type != 1:
            return False
        if (message.is_self or message.data.get("bot_generated") or not message.data.get("dispatch_to_plugins", True) or
                message.data.get("self_wxid", self.service.members.account) != self.service.members.account):
            return True
        try:
            self.queue.put_nowait(message)
        except queue.Full:
            logger.warning("维护队列已满，此指令未执行")
        return True

    def process(self, message):
        members = self.service.members
        if self.facade.session_id != message.session_id:
            logger.info("忽略已结束连接中的维护命令")
            return
        try:
            self.service.knowledge.require_editor(message.sender_id)
            if not message.is_group and message.chat_id != message.sender_id:
                raise MemberError("私聊会话与真实发送者不匹配")
            text = command_text(message.text)
            pieces = text.split(maxsplit=1)
            root, argument = pieces[0], pieces[1] if len(pieces) == 2 else ""
            if root == "/管理":
                command = argument
            elif root in ("/纠正", "/忘记"):
                values = argument.split(maxsplit=1)
                if not values:
                    raise MemberError("请指定记忆编号")
                command = shlex.join(["记忆", "修改" if root == "/纠正" else "停用", *values])
            elif root == "/清空":
                command = "上下文 清空"
            else:
                from .mentions import strip_at_labels
                targets = [wxid for wxid in message.data.get("at_members", ()) if wxid != members.account]
                name = strip_at_labels(argument)
                if len(targets) != 1 or not name:
                    raise MemberError("用法：/关联人物 @群友 人物名（使用微信真实艾特）")
                command = shlex.join(["人物", "关联", name, targets[0]])
            if root == "/清空" and message.data.get("context_reset"):
                result = "已重置本群近期对话上下文，聊天原文和长期知识保留。"
            else:
                result = self.service.execute(command, message.sender_id, current_room=message.chat_id if message.is_group else None)
        except MemberError as error:
            result = str(error)
        except (ValueError, KeyError):
            result = "参数格式不正确，请检查编号、数字和英文引号，或发送 /管理 帮助。"
        except Exception as error:
            logger.error("维护命令失败：%s", type(error).__name__)
            result = "维护命令执行失败，请在服务器查看错误类型和状态。"
        for part in split_text(result):
            if self.stop_event.is_set():
                break
            self.facade.send_text(message.chat_id, part, expected_session=message.session_id)

    def _run(self):
        while not self.stop_event.is_set():
            try:
                message = self.queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.process(message)
            except Exception as error:
                logger.warning("维护回复未完成：%s", type(error).__name__)
            finally:
                self.queue.task_done()
