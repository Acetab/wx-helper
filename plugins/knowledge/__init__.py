"""知识查询与维护；权限始终由实际消息作者判断。"""
from wxbot_framework.commands import command_text, help_text
from wxbot_framework.members import MemberError, parse_aliases
from wxbot_framework.summary import split_text
from wxbot_framework.timeutil import format_local


class Plugin:
    def on_load(self, ctx):
        self.members, self.knowledge = ctx.members, ctx.knowledge

    def on_message(self, message, ctx):
        if (not message.is_group or message.type != 1 or message.chat_id not in self.members.groups or
                message.data.get("bot_generated")):
            return
        parts = command_text(message.text).split(maxsplit=1)
        if not parts or parts[0] not in ("/档案", "/记忆", "/纠正", "/忘记", "/关联人物", "/help"):
            return
        command, argument = parts[0], parts[1].strip() if len(parts) == 2 else ""
        try:
            text = self.handle(command, argument, message)
        except (MemberError, ValueError) as error:
            text = str(error)
        for part in split_text(text):
            ctx.reply(message, part)

    def handle(self, command, argument, message):
        room, sender = message.chat_id, message.sender_id
        if command == "/help":
            return help_text(self.members.is_admin(sender))
        if command == "/关联人物":
            if not self.members.is_admin(sender):
                raise MemberError("只有管理员可以关联人物")
            targets = [wxid for wxid in message.data.get("at_members", ()) if wxid != self.members.account]
            from wxbot_framework.mentions import strip_at_labels
            if targets:
                name = strip_at_labels(argument).strip()
                if len(targets) != 1 or not name:
                    raise MemberError("用法：/绑定 @群友 人物名")
                target_wxid = targets[0]
            else:
                tokens = argument.split(maxsplit=1)
                if len(tokens) != 2:
                    raise MemberError("用法：/绑定 <@群友|M序号|微信昵称|wxid> <人物名>")
                target_wxid = self.members.resolve_member_target(room, tokens[0])
                name = tokens[1].strip()
            entity = self.knowledge.link(room, target_wxid, name, sender, create_if_missing=True)
            return f"已关联：{self.members.wechat_name(target_wxid) or self.members.display_name(target_wxid, room) or '该成员'} → {entity['name']}。"
        if command in ("/纠正", "/忘记"):
            values = argument.split(maxsplit=1)
            if (not values or not values[0].isdigit() or len(values) != (1 if command == "/忘记" else 2)):
                raise MemberError("用法：/忘记 编号" if command == "/忘记" else "用法：/纠正 编号 新内容")
            self.knowledge.edit(room, int(values[0]), sender, forget=command == "/忘记",
                                content=values[1] if len(values) == 2 else None)
            return "已停用该记忆，聊天原文仍保留。" if command == "/忘记" else "已纠正并保存修订记录。"
        if command == "/记忆":
            if not argument:
                raise MemberError("用法：/记忆 关键词")
            items = self.knowledge.search(room, argument)
            return (self.format_items(items, self.knowledge.display_timezone)
                    if items else "还没有找到相关的长期记忆。")
        entity = (self.knowledge.find_entity(room, argument) if argument else
                  self.knowledge.member_entity(room, sender))
        items = self.knowledge.search(room, "", entity_id=entity["id"])
        name = (self.members.display_name(entity["wxid"], room) if entity["wxid"] else None) or entity["name"]
        if entity["entity_key"].startswith("group:"):
            state = "本群公共记忆"
        elif entity["wxid"]:
            wechat = self.members.wechat_name(entity["wxid"])
            state = f"已绑定微信成员（微信名：{wechat}）" if wechat else "已绑定微信成员"
        else:
            state = "人物资料，尚未绑定微信成员"
        if not self.knowledge.visible(entity):
            state += "；提及量尚未达到自动检索门槛"
        style = self.knowledge.style(room, entity["wxid"]) if entity["wxid"] else None
        return self.format_profile(name, state, entity, items, style, self.members.is_admin(sender))

    @staticmethod
    def format_profile(name, state, entity, items, style, is_admin=False):
        aliases = parse_aliases(entity["aliases"])
        lines = [f"{name}（{state}）"]
        if aliases:
            lines.append("别名：" + "、".join(aliases))
        if style:
            lines.append(f"表达习惯（{style['sample_count']}条样本）：{style['description']}")
        else:
            lines.append("表达习惯：样本不足（至少30条）或已被停用。")
        if items:
            lines.append("重要记忆：")
            for item in items:
                prefix = f"#{item['id']} " if is_admin else ""
                lines.append(f"· {prefix}{item['content']}（{item['kind']}）")
        else:
            lines.append("尚无可展示的长期资料。")
        return "\n".join(lines)

    @staticmethod
    def format_items(items, display_timezone=None):
        lines = []
        for item in items:
            sources = item["sources"]
            date = format_local(
                sources[0]["source_time"] if sources else item["updated_at"],
                display_timezone, pattern="%Y-%m-%d")
            if any(source["origin"].startswith("seed:") for source in sources):
                origin = "space整理"
            elif any((source["origin"] or "").startswith("manual:") for source in sources):
                origin = "人工维护"
            else:
                origin = "聊天记录"
            lines.append(f"· {item['name']}｜{item['kind']}：{item['content']}（{origin}，{date}）")
        return "\n\n".join(lines)
