"""真正艾特机器人后，用本群人格回复当前发言者。"""
from wxbot_framework.dialogue import Dialogue
from wxbot_framework.llm import LLMError
from wxbot_framework.members import SnapshotExpired
from wxbot_framework.personality import personality_text
from wxbot_framework.summary import split_text
from wxbot_framework.commands import is_control


class Plugin:
    def on_load(self, ctx):
        self.members = ctx.members
        if not self.members.history_enabled:
            raise ValueError("艾特上下文对话需要 history_enabled: true")
        ctx.llm  # 启动时核对配置，不请求接口。

    def on_message(self, message, ctx):
        if (message.type != 1 or not message.is_group or message.chat_id not in self.members.groups or
                "WXBOT_AT_CHAT_PROBE" in message.text or
                message.data.get("bot_generated") or is_control(message.text) or not message.data.get("is_at_self") or
                self.members.account not in message.data.get("at_members", ())):
            return
        personality = ctx.personality(message)
        guard = lambda: ctx.personality_guard(personality)
        try:
            snapshot = self.members.conversation_snapshot(message)
            guard = lambda: ctx.response_guard(snapshot, personality)
            entity = ctx.knowledge.member_entity(message.chat_id, message.sender_id)
            own = ctx.knowledge.search(message.chat_id, "", entity_id=entity["id"], limit=3, char_limit=1500)
            relevant = ctx.knowledge.search(message.chat_id, message.text, limit=5, char_limit=2500)
            knowledge = list({item["id"]: item for item in own + relevant}.values())
            style = ctx.knowledge.style(message.chat_id, message.sender_id)
            reply = personality_text(Dialogue(ctx.llm_for(message, personality=personality)).reply(snapshot,
                                     knowledge=knowledge, style=style),
                                     personality.name)
            if self.members.binding_reminder_due(message.chat_id, message.sender_id):
                reply += "\n可发送 /绑定 昵称，让机器人记住本群称呼。"
            display = self.members.display_name(message.sender_id, message.chat_id) or snapshot.speaker_name
            budget = 1023 - len(("@" + display + "\u2005\n").encode("utf-8"))
            for part in split_text(reply, byte_limit=budget):
                if not ctx.reply_at(message, part, display_name=display, delivery_guard=guard):
                    raise RuntimeError("原生艾特发送未成功")
            ctx.logger.info("艾特对话完成：room=%s persona=%s context=%d", message.chat_id,
                            personality.name, len(snapshot.messages))
        except SnapshotExpired:
            ctx.logger.info("艾特对话已取消：记录或人格状态已变更")
        except LLMError as error:
            ctx.logger.warning("艾特对话失败：%s", error)
            try:
                ctx.reply(message, "暂时无法回复，请稍后再艾特我。", delivery_guard=guard)
            except SnapshotExpired:
                pass
