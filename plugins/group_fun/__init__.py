"""手动触发的群聊趣味功能；只在显式命令或竞猜入口时回复。"""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import random
import time

from wxbot_framework.commands import command_text
from wxbot_framework.group_fun import GroupReporter, display, source_line, quiz_candidates
from wxbot_framework.group_fun_store import GroupFunStore, source_ids, short_text
from wxbot_framework.llm import LLMError
from wxbot_framework.members import BEIJING, MemberError, SnapshotExpired
from wxbot_framework.summary import split_text

QUIZ = "quote_quiz"
USAGE = ("/谁说的：开始5题原话竞猜；/猜 选项数字\n"
         "/谁说的 提示｜揭晓｜下一题｜结束｜状态\n"
         "/谁说的 排除我｜恢复我：仅设置自己的出题偏好\n"
         "/群报 [今天|昨天]：手动生成群报\n"
         "/群梗 关键词｜随机；/原话 编号\n"
         "/群梗收录 名称 | 说明 | 原话编号\n"
         "/群梗补充 编号 | 说明 | 原话编号\n"
         "/群梗修订 编号 | 说明 | 原话编号")
COMMANDS = {"/谁说的", "/猜", "/群报", "/群梗", "/群梗收录", "/群梗补充", "/群梗修订", "/原话"}


class Plugin:
    def on_load(self, ctx):
        self.members = ctx.members
        if not self.members.history_enabled or ctx.room_state is None:
            raise ValueError("群聊趣味功能需要 history_enabled=true 及群状态存储")
        self.state = ctx.room_state
        self.store = GroupFunStore(self.members)
        self.store.initialize()
        self.rng = random.Random()
        self.ready = {}
        self.report_limit = ctx.config.get("report_limit", 500)
        if type(self.report_limit) is not int or not 50 <= self.report_limit <= 2000:
            raise ValueError("group_fun.report_limit 应为 50–2000 的整数")

    def _send(self, ctx, message, text, guard=None):
        for part in split_text(text):
            if not ctx.reply(message, part, delivery_guard=guard):
                return False
        return True

    def _snapshot(self, message, limit=1000):
        anchor = self.members.message_id(message) or self.members.latest_id(message.chat_id) + 1
        return self.members.recent_snapshot(message.chat_id, limit=limit, before_id=anchor)

    @contextmanager
    def _guard(self, ctx, snapshot, personality, ids=(), *, quiz=False):
        with ctx.response_guard(snapshot, personality):
            excluded = self.store.excluded(snapshot.room) if quiz else set()
            for offset in range(0, len(ids), 100):
                batch = ids[offset:offset + 100]
                rows = self.store.rows(snapshot.room, batch, before_id=snapshot.anchor_id)
                if len(rows) != len(batch) or any(row["sender_id"] in excluded for row in rows):
                    raise SnapshotExpired("原话已经删除或退出出题")
            yield

    def on_message(self, message, ctx):
        if (message.type != 1 or not message.is_group or message.chat_id not in self.members.groups or
                message.data.get("bot_generated")):
            return
        text = command_text(message.text)
        text = {"这话谁说的": "/谁说的", "今日群报": "/群报"}.get(text, text)
        parts = text.split(maxsplit=1)
        if not parts or parts[0] not in COMMANDS:
            return
        command, argument = parts[0], parts[1].strip() if len(parts) > 1 else ""
        personality = ctx.personality(message)
        try:
            snapshot = self._snapshot(message)
            guard = lambda: self._guard(ctx, snapshot, personality)
            if command in {"/谁说的", "/猜"}:
                self._quiz(command, argument, message, ctx, snapshot, personality)
            elif command == "/群报":
                self._report(argument, message, ctx, snapshot, personality)
            elif command == "/原话":
                ids = source_ids(argument)
                rows = self.store.rows(message.chat_id, ids, before_id=snapshot.anchor_id)
                if len(rows) != len(ids):
                    raise MemberError("部分原话不存在或不属于本群")
                body, shown_ids = [], list(ids)
                for row in rows:
                    body.append(source_line(row, limit=600))
                    context = self.store.context(message.chat_id, row["id"], before_id=snapshot.anchor_id)
                    shown_ids.extend(item["id"] for item in context)
                    body.extend("相邻原话：" + source_line(item, limit=100) for item in context)
                self._send(ctx, message, "\n\n".join(body),
                           lambda: self._guard(ctx, snapshot, personality, list(dict.fromkeys(shown_ids))))
            elif command == "/群梗":
                self._meme(argument, message, ctx, snapshot, personality)
            else:
                fields = [item.strip() for item in argument.split("|")]
                if len(fields) != 3:
                    raise MemberError(USAGE)
                target, description, sources = fields
                kind = command.removeprefix("/群梗")
                if kind != "收录":
                    target = source_ids(target)
                    if len(target) != 1:
                        raise MemberError("每次只能修改一个群梗编号")
                    target = target[0]
                number = self.store.revise(message.chat_id, message.sender_id, target, description,
                                           source_ids(sources), before_id=snapshot.anchor_id, kind=kind)
                self._send(ctx, message, f"已{kind}群梗 #{number}。说明按群友提交记录，出处可用 /原话 核对。", guard)
        except MemberError as error:
            self._send(ctx, message, str(error))
        except SnapshotExpired:
            ctx.logger.info("群聊趣味回复已取消：原话或状态失效")
        except (LLMError, ValueError) as error:
            ctx.logger.warning("群聊趣味整理失败：%s", type(error).__name__)
            self._send(ctx, message, "暂时没能整理出来，请稍后重试；原有记录和竞猜进度保留。")

    def _report(self, argument, message, ctx, snapshot, personality):
        if argument not in {"", "今天", "昨天"}:
            raise MemberError("用法：/群报 [今天|昨天]")
        day = self.members._now().astimezone(BEIJING).date() - timedelta(days=argument == "昨天")
        start = datetime.combine(day, datetime.min.time(), BEIJING)
        rows = self.members.recent_rows(message.chat_id, limit=self.report_limit + 1,
                    since=start.astimezone(timezone.utc).isoformat(),
                    until=(start + timedelta(days=1)).astimezone(timezone.utc).isoformat(),
                    before_id=snapshot.anchor_id)
        truncated = len(rows) > self.report_limit
        rows = self.members.label_speakers(rows[-self.report_limit:], message.chat_id)
        snapshot = replace(snapshot, messages=tuple(rows))
        guard = lambda: self._guard(ctx, snapshot, personality, [row["id"] for row in rows])
        if not rows:
            self._send(ctx, message, f"{day} 没有可整理的群聊，今天就不硬凑了。", guard)
            return
        title = f"{day} 群报 · 本次 {len(rows)} 条聊天\n"
        if truncated:
            title += f"仅取当天最近 {self.report_limit} 条，并非全天完整记录。\n"
        title += "较长的单条消息仅取前600字；可用 /原话 编号 看上下文。\n\n"
        if len(rows) < 5:
            body = "聊天不多，翻两句原话：\n\n" + "\n\n".join(source_line(row) for row in rows[-2:])
        else:
            body = GroupReporter(ctx.llm_for(message, personality=personality)).report(
                rows, current=lambda: self.members.snapshot_is_current(snapshot))
        self._send(ctx, message, title + body, guard)

    def _meme(self, argument, message, ctx, snapshot, personality):
        if not argument:
            self._send(ctx, message, USAGE)
            return
        room = message.chat_id
        if argument == "随机":
            entries = self.store.memes(room)
            if not entries:
                self._send(ctx, message, "还没有人工收录的群梗。先用 /群梗 关键词 翻聊天出处。")
                return
            entry = self.rng.choice(entries)
        elif argument.startswith("#") and argument[1:].isdigit():
            entry, _ = self.store.meme(room, int(argument[1:]))
        else:
            entries = self.store.memes(room, short_text(argument, 40))
            exact = next((item for item in entries if item["name"] == argument), None)
            if not exact and len(entries) > 1:
                self._send(ctx, message, "找到多个群梗：\n" + "\n".join(
                    f"#{item['id']} {item['name']}" for item in entries[:10]) + "\n用 /群梗 #编号 查看。")
                return
            entry = exact or (entries[0] if entries else None)
        if entry:
            meme, revisions = self.store.meme(room, entry["id"])
            canonical = next(item for item in reversed(revisions) if item["kind"] != "补充")
            supplements = [item for item in revisions if item["kind"] == "补充"][-2:]
            lines, shown_ids = [f"群梗 #{meme['id']}：{meme['name']}"], []
            for item in [canonical, *supplements]:
                rows = self.store.rows(room, item["sources"], before_id=snapshot.anchor_id)
                who = display(self.members.display_name(item["editor"], room) or "群友")
                lines.append(f"群友{item['kind']}（{who}）：{item['description']}")
                lines.extend(source_line(row) for row in rows)
                shown_ids.extend(row["id"] for row in rows)
                if len(rows) != len(item["sources"]):
                    lines.append("部分出处已不可用，这条说明暂不能完整核验。")
            lines.append(f"共 {len(revisions)} 次收录/补充/修订；可继续 /群梗补充 {meme['id']} | 说明 | 原话编号")
            self._send(ctx, message, "\n\n".join(lines),
                       lambda: self._guard(ctx, snapshot, personality, list(dict.fromkeys(shown_ids))))
            return
        term = short_text(argument, 40)
        oldest = self.store.search(room, term, before_id=snapshot.anchor_id, oldest=True, limit=1)
        if not oldest:
            self._send(ctx, message, "当前本群存档里没找到这个说法，不编词源。")
            return
        latest = self.store.search(room, term, before_id=snapshot.anchor_id, limit=12)
        rows = list({row["id"]: row for row in [*oldest, *latest]}.values())
        lines = [f"“{term}”还没有人工词条。", "当前存档最早可检索到的记录：", source_line(oldest[0])]
        try:
            meaning, ids = GroupReporter(ctx.llm_for(message, personality=personality)).explain_meme(
                term, rows, current=lambda: self.members.snapshot_is_current(snapshot))
            lines.append("根据这些片段推测（未确认）：" + meaning)
            by_id = {row["id"]: row for row in rows}
            lines.extend(source_line(by_id[number]) for number in ids if number != oldest[0]["id"])
        except (LLMError, ValueError):
            lines.append("暂时无法整理含义，先保留能核对的原话。")
            lines.extend(source_line(row) for row in latest[-2:] if row["id"] != oldest[0]["id"])
        lines.append("这不一定是群梗真正的起源；可以用 /群梗收录 名称 | 说明 | 原话编号 补全。")
        ids = [row["id"] for row in rows]
        self._send(ctx, message, "\n\n".join(lines), lambda: self._guard(ctx, snapshot, personality, ids))

    def _quiz_state(self, room):
        state = self.state.get(room, QUIZ)
        if not isinstance(state, dict) or state.get("version") != 1:
            return None
        questions, index = state.get("questions"), state.get("index")
        if (not isinstance(questions, list) or not 1 <= len(questions) <= 5 or
                type(index) is not int or not 0 <= index < len(questions) or
                state.get("phase") not in {"question", "revealed", "void", "finished"} or
                not isinstance(state.get("scores"), dict) or not isinstance(state.get("guesses"), list) or
                len(state["guesses"]) > 100 or any(not isinstance(sender, str) for sender in state["guesses"]) or
                type(state.get("hinted")) is not bool):
            return None
        for item in questions:
            if (not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] <= 0 or
                    not isinstance(item.get("options"), list) or not 2 <= len(item["options"]) <= 4 or
                    any(not isinstance(sender, str) for sender in item["options"]) or
                    len(set(item["options"])) != len(item["options"])):
                return None
        if any(not isinstance(sender, str) or type(score) is not int or not 1 <= score <= 5
               for sender, score in state["scores"].items()):
            return None
        return state

    def _save_quiz(self, room, state):
        self.state.put(room, QUIZ, state)
        self.ready[room] = time.monotonic()

    def _question(self, room, state, anchor):
        question = state["questions"][state["index"]]
        rows = self.store.rows(room, [question["id"]], before_id=anchor)
        if not rows or rows[0]["sender_id"] in self.store.excluded(room) or rows[0]["sender_id"] not in question["options"]:
            return None, "这题的原话已不可用，发送 /谁说的 下一题 跳过。"
        row = rows[0]
        lines = [f"这话谁说的 · 第 {state['index'] + 1}/{len(state['questions'])} 题", f"“{row['text']}”"]
        for index, sender in enumerate(question["options"], 1):
            lines.append(f"{index}. {display(self.members.display_name(sender, room) or '未绑定群友')}")
        lines.append("/猜 数字，每人每题一次；答对+1。没人会就 /谁说的 揭晓。")
        return row, "\n".join(lines)

    def _scores(self, room, state):
        if not state["scores"]:
            return "这局还没人猜中，原话已经够精彩了。"
        return "本局猜中次数：\n" + "\n".join(
            f"{display(self.members.display_name(sender, room) or '群友')}：{score}"
            for sender, score in sorted(state["scores"].items(), key=lambda item: -item[1]))

    def _quiz(self, command, argument, message, ctx, snapshot, personality):
        room = message.chat_id
        if command == "/谁说的" and argument in {"排除我", "恢复我"}:
            self.store.set_excluded(room, message.sender_id, argument == "排除我")
            self._send(ctx, message, "已排除你的消息，当前和后续题目都会检查。" if argument == "排除我" else "已恢复使用你的消息出题。")
            return
        state = self._quiz_state(room)
        if command == "/谁说的" and argument in {"", "开始"} and (not state or state["phase"] == "finished"):
            since = (self.members._now() - timedelta(days=30)).astimezone(timezone.utc).isoformat()
            rows = self.members.label_speakers(self.members.recent_rows(room, limit=2000,
                        before_id=snapshot.anchor_id, since=since), room)
            candidates = quiz_candidates(rows, self.store.excluded(room))
            names = {}
            for row in candidates:
                names.setdefault(row["speaker_name"], set()).add(row["sender_id"])
            candidates = [row for row in candidates if not row["speaker_name"].startswith("未绑定成员")
                          and len(names[row["speaker_name"]]) == 1]
            authors = list(dict.fromkeys(row["sender_id"] for row in candidates))
            if len(candidates) < 3 or len(authors) < 2:
                self._send(ctx, message, "最近30天还没找到至少3条、来自至少2个人的合适原话，先继续聊，素材够了再开。")
                return
            self.rng.shuffle(candidates)
            questions = []
            for row in candidates[:5]:
                others = [sender for sender in authors if sender != row["sender_id"]]
                options = [row["sender_id"], *self.rng.sample(others, min(3, len(others)))]
                self.rng.shuffle(options)
                questions.append({"id": row["id"], "options": options})
            state = {"version": 1, "questions": questions, "index": 0, "phase": "question",
                     "scores": {}, "guesses": [], "hinted": False}
            row, body = self._question(room, state, snapshot.anchor_id)
            if row and self._send(ctx, message, body,
                    lambda: self._guard(ctx, snapshot, personality, [row["id"]], quiz=True)):
                self._save_quiz(room, state)
            return
        if not state:
            self._send(ctx, message, "先发 /谁说的 开始一局。\n" + USAGE)
            return
        if command == "/谁说的" and argument in {"帮助", "help"}:
            self._send(ctx, message, USAGE)
            return
        if command == "/谁说的" and argument == "结束":
            if self._send(ctx, message, "这局收工！\n" + self._scores(room, state)):
                self._save_quiz(room, {**state, "phase": "finished"})
            return
        if state["phase"] == "finished":
            self._send(ctx, message, "这局已经结束。\n" + self._scores(room, state) + "\n发送 /谁说的 再开一局。")
            return
        if command == "/谁说的" and argument == "下一题":
            if state["phase"] == "question":
                self._send(ctx, message, "先猜这一题，或 /谁说的 揭晓 再继续。")
                return
            state = {**state, "index": state["index"] + 1, "phase": "question", "guesses": [], "hinted": False}
            while state["index"] < len(state["questions"]):
                row, body = self._question(room, state, snapshot.anchor_id)
                if row:
                    if self._send(ctx, message, body,
                            lambda: self._guard(ctx, snapshot, personality, [row["id"]], quiz=True)):
                        self._save_quiz(room, state)
                    return
                state["index"] += 1
            state["index"] = len(state["questions"]) - 1
            if self._send(ctx, message, "题目都翻完了！\n" + self._scores(room, state)):
                self._save_quiz(room, {**state, "phase": "finished"})
            return
        row, body = self._question(room, state, snapshot.anchor_id)
        if not row:
            if self._send(ctx, message, body):
                self._save_quiz(room, {**state, "phase": "void"})
            return
        guard = lambda: self._guard(ctx, snapshot, personality, [row["id"]], quiz=True)
        if command == "/谁说的" and argument in {"", "开始", "状态"}:
            self._send(ctx, message, body if state["phase"] == "question" else "本题已揭晓，发送 /谁说的 下一题。", guard)
            return
        if state["phase"] != "question":
            self._send(ctx, message, "本题已揭晓，发送 /谁说的 下一题。")
            return
        if message.received_at <= self.ready.get(room, 0):
            return
        options = state["questions"][state["index"]]["options"]
        if command == "/谁说的" and argument == "提示":
            if state["hinted"]:
                self._send(ctx, message, "这题提示已经用过，可以猜一下或揭晓。")
                return
            wrong = next(index for index, sender in enumerate(options, 1) if sender != row["sender_id"])
            if self._send(ctx, message, f"排除一个：不是选项 {wrong}。这题还有机会。", guard):
                self._save_quiz(room, {**state, "hinted": True})
            return
        correct = False
        if command == "/猜":
            if not argument.isascii() or not argument.isdigit() or not 1 <= int(argument) <= len(options):
                raise MemberError(f"用法：/猜 1–{len(options)}，填写选项数字")
            if message.sender_id in state["guesses"]:
                self._send(ctx, message, "这题你已经猜过，留给其他人试试。")
                return
            correct = options[int(argument) - 1] == row["sender_id"]
            if not correct:
                if len(state["guesses"]) >= 100:
                    self._send(ctx, message, "这题已经有100人猜过了，直接 /谁说的 揭晓 看原话吧。", guard)
                    return
                if self._send(ctx, message, "这回没猜中，其他人继续！不会可以 /谁说的 提示 或揭晓。", guard):
                    self._save_quiz(room, {**state, "guesses": [*state["guesses"], message.sender_id]})
                return
        elif argument not in {"揭晓", "跳过", "认领"}:
            raise MemberError(USAGE)
        elif argument == "认领" and message.sender_id != row["sender_id"]:
            raise MemberError("这条原话不是你发的，要猜请用 /猜 数字。")
        own = message.sender_id == row["sender_id"]
        scores = dict(state["scores"])
        if correct and not own:
            scores[message.sender_id] = scores.get(message.sender_id, 0) + 1
        title = "猜中了，+1！" if correct and not own else "本人认领，不计分。" if own and (correct or argument == "认领") else "揭晓这题："
        lines = [title, source_line(row)]
        context = self.store.context(room, row["id"], before_id=snapshot.anchor_id, excluded=self.store.excluded(room))
        lines.extend("相邻原话：" + source_line(item, limit=70) for item in context)
        finished = state["index"] == len(state["questions"]) - 1
        updated = {**state, "scores": scores, "phase": "finished" if finished else "revealed"}
        lines.append(self._scores(room, updated))
        lines.append("这局收工，发送 /谁说的 再来。" if finished else "发送 /谁说的 下一题 继续。")
        revealed_ids = [row["id"], *[item["id"] for item in context]]
        if self._send(ctx, message, "\n\n".join(lines),
                      lambda: self._guard(ctx, snapshot, personality, revealed_ids, quiz=True)):
            self._save_quiz(room, updated)
