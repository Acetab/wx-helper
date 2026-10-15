# 微信文本机器人框架

面向 Windows x64、微信 4.1.15.13 的 Python 插件框架。统一入口为 `main.py`，原生桥接负责文本收发，功能以插件加载。

公开模板不包含真实账号、群号、成员绑定、API key、聊天记录或运行日志。首次使用需要填写自己的本地配置；示例账号 `wxid_example_robot` 是占位符。

## 功能

- 文件传输助手、好友和群聊文本收发，以及真正的群艾特。
- 成语接龙：独立玩家、合并抢答和退出模式。
- `/绑定 昵称`：只设置当前群的称呼；不同群各自保存。未绑定时使用联系人缓存中的微信名。
- `/总结 昵称`、`/总结群聊`：通过配置的大模型总结当前群当天的文字记录。
- `/切换人格 嘴臭|卑微|夸夸|取消`：为当前群的对话、总结和接龙点评设置表达风格。
- 真正艾特机器人后对话，携带该成员在当前群当天此前最多十条有效发言。
- 每天北京时间 0 点清理旧日文本；`/清空` 手动清理当前群文本。
- 按文本长度随机等待后发送；这是发送节奏设置，不保证账号不会受到平台限制。
- 插件启停、重载与开发模板。

## 运行环境

Windows 10/11 x64、64 位 Python（已验证 Python 3.13）、`requirements.txt` 中的依赖，以及 Microsoft Visual C++ x64 运行库。

使用预编译桥接不需要 Visual Studio、Ghidra 或调试器。微信版本和核心文件 SHA256 必须符合 `native/compatibility.json`；其他版本需要重新适配。

## 首次配置

在项目目录运行：

```powershell
python -m pip install -r requirements.txt
python tools/init_config.py
```

编辑生成的三个本地文件。这三个文件已被 Git 忽略，公开仓库只需提交对应的 `.example.json` 模板。

1. `bot_config.json`：将 `wechat_account` 改为自己的机器人微信 ID。绑定保存的是账号 ID，重新登录后 PID 改变不影响选择；多开时会等待指定账号，不改用其他账号。
2. `members_config.json`：在 `groups` 中添加自己要启用的群 ID，成员列表可以为空：

```json
{
  "database_file": ".runtime/member_data.sqlite3",
  "history_enabled": true,
  "daily_clear": true,
  "groups": {
    "12345678@chatroom": []
  }
}
```

3. `llm_config.json`：填写自己的 `api_key`、`base_url` 和 `model`。模板保留 DeepSeek 接口格式，密钥为空。
4. 模板默认关闭需要模型的 `idiom_chain`、`chat_summary`、`mention_chat` 插件。填写模型配置后，把它们的 `enabled` 改为 `true`。

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
tests/                   不连接微信或真实模型的本地测试
tools/                   本地配置初始化和发布前检查
*.example.json           可提交的脱敏配置模板
```

运行后才会生成 `.runtime/`、`logs/` 和本地配置。它们不属于公开源代码。

## 测试与原生编译

```powershell
python -B -m unittest discover -s tests -q
python -B tools/check_publication.py
```

单元测试使用虚构身份、内存后端和模拟模型响应，不向真实微信发送消息。

发布检查面向待上传的干净副本；测试生成的 `.runtime/`、缓存和日志应在打包前排除，检查工具会将它们列为待清理项。

需要重新编译 DLL 时，先进入 MSVC 的 x64 开发者环境，再执行：

```powershell
powershell -ExecutionPolicy Bypass -File native/build_bridges.ps1
```

该脚本只编译桥接组件，不修改系统安全设置。正常运行不需要执行编译步骤。
