"""群聊成语接龙；全群共享一条接龙进度，空闲一段时间后自动结束。

进度写进按群共享的状态表，重启、重连或插件重载后可以接着上一局继续；
艾特机器人发的成语同样算答案，人格对话靠同一份状态知道当前接字。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from wxbot_framework.llm import LLMError
from wxbot_framework.members import SnapshotExpired
from wxbot_framework.mentions import strip_at_labels
from wxbot_framework.personality import game_reply
from wxbot_framework.text import FOUR_CHINESE, GROUP_CONTROL_COMMANDS, validate_mentions
from .judge import IdiomJudge
from wxbot_framework.commands import command_text, HELP_TEXT

HELP = HELP_TEXT
DEFAULT_IDLE_SECONDS = 1800
FEATURE = "idiom_chain"
USED_LIMIT = 200


def answer_text(text):
    """接龙答案的正文：去掉任意位置的艾特标签，与人格对话的判定口径保持一致。"""
    return strip_at_labels(text).strip()


@dataclass
class Chain:
    current: str
    used: tuple
    ready_at: float = 0
    ready_id: int = 0
    ready_sequence: int = 0
    ready_session: str = ""


@dataclass
class Game:
    opening: str
    chain: Chain
    updated_at: float = field(default_factory=time.monotonic)


class Plugin:
    def on_load(self, ctx):
        self.games = {}
        self.room_state = getattr(ctx, "room_state", None)
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
        # 答案去掉任意位置的艾特标签：群里常把“成语@机器人”和“@机器人 成语”都当答案发。
        if message.is_self and text not in GROUP_CONTROL_COMMANDS and not FOUR_CHINESE.fullmatch(answer_text(message.text)):
            return
        personality = ctx.personality(message)
        try:
            self._handle(message, ctx, personality)
        except SnapshotExpired:
            ctx.logger.info("接龙回复已取消：人格已切换")

    def _expire(self, ctx, room, now=None):
        """空闲超时的局直接作废，避免后来的四字消息被当成接龙。"""
        game = self.games.get(room)
        if game and (now if now is not None else time.monotonic()) - game.updated_at > self.idle_seconds:
            self._drop(ctx, room)
            return True
        return False

    def _game(self, room):
        """内存里的局；没有时尝试用上次运行留下的进度续上。"""
        game = self.games.get(room)
        if game is None:
            game = self._restore(room)
            if game is not None:
                self.games[room] = game
        return game

    def _restore(self, room):
        if self.room_state is None:
            return None
        try:
            state = self.room_state.get(room, FEATURE)
        except Exception:
            return None
        if not isinstance(state, dict):
            return None
        current, used = state.get("current"), state.get("used")
        if not isinstance(current, str) or not FOUR_CHINESE.fullmatch(current):
            return None
        words = tuple(word for word in used if isinstance(word, str) and FOUR_CHINESE.fullmatch(word)) \
            if isinstance(used, list) else ()
        if current not in words:
            return None
        try:
            saved = datetime.fromisoformat(state.get("updated_at"))
        except (TypeError, ValueError):
            return None
        if saved.tzinfo is None:
            saved = saved.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - saved).total_seconds()
        if age < 0 or age > self.idle_seconds:
            return None
        # 续局保留剩余空闲时间：重启本身不给这一局续命。
        return Game(words[0], Chain(current, words), time.monotonic() - age)

    def _save(self, ctx, room):
        """把当前进度写回共享状态；没有局就清掉，供人格对话和续局读取。"""
        if self.room_state is None:
            return
        game = self.games.get(room)
        try:
            if game is None:
                self.room_state.clear(room, FEATURE)
            else:
                self.room_state.put(room, FEATURE, {
                    "current": game.chain.current,
                    "used": list(game.chain.used[-USED_LIMIT:]),
                    "idle_seconds": self.idle_seconds,
                    "updated_at": datetime.now(timezone.utc).isoformat()})
        except Exception as error:  # 进度只用于续局，存不下不影响本轮接龙
            ctx.logger.warning("接龙进度保存失败：%s", error)

    def _drop(self, ctx, room):
        self.games.pop(room, None)
        self._save(ctx, room)

    @staticmethod
    def _members(ctx):
        """未配置成员目录时 PluginContext.members 会抛异常，这里统一降级为 None。"""
        try:
            return getattr(ctx, "members", None)
        except Exception:
            return None

    def _stamp_ready(self, ctx, room, message, chain):
        """记录发题完成时的水位：发题期间已到达的答案不能参与下一轮。"""
        chain.ready_at = time.monotonic()
        chain.ready_sequence = int(getattr(message, "sequence", 0) or 0)
        chain.ready_session = getattr(message, "session_id", "") or ""
        members = self._members(ctx)
        if members is not None and getattr(members, "history_enabled", False):
            try:
                chain.ready_id = int(members.latest_id(room))
            except Exception:
                chain.ready_id = 0

    def _is_new_answer(self, ctx, room, message, chain):
        """先按接收时钟划清发题边界；只有时间戳相同时才用 ID/sequence 兜底。"""
        if chain.ready_at and message.received_at != chain.ready_at:
            # 发题完成前到达的答案（包括记录线程晚提交、ID 反而更大的）一律不算下一轮。
            return message.received_at > chain.ready_at
        members = self._members(ctx)
        if members is not None and getattr(members, "history_enabled", False) and chain.ready_id:
            try:
                message_id = members.message_id(message)
            except Exception:
                message_id = None
            if message_id is not None:
                if message_id > chain.ready_id:
                    return True
                # 跨天清理会让 rowid 从 1 重新分配，ID 退回不代表消息旧。
                return message.received_at > chain.ready_at
        sequence = int(getattr(message, "sequence", 0) or 0)
        same_session = not chain.ready_session or message.session_id == chain.ready_session
        if sequence and chain.ready_sequence and same_session:
            return sequence > chain.ready_sequence
        # 断线重连后 sequence 会从 1 重新计数，只能靠接收时钟判断先后。
        return message.received_at > chain.ready_at

    def _handle(self, message, ctx, personality):
        text = command_text(message.text)
        judge = IdiomJudge(ctx.llm_for(message, personality=personality))
        guard = lambda: ctx.personality_guard(personality)
        if text == "/help":
            return
        room = message.chat_id
        self._expire(ctx, room)
        game = self._game(room)
        if text == "退出接龙":
            self._drop(ctx, room)
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
                chain = Chain(opening, (opening,))
                self._stamp_ready(ctx, room, message, chain)
                self.games[room] = Game(opening, chain)
                self._save(ctx, room)
            return
        if text == "合并接龙":
            if not game:
                ctx.reply(message, "请先发送“成语接龙”。", delivery_guard=guard)
                return
            ctx.reply(message, f"接龙进度全群共享，当前仍是：{game.chain.current}。", delivery_guard=guard)
            return
        answer = answer_text(message.text)
        if not game or not answer or answer.startswith("/") or not FOUR_CHINESE.fullmatch(answer):
            return
        chain = game.chain
        if not self._is_new_answer(ctx, room, message, chain):
            return  # 同一轮判定/发题期间到达的抢答不重复计入。
        if answer[0] != chain.current[-1]:
            return  # 与当前接字无关的四字消息按普通聊天处理，不打断群聊。
        name = self.member_names.get(room, {}).get(message.sender_id)
        try:
            verdict = judge.evaluate(chain.current, answer, chain.used)
        except LLMError as error:
            ctx.logger.warning("接龙判定失败：%s", error)
            if ctx.reply_at(message, f"大模型暂时无法判定，请稍后重试。当前成语：{chain.current}",
                            display_name=name, delivery_guard=guard):
                next_chain = Chain(chain.current, chain.used)
                self._stamp_ready(ctx, room, message, next_chain)
                game.chain = next_chain
                game.updated_at = time.monotonic()
                self._save(ctx, room)
            return
        following = (verdict.next_idiom or answer) if verdict.accepted else chain.current
        used = chain.used
        if verdict.accepted:
            used += (answer,)
            if verdict.next_idiom:
                used += (following,)
        if verdict.accepted and not verdict.next_idiom:
            response = (f"接得漂亮！{chain.current} → {answer}\n"
                        f"这轮我接不上，认输。大家继续接以“{answer[-1]}”开头的四字成语。")
        else:
            response = game_reply(personality.name, verdict.accepted, verdict.reason, chain.current, answer, following)
        if ctx.reply_at(message, response, display_name=name, delivery_guard=guard):
            next_chain = Chain(following, used)
            self._stamp_ready(ctx, room, message, next_chain)
            game.chain = next_chain
            game.updated_at = time.monotonic()
            self._save(ctx, room)
