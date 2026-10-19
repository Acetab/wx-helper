"""指令识别集中在此；显示用艾特不参与指令名判断。"""
import re

COMMANDS = frozenset({"/管理", "/绑定", "/解绑", "/别名", "/改名", "/成员", "/人物", "/记住",
                      "/档案", "/记忆", "/纠正", "/忘记", "/关联人物",
                      "/总结", "/总结群聊", "/清空", "/切换人格", "/help"})
GAME_COMMANDS = frozenset({"成语接龙", "合并接龙", "退出接龙"})
PUBLIC_HELP = ("艾特机器人：对话、接梗（微信昵称自动读取）\n"
               "/档案 [昵称或本群]：查看人物或本群公共记忆\n"
               "/记忆 关键词：查长期知识\n"
               "/总结群聊 [条数或今天]\n/总结 昵称 [条数或今天]\n"
               "成语接龙 / 退出接龙：全群共享一条接龙进度")
ADMIN_HELP = ("—— 管理员命令（群聊或私聊均可）——\n"
              "/成员 [未绑定|已绑定|同步]：查看群成员M编号与微信名\n"
              "/绑定 <@群友|M序号|微信名|wxid> <人物名>：确认绑定并建档\n"
              "/解绑 <@群友|M序号|人物名>：解除绑定\n"
              "/人物 [人物名]：查看人物列表或详情\n"
              "/别名 <人物名> <+新别名|-旧别名>：增删人物别名\n"
              "/改名 <旧人物名> <新人物名>：修改人物主名\n"
              "/记住 <人物名> <主题> [类别] <内容>：新增人工记忆\n"
              "/纠正 编号 新内容｜/忘记 编号｜/清空\n"
              "/管理 帮助：完整维护命令（含人物隔离/合并、公共记忆候选）")
HELP_TEXT = PUBLIC_HELP + "\n" + ADMIN_HELP


def help_text(is_admin=False):
    """普通成员只看到公开命令；管理员额外看到管理命令。"""
    return HELP_TEXT if is_admin else PUBLIC_HELP


def command_text(text):
    return re.sub(r"^(?:\s*@[^@\r\n\u2005]+\u2005)+", "", text).strip()


def is_control(text):
    value = command_text(text)
    return value.startswith("/") or value in GAME_COMMANDS


def effective_text(text):
    return bool(text.strip() and not is_control(text) and "WXBOT_" not in text)
