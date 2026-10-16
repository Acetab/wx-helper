"""接收线程只做快速状态更新，让重置立即取消正在生成的回复。"""
from dataclasses import replace
from types import MappingProxyType

from .commands import command_text


def prepare_reset(message, store):
    if (store is None or message.type != 1 or not message.is_group or message.chat_id not in store.groups or
            message.data.get("bot_generated") or not message.data.get("dispatch_to_plugins", True) or
            command_text(message.text) != "/清空"):
        return message
    data = dict(message.data)
    if not store.is_admin(message.sender_id):
        data["context_error"] = "只有管理员可以重置本群对话上下文。"
    else:
        store.reset_context(message.chat_id)
        data["context_reset"] = True
    return replace(message, data=MappingProxyType(data))
