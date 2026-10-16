"""指令识别集中在此；显示用艾特不参与指令名判断。"""
import re

COMMANDS = frozenset({"/管理", "/绑定", "/档案", "/记忆", "/纠正", "/忘记", "/关联人物",
                      "/总结", "/总结群聊", "/清空", "/切换人格", "/help"})
GAME_COMMANDS = frozenset({"成语接龙", "合并接龙", "退出接龙"})
HELP_TEXT = ("艾特机器人：对话、接梗\n/绑定 昵称：登记本人称呼\n/档案 [昵称]：查看人物资料\n"
             "/记忆 关键词：查长期知识\n/纠正 编号 新内容：维护者修订资料\n/忘记 编号：维护者停用资料\n"
             "/总结群聊 [条数或今天]\n/总结 昵称 [条数或今天]\n"
             "/管理 帮助：维护者命令（支持私聊）\n/关联人物 @群友 人物名：维护者关联身份\n/清空：维护者重置上下文，保留历史和知识\n"
             "成语接龙 / 合并接龙 / 退出接龙\n固定熟人损友风格；默认总结最近200条。")


def command_text(text):
    return re.sub(r"^(?:\s*@[^@\r\n\u2005]+\u2005)+", "", text).strip()


def is_control(text):
    value = command_text(text)
    return value.startswith("/") or value in GAME_COMMANDS


def effective_text(text):
    return bool(text.strip() and not is_control(text) and "WXBOT_" not in text)
