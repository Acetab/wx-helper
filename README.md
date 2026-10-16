# 微信文本机器人框架
项目已在linux.do公开声明[https://linux.do/](https://linux.do/t/topic/3029497/51)


面向 Windows x64、微信 4.1.15.13 的 Python 插件框架。统一入口为 `main.py`，原生桥接负责文本收发，功能以插件加载。

完整操作说明见[使用手册](使用手册.md)。已有用户可运行 `python tools/migrate_memory.py` 升级本地配置及数据库，再正常重启机器人。

本 fork 的维护仓库为 [Inmata/helper](https://github.com/Inmata/helper)，原始项目为 [Prometheus-first/helper](https://github.com/Prometheus-first/helper)。日常更新从自己的 fork 获取；原项目保留为 `upstream`，按需评估后合并。


# 本项目仅供学习，请勿用于违法行为

## 功能

- 文件传输助手、好友和群聊文本收发，以及真正的群艾特。
- 成语接龙：全群共享一条进度，四字成语接首字，普通群聊不会触发接龙。
- `/绑定 昵称`：只设置当前群的称呼；不同群各自保存。未绑定时使用联系人缓存中的微信名。
- `/总结 昵称 [条数或今天]`、`/总结群聊 [条数或今天]`：默认总结最近200条，可跨天。
- 固定熟人损友语气；旧人格切换指令只显示兼容提示。
- 真正艾特机器人后，结合本人最近10条、全群最近50条和检索到的长期知识回复。
- 原文与知识长期保存；管理员 `/清空` 仅重置对话上下文，保留历史和知识。
- `/档案`、`/记忆`、`/纠正`、`/忘记`：查询和维护有来源的成员资料、语言习惯与群梗。
- 唯一维护者可在群聊、私聊使用 `/管理`；服务器控制台和离线命令行共用维护逻辑，支持人物、别名、知识归属、清空重学和操作历史。
- 可配置本版本首次启动后的更新公告；改了正文就会在下次启动自动公告一次，重连和重复重启不刷屏。
- 可配置自动学习，以及09:00—23:00有新聊天时的整点发言。
- 按文本长度随机等待后发送。
- 插件启停、重载与开发模板。

## 运行环境

Windows 10/11 x64、64 位 Python（已验证 Python 3.13）、`requirements.txt` 中的依赖，以及 Microsoft Visual C++ x64 运行库。

## 首次配置

在项目目录运行：

```powershell
python -m pip install -r requirements.txt
python tools/init_config.py
```

1. `bot_config.json`：将 `wechat_account` 改为自己的机器人微信 ID。绑定保存的是账号 ID，重新登录后 PID 改变不影响选择；多开时会等待指定账号，不改用其他账号。
2. `members_config.json`：在 `groups` 中添加自己要启用的群 ID，成员列表可以为空：

```json
{
  "database_file": ".runtime/member_data.sqlite3",
  "history_enabled": true,
  "daily_clear": false,
  "owner_wxid": "wxid_example_owner",
  "groups": {
    "12345678@chatroom": []
  }
}
```

3. `llm_config.json`：填写自己的 `api_key`、`base_url` 和 `model`。模板保留 DeepSeek 接口格式，密钥为空。
4. 模板默认关闭需要模型的 `idiom_chain`、`chat_summary`、`mention_chat` 插件。填写模型配置后，把它们的 `enabled` 改为 `true`。

`owner_wxid` 必须填写维护者本人账号，不能填写机器人本号。留空时聊天维护关闭，服务器命令行仍可用。旧 `admin_wxids` 不再授予权限。

账号 ID 必须是实际微信 ID；群 ID 以 `@chatroom` 结尾。`test` 消息会由 `id_responder` 插件回复当前会话 ID，`chats` 控制台命令可以列出本次连接收到消息的会话。所有示例 ID 和测试用名称均为虚构数据。

## 启动与验证

```powershell
python main.py --check
python main.py
```

`--check` 只检查配置、插件清单与语法，不注入微信，也不调用模型。正常运行前打开微信并登录配置中的账号。

也可以运行 `start_bot.ps1`：脚本优先使用项目的 `.venv`，其次使用 PATH 中的 Python；可通过 `-Python` 或环境变量 `WXBOT_PYTHON` 指定解释器，不依赖某台电脑的安装路径。

控制台命令：

```text
status
manage --room 群ID 帮助
chats
members [群ID]
history 称呼 [条数]
plugins
enable 插件名
disable 插件名
reload [插件名]
quit
```

增加群、修改账号或成员配置后重启。停止时使用 `quit`，等待正常卸载原生桥接；微信可以保持打开。

`wechat_account` 设为 `null` 时，原生收发层可以选择唯一登录账号；启用成员目录和群记录需要明确配置机器人账号，以隔离不同账号的数据。

## 目录

```text
main.py                  统一入口
wxbot_framework/         框架、成员目录、消息缓存、大模型客户端
plugins/                 各功能插件和开发模板
native/                  原生桥接源码、两个必要 DLL、版本指纹和 MinHook
tests/                  本地测试
tools/                   本地配置初始化和发布前检查
```

