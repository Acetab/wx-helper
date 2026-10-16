"""知识查询与维护；权限始终由实际消息作者判断。"""
from wxbot_framework.commands import command_text, HELP_TEXT
from wxbot_framework.members import MemberError
from wxbot_framework.summary import split_text


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
            return HELP_TEXT
        if command == "/关联人物":
            if not self.members.is_admin(sender):
                raise MemberError("只有管理员可以关联人物")
            targets = [wxid for wxid in message.data.get("at_members", ()) if wxid != self.members.account]
            from wxbot_framework.mentions import strip_at_labels
            name = strip_at_labels(argument)
            if len(targets) != 1 or not name:
                raise MemberError("用法：/关联人物 @群友 人物名（请使用微信真实艾特）")
            entity = self.knowledge.link(room, targets[0], name, sender)
            return f"已关联：{self.members.display_name(targets[0], room) or '该成员'} → {entity['name']}。"
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
            return self.format_items(items) if items else "还没有找到相关的长期记忆。"
        entity = (self.knowledge.find_entity(room, argument) if argument else
                  self.knowledge.member_entity(room, sender))
        items = self.knowledge.search(room, "", entity_id=entity["id"])
        name = (self.members.display_name(entity["wxid"], room) if entity["wxid"] else None) or entity["name"]
        state = "已关联微信成员" if entity["wxid"] else "人物资料，尚未关联微信成员"
        if not self.knowledge.visible(entity):
            state += "；提及量尚未达到自动检索门槛"
        style = self.knowledge.style(room, entity["wxid"]) if entity["wxid"] else None
        description = (f"表达习惯（{style['sample_count']}条样本）：{style['description']}" if style else
                       "表达习惯尚无足够样本（至少30条），或已被停用。")
        return f"{name}（{state}）\n{description}\n" + (self.format_items(items) if items else "尚无可展示的长期资料。")

    @staticmethod
    def format_items(items):
        lines = []
        for item in items:
            sources = item["sources"]
            ids = "/".join(str(source["message_id"]) for source in sources if source["message_id"] is not None)
            origin = "空间整理（导入日期）" if any(s["origin"].startswith("seed:") for s in sources) else "聊天记录#" + (ids or "待补充")
            date = sources[0]["source_time"][:10] if sources else item["updated_at"][:10]
            lines.append(f"#{item['id']} {item['name']}｜{item['kind']}\n{item['content']}\n来源：{origin}，{date}")
        return "\n\n".join(lines)
