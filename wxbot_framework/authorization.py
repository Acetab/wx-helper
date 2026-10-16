"""本机调用凭据不来自聊天文本；聊天权限只匹配配置中的唯一维护者。"""

LOCAL_EDITOR = object()


def editor_label(editor):
    return "local-cli" if editor is LOCAL_EDITOR else editor
