"""统一固定语气；保留插件调用接口，不再保存或切换人格。"""
from dataclasses import dataclass, replace
from types import MappingProxyType

from .commands import command_text
from .members import SnapshotExpired

FIXED_NAME = "熟人损友"
FIXED_PROMPT = """你的固定语气是熟人损友：口语自然，懂群梗，能适度调侃，回答要有实际内容。
不使用客服腔，不机械复读梗，不把别人真实的困境当笑料，不用家属、疾病或现实创伤攻击对方。
根据表达习惯理解反话和省略，但保持自己的语气，不冒充任何群友，不假装亲历未提供的往事。
普通聊天简洁；知识问答、解释和总结按内容展开，不设统一20字限制。
事实、来源和功能规则优先：玩笑必须作为玩笑，转述不能变成已证实事实，不能为了接梗编造经历。
JSON是内部传输格式；正文适合直接在微信阅读，避免Markdown表格和代码围栏。"""
FIXED_NOTICE = "已固定为熟人损友风格，不再切换人格。"
PROMPTS = {}


@dataclass(frozen=True)
class PersonalityState:
    room: str
    name: str = FIXED_NAME
    generation: int = 0


class PersonalityExpired(SnapshotExpired):
    pass


def system_with_personality(system, state):
    return FIXED_PROMPT + "\n功能要求：\n" + system


def personality_text(text, name=FIXED_NAME):
    return text.strip()


class PersonalityLLM:
    def __init__(self, client, state, *, current):
        self.client, self.state, self.current = client, state, current

    def complete_json(self, system, user, **options):
        if not self.current():
            raise PersonalityExpired("当前回复已失效")
        result = self.client.complete_json(system_with_personality(system, self.state), user, **options)
        if not self.current():
            raise PersonalityExpired("当前回复已失效")
        return result

    def complete_text(self, system, user, **options):
        if not self.current():
            raise PersonalityExpired("当前回复已失效")
        result = self.client.complete_text(system_with_personality(system, self.state), user, **options)
        if not self.current():
            raise PersonalityExpired("当前回复已失效")
        return result


def prepare_switch(message, store):
    if (store is None or message.type != 1 or not message.is_group or message.chat_id not in store.groups or
            message.data.get("bot_generated") or not message.data.get("dispatch_to_plugins", True)):
        return message
    if command_text(message.text).split(maxsplit=1)[:1] != ["/切换人格"]:
        return message
    return replace(message, data=MappingProxyType({**message.data, "personality_error": FIXED_NOTICE}))


def switch_ack(name):
    return FIXED_NOTICE


def game_reply(name, accepted, reason, previous, word, following):
    if accepted:
        return f"接得漂亮！{previous} → {word}\n我接：{following}\n请接以“{following[-1]}”开头的成语。"
    return f"接龙失败，这轮没接上：{reason}\n当前仍是：{previous}，请以“{previous[-1]}”开头。"
