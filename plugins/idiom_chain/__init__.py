"""群聊成语接龙；全群共享一条接龙进度，空闲一段时间后自动结束。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from wxbot_framework.llm import LLMError
from wxbot_framework.members import SnapshotExpired
from wxbot_framework.personality import game_reply
from wxbot_framework.text import FOUR_CHINESE, GROUP_CONTROL_COMMANDS, validate_mentions
from .judge import IdiomJudge
from wxbot_framework.commands import command_text, HELP_TEXT

HELP = HELP_TEXT
DEFAULT_IDLE_SECONDS = 1800


@dataclass
class Chain:
    current: str
    used: tuple
    ready_at: float = 0


@dataclass
class Game:
    opening: str
    chain: Chain
    updated_at: float = field(default_factory=time.monotonic)


class Plugin:
    def on_load(self, ctx):
        self.judge = IdiomJudge(ctx.llm)
        self.games = {}
        self.session_id = None
        self.member_names = ctx.config.get("member_names", {})
        if not isinstance(self.member_names, dict):
            raise ValueError("idiom_chain.member_names 应为按群 ID 分类的昵称配置")
        for room, names in self.member_names.items():
            if not isinstance(names, dict):
                raise ValueError("群昵称配置必须是 JSON 对象")
            for member, name in names.items():
                validate_mentions(room, [member])
                if not isinstance(name, str) or not name.strip() or len(name) > 64 or any(ch in name for ch in "\n\r\0"):
                    raise ValueError("群昵称应为 1–64 字符的单行文本")
        idle = ctx.config.get("idle_seconds", DEFAULT_IDLE_SECONDS)
        if isinstance(idle, bool) or not isinstance(idle, int) or not 60 <= idle <= 86400:
            raise ValueError("idiom_chain.idle_seconds 应为 60–86400 的整数")
        self.idle_seconds = idle

    def on_unload(self, ctx):
        self.games.clear()

    def on_message(self, message, ctx):
        if message.type != 1 or not message.is_group:
            return
        text = command_text(message.text)
        if message.data.get("is_at_self") and text not in GROUP_CONTROL_COMMANDS:
            return
        if message.is_self and text not in GROUP_CONTROL_COMMANDS and not FOUR_CHINESE.fullmatch(text):
            return
        personality = ctx.personality(message)
        try:
            self._handle(message, ctx, personality)
        except SnapshotExpired:
            ctx.logger.info("接龙回复已取消：人格已切换")

    def _expire(self, room, now=None):
        """空闲超时的局直接作废，避免后来的四字消息被当成接龙。"""
        game = self.games.get(room)
        if game and (now if now is not None else time.monotonic()) - game.updated_at > self.idle_seconds:
            self.games.pop(room, None)
            return True
        return False

    def _handle(self, message, ctx, personality):
        text = command_text(message.text)
        judge = IdiomJudge(ctx.llm_for(message, personality=personality))
        guard = lambda: ctx.personality_guard(personality)
        if self.session_id != message.session_id:
            self.games.clear()
            self.session_id = message.session_id
        if text == "/help":
            return
        self._expire(message.chat_id)
        game = self.games.get(message.chat_id)
        if text == "退出接龙":
            self.games.pop(message.chat_id, None)
            ctx.reply(message, "已退出接龙。", delivery_guard=guard)
            return
        if text == "成语接龙":
            if game:
                ctx.reply(message, f"当前成语：{game.chain.current}，请接以“{game.chain.current[-1]}”开头的四字成语。",
                          delivery_guard=guard)
                return
            try:
                opening = judge.opening()
            except LLMError as error:
                ctx.logger.warning("接龙开场失败：%s", error)
                ctx.reply(message, "暂时无法向大模型获取成语，请稍后再发“成语接龙”。", delivery_guard=guard)
                return
            reply = f"{opening}\n请接以“{opening[-1]}”开头的四字成语，发送“退出接龙”结束。"
            if ctx.reply(message, reply, delivery_guard=guard):
                self.games[message.chat_id] = Game(opening, Chain(opening, (opening,), time.monotonic()))
            return
        if text == "合并接龙":
            if not game:
                ctx.reply(message, "请先发送“成语接龙”。", delivery_guard=guard)
                return
            ctx.reply(message, f"接龙进度全群共享，当前仍是：{game.chain.current}。", delivery_guard=guard)
            return
        if not game or not text or text.startswith("/") or not FOUR_CHINESE.fullmatch(text):
            return
        chain = game.chain
        if message.received_at <= chain.ready_at:
            return  # 同一轮判定/发题期间到达的抢答不重复计入。
        if text[0] != chain.current[-1]:
            return  # 与当前接字无关的四字消息按普通聊天处理，不打断群聊。
        name = self.member_names.get(message.chat_id, {}).get(message.sender_id)
        try:
            verdict = judge.evaluate(chain.current, text, chain.used)
        except LLMError as error:
            ctx.logger.warning("接龙判定失败：%s", error)
            if ctx.reply_at(message, f"大模型暂时无法判定，请稍后重试。当前成语：{chain.current}",
                            display_name=name, delivery_guard=guard):
                game.chain = Chain(chain.current, chain.used, time.monotonic())
                game.updated_at = time.monotonic()
            return
        following = verdict.next_idiom if verdict.accepted else chain.current
        used = chain.used + (text, following) if verdict.accepted else chain.used
        response = game_reply(personality.name, verdict.accepted, verdict.reason, chain.current, text, following)
        if ctx.reply_at(message, response, display_name=name, delivery_guard=guard):
            game.chain = Chain(following, used, time.monotonic())
            game.updated_at = time.monotonic()
