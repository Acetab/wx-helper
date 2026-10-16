"""按指定条数或今天的范围总结，默认跨天最近200条。"""
from dataclasses import replace
from datetime import datetime

from wxbot_framework.commands import command_text, effective_text
from wxbot_framework.llm import LLMError
from wxbot_framework.members import BEIJING, MemberError, SnapshotExpired
from wxbot_framework.summary import ChatSummarizer, split_text

USAGE = "用法：/总结群聊 [1—5000条或今天]；/总结 昵称 [1—5000条或今天]"


def parse(text):
    parts = command_text(text).split(maxsplit=1)
    if not parts or parts[0] not in ("/总结", "/总结群聊"):
        return None
    group = parts[0] == "/总结群聊"
    argument = parts[1].strip() if len(parts) > 1 else ""
    scope, count, name = "recent", 200, None
    if group:
        token = argument
    else:
        if not argument:
            raise MemberError(USAGE)
        tail = argument.rsplit(maxsplit=1)
        if len(tail) == 2 and (tail[1].isdigit() or tail[1] == "今天"):
            name, token = tail
        else:
            name, token = argument, ""
    if token == "今天":
        scope = "today"
    elif token:
        if not token.isdigit() or not 1 <= int(token) <= 5000:
            raise MemberError(USAGE)
        count = int(token)
    return group, name, scope, count


class Plugin:
    def on_load(self, ctx):
        self.members = ctx.members
        if not self.members.history_enabled:
            raise ValueError("聊天总结需要 history_enabled=true")
        self.summarizer = ChatSummarizer(ctx.llm, chunk_chars=ctx.config.get("chunk_chars", 16000))

    def on_message(self, message, ctx):
        if (message.type != 1 or not message.is_group or message.chat_id not in self.members.groups or
                message.data.get("bot_generated")):
            return
        try:
            parsed = parse(message.text)
            if parsed is None:
                return
            group, name, scope, count = parsed
            anchor = self.members.message_id(message) or self.members.latest_id(message.chat_id) + 1
            if scope == "today":
                snapshot = (self.members.today_group_snapshot(message.chat_id) if group else
                            self.members.today_snapshot(name, message.chat_id))
                snapshot = replace(snapshot, anchor_id=anchor, messages=tuple(row for row in snapshot.messages
                    if row["id"] < anchor and effective_text(row["text"])))
            else:
                snapshot = self.members.recent_snapshot(message.chat_id, name=name, limit=count, before_id=anchor)
        except MemberError as error:
            ctx.reply(message, str(error) + ("。请本人先发送 /绑定 昵称。" if str(error) != USAGE else ""))
            return
        rows = snapshot.messages
        personality = ctx.personality(message)
        guard = lambda: ctx.response_guard(snapshot, personality)
        try:
            if not rows:
                ctx.reply(message, "所选范围内没有可总结的聊天记录。", delivery_guard=guard)
                return
            summarizer = ChatSummarizer(ctx.llm_for(message, personality=personality),
                                       chunk_chars=self.summarizer.chunk_chars)
            summary = summarizer.summarize(snapshot, rows,
                current=lambda: self.members.snapshot_is_current(snapshot))
            start, end = [datetime.fromisoformat(row["received_at"]).astimezone(BEIJING).strftime("%Y-%m-%d %H:%M")
                          for row in (rows[0], rows[-1])]
            label = "今日" if scope == "today" else "最近"
            target = "群聊总结" if group else snapshot.member.name + " 聊天总结"
            participants = f"，{len({row['sender_id'] for row in rows})}人" if group else ""
            title = f"{label}{target}（实际{len(rows)}条{participants}）\n{start} — {end}\n"
            for part in split_text(title + summary):
                if not ctx.reply(message, part, delivery_guard=guard):
                    raise RuntimeError("原生文本发送未成功")
            ctx.logger.info("聊天总结完成：room=%s records=%d", message.chat_id, len(rows))
        except SnapshotExpired:
            ctx.logger.info("聊天总结已取消：上下文已重置或原始记录失效")
        except LLMError as error:
            ctx.logger.warning("聊天总结失败：%s", error)
            try:
                ctx.reply(message, "暂时无法生成聊天总结，请稍后重试。", delivery_guard=guard)
            except SnapshotExpired:
                pass
