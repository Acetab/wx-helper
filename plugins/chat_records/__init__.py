"""独立处理清空，避免排在耗时的模型请求后面。"""
from wxbot_framework.commands import command_text
from wxbot_framework.controls import prepare_reset
class Plugin:
    def on_load(self, ctx):
        self.members = ctx.members

    def on_message(self, message, ctx):
        if message.type != 1 or not message.is_group or message.chat_id not in self.members.groups:
            return
        words = command_text(message.text).split(maxsplit=1)
        if not words or words[0] != "/清空":
            return
        if len(words) != 1:
            ctx.reply(message, "用法：/清空")
            return
        if not message.data.get("context_reset") and not message.data.get("context_error"):
            message = prepare_reset(message, self.members)
        if message.data.get("context_error"):
            ctx.reply(message, message.data["context_error"])
            return
        ctx.reply(message, "已重置本群对话上下文，待发旧回复已取消；聊天原文和长期知识仍保留。")
