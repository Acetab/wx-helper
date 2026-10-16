"""确定性的维护命令；独立队列不依赖可停用的聊天插件。"""
from __future__ import annotations

import json
import logging
import queue
import shlex
import threading

from .authorization import LOCAL_EDITOR
from .commands import command_text
from .members import MemberError, parse_aliases
from .mentions import strip_at_labels
from .summary import split_text
from .text import validate_target

logger = logging.getLogger("Maintenance")
ADMIN_ROOTS = frozenset({
    "/管理", "/绑定", "/解绑", "/别名", "/改名", "/成员", "/人物", "/记住",
    "/纠正", "/忘记", "/关联人物", "/清空",
})
HELP = """管理员快捷命令（群聊与私聊通用，含空格时用英文双引号）：
/成员 [未绑定|已绑定|同步]
/绑定 <@群友|M序号|微信昵称|wxid> <人物名> [--强制]
/解绑 <@群友|M序号|微信昵称|wxid|人物名>
/人物 [页码|人物名|#编号]
/别名 <人物名> <+新别名|-旧别名>
/改名 <人物名> <新主名>
/记住 <人物名> <主题> [类别] <内容>
/纠正 <记忆编号> <新内容>
/忘记 <记忆编号>
/清空

完整 /管理 子命令：
/管理 状态
/管理 同步
/管理 群 列表
/管理 [--群 群ID] 人物 列表 [页码]
/管理 人物 查看|新建|解绑|清空|修订 人物
/管理 人物 改名 人物 新名字
/管理 人物 关联 人物 微信ID
/管理 确认 操作编号
/管理 别名 添加|删除 人物 别名
/管理 成员 列表|未绑定|已绑定|同步
/管理 记忆 列表|历史 人物 [页码]
/管理 记忆 新增 人物 主题 类别 正文
/管理 记忆 修改|移动|分类 编号 参数
/管理 记忆 停用|恢复|来源|修订 编号 [页码]
/管理 学习 开启|关闭|立即处理
/管理 定时 开启|关闭|设置 开始小时 结束小时 最近条数
/管理 插件 列表|启用 名称|停用 名称|重载 [名称]
/管理 公告 状态|重发"""


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

    def execute(self, command, editor, *, current_room=None, explicit_room=None, at_members=()):
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
            if words == ["同步"]:
                synced = self.app.sync_wechat_directory(room) if self.app else {"members": 0, "names": 0}
                return f"已同步微信内存群成员缓存：成员 {synced['members']} 人，有效微信昵称 {synced['names']} 个。"
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
            if words[:1] == ["绑定"]:
                return self._bind_shortcut(room, words[1:], editor, at_members=at_members)
            if words[:1] == ["解绑"]:
                return self._unbind_shortcut(room, words[1:], editor, at_members=at_members)
            if words[:1] == ["改名"] and len(words) == 3:
                entity = self.knowledge.change_identity(room, words[1], editor, name=words[2])
                return f"已将人物改名为 #{entity['id']} {entity['name']}（原名保留为别名）。"
            if words[:1] == ["记住"] and len(words) >= 4:
                return self._remember_shortcut(room, words[1:], editor)
            if words[:1] == ["人物"]:
                return self._people(room, words[1:], editor)
            if words[:1] == ["别名"]:
                return self._alias_command(room, words[1:], editor)
            if words[:1] == ["记忆"]:
                return self._memory(room, words[1:], editor)
            if words[:1] == ["成员"]:
                return self._members(room, words[1:], editor)
        # 启停线程、等待插件退出与发送公告均不能占数据库锁。
        with self._lock:
            return self._runtime(room, words)

    def _bind_shortcut(self, room, words, editor, *, at_members=()):
        force = False
        filtered = []
        for w in words:
            if w in ("--强制", "强制", "--force"):
                force = True
            else:
                filtered.append(w)
        targets = [wxid for wxid in at_members if wxid != self.members.account]
        if targets:
            if len(targets) != 1 or not filtered:
                raise MemberError("用法：/绑定 @群友 人物名 [--强制]")
            wxid = targets[0]
            person_name = " ".join(filtered).strip()
        else:
            if len(filtered) != 2:
                raise MemberError("用法：/绑定 <@群友|M序号|微信昵称|wxid> <人物名> [--强制]")
            wxid = self.members.resolve_member_target(room, filtered[0])
            person_name = filtered[1].strip()
        entity = self.knowledge.link(room, wxid, person_name, editor, create_if_missing=True, replace=force)
        wechat = self.members.wechat_name(wxid) or "未缓存微信名"
        aliases = "、".join(parse_aliases(entity["aliases"])) or "无"
        return f"已绑定：{wechat}（{wxid}）→ #{entity['id']} {entity['name']}（别名：{aliases}）。"

    def _unbind_shortcut(self, room, words, editor, *, at_members=()):
        targets = [wxid for wxid in at_members if wxid != self.members.account]
        if targets:
            if len(targets) != 1:
                raise MemberError("一次只能解绑一位群成员")
            entity = self.knowledge.member_entity(room, targets[0])
            self.knowledge.change_identity(room, entity["id"], editor, unlink=True)
            return f"已解除 #{entity['id']} {entity['name']} 的微信身份绑定，人物资料保留。"
        if len(words) != 1:
            raise MemberError("用法：/解绑 <@群友|M序号|微信昵称|wxid|人物名>")
        token = words[0].strip()
        try:
            entity = self.knowledge.admin_entity(room, token)
            if entity["entity_key"].startswith("person:") and entity["wxid"]:
                self.knowledge.change_identity(room, entity["id"], editor, unlink=True)
                return f"已解除 #{entity['id']} {entity['name']} 的微信身份绑定，人物资料保留。"
        except MemberError:
            pass
        wxid = self.members.resolve_member_target(room, token)
        entity = self.knowledge.member_entity(room, wxid)
        self.knowledge.change_identity(room, entity["id"], editor, unlink=True)
        return f"已解除 #{entity['id']} {entity['name']} 的微信身份绑定，人物资料保留。"

    def _alias_command(self, room, words, editor):
        if len(words) == 3 and words[0] in ("添加", "删除"):
            entity = self.knowledge.change_identity(room, words[1], editor, alias=words[2], remove=words[0] == "删除")
            return f"已更新 #{entity['id']} {entity['name']} 的别名：{'、'.join(parse_aliases(entity['aliases'])) or '无'}。"
        if len(words) == 3 and words[1] in ("添加", "删除", "+", "-"):
            remove = words[1] in ("删除", "-")
            entity = self.knowledge.change_identity(room, words[0], editor, alias=words[2], remove=remove)
            return f"已更新 #{entity['id']} {entity['name']} 的别名：{'、'.join(parse_aliases(entity['aliases'])) or '无'}。"
        if len(words) == 2 and len(words[1]) >= 2 and words[1][0] in ("+", "-"):
            remove = words[1][0] == "-"
            entity = self.knowledge.change_identity(room, words[0], editor, alias=words[1][1:], remove=remove)
            return f"已更新 #{entity['id']} {entity['name']} 的别名：{'、'.join(parse_aliases(entity['aliases'])) or '无'}。"
        raise MemberError("用法：/别名 <人物名> <+新别名|-旧别名> 或 /管理 别名 添加|删除 人物 别名")

    def _remember_shortcut(self, room, words, editor):
        from .knowledge import KINDS
        person, slot = words[0], words[1]
        if len(words) >= 4 and words[2] in KINDS:
            kind, content = words[2], " ".join(words[3:])
        else:
            kind, content = "人工确认", " ".join(words[2:])
        entity = self.knowledge.admin_entity(room, person)
        existing = self.knowledge._db().execute(
            "SELECT id FROM knowledge_items WHERE entity_id=? AND epoch=? AND slot=?",
            (entity["id"], entity["epoch"], slot)).fetchone()
        if existing:
            self.knowledge.edit(room, existing["id"], editor, content=content)
            if kind != "人工确认":
                self.knowledge.classify_memory(room, existing["id"], kind, editor)
            return f"已更新 #{entity['name']} 的记忆条目 #{existing['id']}（{slot}）。"
        item_id = self.knowledge.add_memory(room, entity["id"], slot, kind, content, editor)
        return f"已为 #{entity['id']} {entity['name']} 新增记忆 #{item_id}（{slot}｜{kind}）。"

    @staticmethod
    def page(value):
        page = int(value)
        if not 1 <= page <= 100000:
            raise MemberError("页码必须为正整数")
        return page

    def _people(self, room, words, editor):
        k = self.knowledge
        if not words or (words[:1] == ["列表"] and len(words) in (1, 2)) or (len(words) == 1 and words[0].isdigit()):
            if not words:
                page = 1
            elif words[0] == "列表":
                page = self.page(words[1]) if len(words) == 2 else 1
            else:
                page = self.page(words[0])
            entities = [e for e in k._entities(room) if e["entity_key"].startswith("person:")]
            code_by_wxid = {entry["wxid"]: entry["code"] for entry in self.members.room_member_directory(room)}
            return f"人物共{len(entities)}份，第{page}页：\n" + "\n".join(
                f"#{e['id']} {e['name']}｜别名：{'、'.join(parse_aliases(e['aliases'])) or '无'}｜"
                + (f"已绑 {code_by_wxid.get(e['wxid'], '')} 微信:{self.members.wechat_name(e['wxid']) or e['wxid']}" if e["wxid"] else "未绑定")
                for e in entities[(page-1)*20:page*20])
        if len(words) == 1:
            words = ["查看", words[0]]
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
            wechat = f"{self.members.wechat_name(entity['wxid']) or '未缓存'} ({entity['wxid']})" if entity["wxid"] else "未绑定"
            return (f"#{entity['id']} {entity['name']}\n别名：{'、'.join(parse_aliases(entity['aliases'])) or '无'}\n"
                    f"绑定微信：{wechat}\n现行资料{count}条，清空后学习起点 #{entity['learning_floor']}\n" + self.format_items(items))
        if action == "改名" and len(words) == 3:
            k.change_identity(room, value, editor, name=words[2])
            return "已改名，原名字保留为别名；可另行删除旧别名。"
        if action == "关联" and len(words) == 3:
            wxid = self.members.resolve_member_target(room, words[2])
            k.link(room, wxid, entity["name"], editor)
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
        if not words or words in (["列表"], ["未绑定"], ["已绑定"], ["同步"]):
            if self.app and self.app._connection_ready:
                self.app.sync_wechat_directory(room)
            directory = self.members.room_member_directory(room)
            if not directory:
                return "尚无成员记录。"
            mode = words[0] if words else "列表"
            bound_count = sum(1 for entry in directory if entry["member"] is not None)
            if mode == "未绑定":
                directory = [entry for entry in directory if entry["member"] is None]
            elif mode == "已绑定":
                directory = [entry for entry in directory if entry["member"] is not None]
            lines = [f"群成员共{len(directory)}人（全群已绑定{bound_count}人）："]
            for entry in directory:
                if entry["member"] is not None:
                    status = f"已绑:{entry['member'].name}"
                elif entry["impersonating"]:
                    status = "未绑定·撞名保护"
                else:
                    status = "未绑定"
                wx_name = entry["wechat_name"] or "未缓存"
                lines.append(f"{entry['code']} [{status}] 微信:{wx_name} ({entry['wxid']})")
            return "\n".join(lines)
        if words[:1] == ["绑定"] and len(words) == 3:
            self.knowledge.require_editor(editor)
            return self._bind_shortcut(room, [words[1], words[2]], editor)
        raise MemberError("用法：/成员 [未绑定|已绑定|同步] 或 /绑定 <@群友|M序号|微信昵称|wxid> <人物名>")

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
                info = app.startup_notice.describe()
                return (f"本版本公告状态：{info['status']}（id={info['id']}，群={info['room']}，"
                        f"最近尝试={info['updated_at'] or '尚未发送'}）")
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
        if root not in ADMIN_ROOTS or message.type != 1:
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
            at_members = ()
            if root == "/管理":
                command = argument
                at_members = message.data.get("at_members", ())
            elif root in ("/绑定", "/关联人物"):
                at_members = [wxid for wxid in message.data.get("at_members", ()) if wxid != members.account]
                cleaned_arg = strip_at_labels(argument).strip() if at_members else argument.strip()
                if at_members and cleaned_arg.startswith("@") and len(cleaned_arg.split(maxsplit=1)) == 2:
                    cleaned_arg = cleaned_arg.split(maxsplit=1)[1].strip()
                if not cleaned_arg:
                    raise MemberError("用法：/绑定 <@群友|M序号|微信昵称|wxid> <人物名>")
                command = "绑定 " + cleaned_arg
            elif root == "/解绑":
                at_members = [wxid for wxid in message.data.get("at_members", ()) if wxid != members.account]
                cleaned_arg = strip_at_labels(argument).strip() if at_members else argument.strip()
                if at_members and cleaned_arg.startswith("@"):
                    parts = cleaned_arg.split(maxsplit=1)
                    cleaned_arg = parts[1].strip() if len(parts) == 2 else ""
                if not cleaned_arg and not at_members:
                    raise MemberError("用法：/解绑 <@群友|M序号|微信昵称|wxid|人物名>")
                command = "解绑" + (" " + cleaned_arg if cleaned_arg else "")
            elif root == "/成员":
                command = "成员" + (" " + argument.strip() if argument.strip() else " 列表")
            elif root == "/人物":
                command = "人物" + (" " + argument.strip() if argument.strip() else " 列表")
            elif root == "/别名":
                if not argument.strip():
                    raise MemberError("用法：/别名 <人物名> <+新别名|-旧别名>")
                command = "别名 " + argument.strip()
            elif root == "/改名":
                if not argument.strip():
                    raise MemberError("用法：/改名 <旧人物名> <新人物名>")
                command = "改名 " + argument.strip()
            elif root == "/记住":
                if not argument.strip():
                    raise MemberError("用法：/记住 <人物名> <主题> [类别] <内容>")
                command = "记住 " + argument.strip()
            elif root in ("/纠正", "/忘记"):
                values = argument.split(maxsplit=1)
                if not values:
                    raise MemberError("请指定记忆编号")
                command = shlex.join(["记忆", "修改" if root == "/纠正" else "停用", *values])
            elif root == "/清空":
                command = "上下文 清空"
            if root == "/清空" and message.data.get("context_reset"):
                result = "已重置本群近期对话上下文，聊天原文和长期知识保留。"
            else:
                result = self.service.execute(command, message.sender_id,
                                              current_room=message.chat_id if message.is_group else None,
                                              at_members=at_members)
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
