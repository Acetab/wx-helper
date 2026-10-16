"""兼容群内 /绑定 入口；日常群友微信昵称已自动读取，人物绑定统一由管理员执行。"""
from wxbot_framework.members import MemberError
from wxbot_framework.commands import command_text
from wxbot_framework.mentions import strip_at_labels


class Plugin:
    def on_load(self, ctx):
        self.members = ctx.members

    def on_message(self, message, ctx):
        if not message.is_group or message.type != 1 or message.chat_id not in self.members.groups:
            return
        parts = command_text(message.text).split(maxsplit=1)
        if not parts or parts[0] != "/绑定":
            return
        if not self.members.is_admin(message.sender_id):
            ctx.reply(message, "群友微信昵称已自动识别，无需自助绑定；人物身份请由管理员使用 /绑定 指定。")
            return
        if len(parts) != 2:
            ctx.reply(message, "用法：/绑定 <@群友|M序号|微信昵称|wxid> <人物名>")
            return
        argument = parts[1].strip()
        at_targets = [wxid for wxid in message.data.get("at_members", ()) if wxid != self.members.account]
        try:
            if at_targets:
                person_name = strip_at_labels(argument).strip()
                if person_name.startswith("@") and len(person_name.split(maxsplit=1)) == 2:
                    person_name = person_name.split(maxsplit=1)[1].strip()
                if len(at_targets) != 1 or not person_name:
                    raise MemberError("用法：/绑定 @群友 人物名")
                entity = ctx.knowledge.link(message.chat_id, at_targets[0], person_name, message.sender_id, create_if_missing=True)
                ctx.reply(message, f"已绑定：{self.members.wechat_name(at_targets[0]) or at_targets[0]} → #{entity['id']} {entity['name']}。")
                return
            tokens = argument.split(maxsplit=1)
            if len(tokens) == 2:
                wxid = self.members.resolve_member_target(message.chat_id, tokens[0])
                entity = ctx.knowledge.link(message.chat_id, wxid, tokens[1].strip(), message.sender_id, create_if_missing=True)
                ctx.reply(message, f"已绑定：{self.members.wechat_name(wxid) or wxid} → #{entity['id']} {entity['name']}。")
                return
            raise MemberError("用法：/绑定 <@群友|M序号|微信昵称|wxid> <人物名>")
        except MemberError as error:
            ctx.reply(message, str(error))
