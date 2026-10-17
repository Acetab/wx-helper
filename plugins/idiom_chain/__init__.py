"""群聊成语接龙；全群共享一条接龙进度，空闲一段时间后自动结束。

进度写进按群共享的状态表，重启、重连或插件重载后可以接着上一局继续；
艾特机器人发的成语同样算答案，人格对话靠同一份状态知道当前接字。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from wxbot_framework.llm import LLMError
from wxbot_framework.members import SnapshotExpired
from wxbot_framework.mentions import strip_at_labels
from wxbot_framework.personality import game_reply
from wxbot_framework.text import FOUR_CHINESE, GROUP_CONTROL_COMMANDS, validate_mentions
from .judge import IdiomJudge
from wxbot_framework.commands import command_text, HELP_TEXT, GAME_COMMANDS

HELP = HELP_TEXT
DEFAULT_IDLE_SECONDS = 1800
FEATURE = "idiom_chain"
USED_LIMIT = 200
MIN_FOLLOWUPS = 3
DEFAULT_GOAL = 10


def answer_text(text):
    """接龙答案的正文：去掉任意位置的艾特标签，与人格对话的判定口径保持一致。"""
    return strip_at_labels(text).strip()


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
    rounds: int = 0
    goal: int = DEFAULT_GOAL
    scores: dict = field(default_factory=dict)
    hints_left: int = 2
    swaps_left: int = 1
    last_player: str = ""
    hint_word: str = ""


class Plugin:
    def on_load(self, ctx):
        self.judge = IdiomJudge(ctx.llm)
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
        goal = ctx.config.get("target_rounds", DEFAULT_GOAL)
        if type(goal) is not int or not 3 <= goal <= 30:
            raise ValueError("idiom_chain.target_rounds 应为 3–30 的整数")
        self.goal = goal

    def on_unload(self, ctx):
        self.games.clear()

    def on_message(self, message, ctx):
        if message.type != 1 or not message.is_group:
            return
        text = command_text(message.text)
        # 答案去掉任意位置的艾特标签：群里常把“成语@机器人”和“@机器人 成语”都当答案发。
        if (message.is_self and text not in GROUP_CONTROL_COMMANDS and
                text.split(maxsplit=1)[:1] != ["/接龙"] and not FOUR_CHINESE.fullmatch(answer_text(message.text))):
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
        game = Game(words[0], Chain(current, words), time.monotonic() - age, goal=self.goal)
        if state.get("version") == 2:
            rounds, goal = state.get("rounds"), state.get("goal")
            hints, swaps, scores = state.get("hints_left"), state.get("swaps_left"), state.get("scores")
            if (type(rounds) is not int or type(goal) is not int or not 3 <= goal <= 30 or
                    not 0 <= rounds < goal or type(hints) is not int or not 0 <= hints <= 2 or
                    type(swaps) is not int or not 0 <= swaps <= 1 or not isinstance(scores, dict) or
                    any(not isinstance(key, str) or type(value) is not int or value <= 0
                        for key, value in scores.items()) or sum(scores.values()) != rounds):
                return None
            game.rounds, game.goal, game.scores = rounds, goal, dict(scores)
            game.hints_left, game.swaps_left = hints, swaps
            last_player, hint_word = state.get("last_player", ""), state.get("hint_word", "")
            if isinstance(last_player, str) and last_player in words and last_player[-1] == current[0]:
                game.last_player = last_player
            if hint_word in self.judge.lexicon.continuations(current, words):
                game.hint_word = hint_word
        return game

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
                    "version": 2,
                    "current": game.chain.current,
                    "used": list(game.chain.used[-USED_LIMIT:]),
                    "rounds": game.rounds, "goal": game.goal, "scores": game.scores,
                    "hints_left": game.hints_left, "swaps_left": game.swaps_left,
                    "last_player": game.last_player, "hint_word": game.hint_word,
                    "idle_seconds": self.idle_seconds,
                    "updated_at": datetime.now(timezone.utc).isoformat()})
        except Exception as error:  # 进度只用于续局，存不下不影响本轮接龙
            ctx.logger.warning("接龙进度保存失败：%s", error)

    def _drop(self, ctx, room):
        self.games.pop(room, None)
        self._save(ctx, room)

    @staticmethod
    def _progress(game):
        return (f"全群进度 {game.rounds}/{game.goal} · {len(game.scores)} 人参与\n"
                f"接龙提示 {game.hints_left} 次 · 接龙换题 {game.swaps_left} 次")

    def _status(self, game):
        return (f"当前成语：{game.chain.current}，请接以“{game.chain.current[-1]}”开头的四字成语。\n"
                f"{self._progress(game)}")

    def _commit(self, ctx, room, game):
        game = replace(game, updated_at=time.monotonic(),
                       chain=replace(game.chain, ready_at=time.monotonic()))
        self.games[room] = game
        self._save(ctx, room)

    def _assist(self, text, message, ctx, game, judge, guard):
        if message.received_at <= game.chain.ready_at:
            return
        current, used = game.chain.current, game.chain.used
        if text == "接龙提示":
            if game.hints_left == 0:
                ctx.reply(message, "本局两次提示已经用完，还可以试试“接龙换题”。", delivery_guard=guard)
                return
            choices = judge.lexicon.continuations(current, used)
            if not choices:
                ctx.reply(message, "常用词池里没有可用提示，可以换题或发送“接龙认输”结束本局。", delivery_guard=guard)
                return
            word = game.hint_word if game.hint_word in choices else choices[0]
            mask = word[:2] + "□" + word[-1] if game.hint_word == word else word[0] + "□□" + word[-1]
            updated = replace(game, hints_left=game.hints_left - 1, hint_word=word)
            response = f"给你留点发挥空间：{mask}\n这是本局还没用过的成语。提示剩余 {updated.hints_left} 次。"
        else:
            if game.swaps_left == 0:
                ctx.reply(message, "本局换题机会已经用完，可以试试“接龙提示”。", delivery_guard=guard)
                return
            # 开场允许换开场；续局缺少上一位玩家记录时不伪造衔接。
            if game.last_player:
                word = judge.lexicon.choose(game.last_player, used, MIN_FOLLOWUPS)
            elif game.rounds == 0 and len(used) == 1:
                options = judge.lexicon.playable(sorted(judge.lexicon.words), used, MIN_FOLLOWUPS)
                word = judge.lexicon.rng.choice(options) if options else ""
            else:
                word = ""
            if not word:
                ctx.reply(message, "没有其他符合规则的题目，这次不扣换题机会。可以提示或认输。", delivery_guard=guard)
                return
            updated = replace(game, chain=Chain(word, (*used, word)), swaps_left=0, hint_word="")
            response = f"换一道，已经接对的次数保留！\n{self._status(updated)}"
        if ctx.reply(message, response, delivery_guard=guard):
            self._commit(ctx, message.chat_id, updated)

    def _handle(self, message, ctx, personality):
        text = command_text(message.text)
        judge = IdiomJudge(ctx.llm_for(message, personality=personality),
                           lexicon=self.judge.lexicon, minimum=MIN_FOLLOWUPS)
        guard = lambda: ctx.personality_guard(personality)
        if text == "/help":
            return
        room = message.chat_id
        self._expire(ctx, room)
        game = self._game(room)
        if text in {"退出接龙", "接龙认输"}:
            response = "已退出接龙。" if text == "退出接龙" else "本局结束，下局再战！"
            if game:
                response += f"\n本局接对 {game.rounds}/{game.goal} 次，{len(game.scores)} 人参与。"
                examples = judge.lexicon.continuations(game.chain.current, game.chain.used)[:2]
                if text == "接龙认输" and examples:
                    response += "\n刚才可以接：" + "、".join(examples)
                response += "\n发送“成语接龙”再开一局。"
            if ctx.reply(message, response, delivery_guard=guard):
                self._drop(ctx, room)
            return
        if text == "成语接龙":
            if game:
                ctx.reply(message, self._status(game), delivery_guard=guard)
                return
            opening = judge.opening()
            new_game = Game(opening, Chain(opening, (opening,)), goal=self.goal)
            reply = (f"全群一起上！接对 {self.goal} 次就通关，答错可以重试。\n{self._status(new_game)}\n"
                     "同字衔接，本局不重复；我每次出题至少留 3 个常用答案。\n"
                     "直接发成语，或用“/接龙 成语”明确作答；发送“接龙认输”或“退出接龙”结束。")
            if ctx.reply(message, reply, delivery_guard=guard):
                self._commit(ctx, room, new_game)
            return
        explicit = text.split(maxsplit=1)[:1] == ["/接龙"]
        if not game and (text in GAME_COMMANDS or explicit):
            ctx.reply(message, "请先发送“成语接龙”。", delivery_guard=guard)
            return
        if text in {"合并接龙", "接龙状态"}:
            if not game:
                ctx.reply(message, "请先发送“成语接龙”。", delivery_guard=guard)
                return
            ctx.reply(message, self._status(game), delivery_guard=guard)
            return
        if text in {"接龙提示", "接龙换题"}:
            self._assist(text, message, ctx, game, judge, guard)
            return
        answer = answer_text(message.text)
        if explicit:
            parts = text.split(maxsplit=1)
            answer = parts[1].strip() if len(parts) > 1 else ""
        if not game or (not explicit and (not answer or not FOUR_CHINESE.fullmatch(answer))):
            return
        chain = game.chain
        if message.received_at <= chain.ready_at:
            return  # 同一轮判定/发题期间到达的抢答不重复计入。
        if not explicit and answer[0] != chain.current[-1]:
            return  # 与当前接字无关的四字消息按普通聊天处理，不打断群聊。
        name = self.member_names.get(room, {}).get(message.sender_id)
        try:
            verdict = judge.evaluate(chain.current, answer, chain.used)
        except LLMError as error:
            ctx.logger.warning("接龙判定失败：%s", error)
            if ctx.reply_at(message, f"大模型暂时无法判定，请稍后重试。当前成语：{chain.current}",
                            display_name=name, delivery_guard=guard):
                self._commit(ctx, room, game)
            return
        if not verdict.accepted:
            response = game_reply(personality.name, False, verdict.reason, chain.current, answer, chain.current)
            response += f"\n不扣进度，换一个再试。{self._progress(game)}"
            if ctx.reply_at(message, response, display_name=name, delivery_guard=guard):
                self._commit(ctx, room, game)
            return
        scores = dict(game.scores)
        scores[message.sender_id] = scores.get(message.sender_id, 0) + 1
        updated = replace(game, rounds=game.rounds + 1, scores=scores, last_player=answer, hint_word="")
        won = updated.rounds >= updated.goal or not verdict.next_idiom
        if won:
            title = "全群通关！这局是你们赢了。" if updated.rounds >= updated.goal else "绝杀！这局你们赢了。"
            response = f"{title}\n{chain.current} → {answer}"
            if not verdict.next_idiom and updated.rounds < updated.goal:
                response += "\n我的常用词池已找不到还能留足 3 个答案的接法，这轮认输。"
            response += (f"\n本局接对 {updated.rounds}/{updated.goal} 次，{len(scores)} 人参与；"
                         f"你贡献了 {scores[message.sender_id]} 次。\n发送“成语接龙”再开一局。")
        else:
            updated = replace(updated, chain=Chain(verdict.next_idiom, (*chain.used, answer, verdict.next_idiom)))
            response = game_reply(personality.name, True, verdict.reason, chain.current, answer, verdict.next_idiom)
            if updated.rounds == updated.goal - 1:
                response += "\n最后一棒！再接对一次就通关。"
            elif updated.rounds == (updated.goal + 1) // 2:
                response += "\n已经过半，继续！"
            response += f"\n本轮 +1，你已贡献 {scores[message.sender_id]} 次。\n{self._progress(updated)}"
        if ctx.reply_at(message, response, display_name=name, delivery_guard=guard):
            if won:
                self._drop(ctx, room)
            else:
                self._commit(ctx, room, updated)
