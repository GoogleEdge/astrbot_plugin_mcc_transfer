# AstrBot MCC 聊天转发插件

这是一个不调用 LLM 的 AstrBot 插件：通过当前 MCC 的 HTTP MCP Server（默认
`http://127.0.0.1:33333/mcp`）轮询游戏事件，使用本地确定性规则解析/过滤，并通过
AstrBot 主动消息 API 转发到 QQ 群。默认使用 Context API，也可显式选择 AstrBot HTTP OpenAPI；旧式 WebSocket `PlayerMessage` profile 仍可显式启用。

## 特性

- HTTP MCP JSON-RPC initialize/session/tool-call，默认轮询 `mcc_chat_history`
- 旧式 WebSocket JSON auth、`PlayerMessage` 订阅与断线指数退避重连
- 固定顺序过滤：Bot、系统/加入/退出/死亡/进度、公告、命令、黑白名单、关键词、正则、空消息、去重
- 失败消息持久化和指数退避重试
- 去重缓存 TTL/容量控制，消息限流、长度分片/截断、可选合并
- 纯核心模块可在没有 AstrBot 的环境中通过 CLI 和测试运行

## 安装

将本目录复制到 AstrBot 的 `data/plugins/astrbot_plugin_mcc_transfer`，在 AstrBot
环境中安装依赖：

```powershell
powershell -NoProfile -Command "python -m pip install -r requirements.txt"
```

重载插件后，在 AstrBot WebUI 中填写 `_conf_schema.json` 暴露的设置。AstrBot
原生部署以注入的 `AstrBotConfig` 为准，不会自动读取仓库中的 `config.ini`。
运行状态默认保存到 AstrBot 数据目录的
`plugin_data/astrbot_plugin_mcc_transfer/`。首次安装时 `target.group_id` 可以留空，
插件会保持未启用状态；安装完成后在插件配置页填写群号并重载插件即可开始转发。
插件通过 `Star.initialize()` 在每次加载/重载时启动，不依赖只在 AstrBot 全局启动时触发的事件。

## 配置

`config.example.ini` 是独立 CLI/测试模式的 INI 示例；它与 AstrBot 配置映射到
同一个内部配置模型。核心设置如下：

### 发送方式

默认 `sender.mode = native`，调用 AstrBot 的 `Context.send_message()`。如果原生主动发送不可用，
可以在 AstrBot 4.18+ 中显式选择 `sender.mode = openapi`。OpenAPI 默认地址为
`http://127.0.0.1:6185/api/v1/im/message`，请求使用完整 UMO 和纯文本消息：

```json
{"umo":"qqbot:GroupMessage:<session>","message":"[Minecraft] <player> hello"}
```

API key 只从 `sender.api_key_env` 指定的环境变量读取，默认变量名为
`ASTRBOT_MCC_TRANSFER_OPENAPI_KEY`；不要把 key 写入配置、代码、日志或提交记录。认证头可选
`Authorization: Bearer <key>` 或 `X-API-Key: <key>`。权限不足时 AstrBot 可能返回 `403`；
插件将任意 HTTP 2xx 视为成功。OpenAPI 文档没有规定 QQ 群 UMO 的生成规则，因此应直接使用
`target.umo_override` 中 `/sid` 显示的完整 UMO。实际部署若路径不同，可修改 endpoint。

```ini
[sender]
mode = native
endpoint = http://127.0.0.1:6185/api/v1/im/message
auth_header = bearer
api_key_env = ASTRBOT_MCC_TRANSFER_OPENAPI_KEY
timeout = 10

[mcp]
transport = http
url = http://127.0.0.1:33333/mcp
poll_interval = 2
chat_tool = mcc_chat_history
chat_max_count = 50

[target]
; QQ Official 使用 qq_official，并填写群 openid 或频道 channel_id
platform_name = qq_official
platform_instance = default
message_type = GroupMessage
group_id = your_group_openid
message_template = [Minecraft] <{sender}> {message}
```

本插件支持 AstrBot 的 QQ 官方渠道（`qq_official`）和 OneBot v11（`aiocqhttp`）。
QQ 官方群目标通常填写群 `openid`；如果转发到频道场景，则填写 `channel_id`。
如果你手里没有 openid，只有 AstrBot 显示的 `uid`、`session_id` 或完整 `umo`，
优先直接填写完整 `umo`，不要把 `uid` 猜成群号。配置方式如下：

```ini
platform_name = qq_official
message_type = GroupMessage
; 没有 group openid 时留空
group_id =
; 把 AstrBot 事件/会话中显示的完整 UMO 原样粘贴到这里
umo_override = qq_official:GroupMessage:你的session_id
```

如果 WebUI 配置项中直接有 `umo` 字段，也可以把完整 UMO 填到 `umo`；插件会将
它当作 `umo_override` 使用。`umo_override` 会原样传给 `context.send_message`，
因此它优先级最高。
如果只有 `session_id` 而没有完整 UMO，可按 AstrBot v4.26.8 的格式组成
`qq_official:GroupMessage:<session_id>`；但必须确认该 session 是群或频道会话，
不能使用个人用户的 `uid`。OneBot v11 才填写数字群号。`platform_instance` 仅用于
自定义模板，原生 UMO 由适配层按 `platform:message_type:group_id` 构造。

MCC 字段名可能随版本变化。`[protocol]` 和 `[parser]` 中的 JSON 路径/模板可以
修改认证、订阅、事件 payload 及 sender/message/kind/timestamp 字段，不需要修改
WebSocket 客户端或过滤器。

### 三种典型配置

**A：仅转发玩家聊天**

```ini
[filter]
ignore_bot_messages = true
ignore_system_messages = true
ignore_join_messages = true
ignore_leave_messages = true
ignore_death_messages = true
ignore_advancement_messages = true
ignore_server_announcements = true
```

**B：玩家聊天 + 加入/退出**

```ini
[filter]
ignore_bot_messages = true
ignore_system_messages = true
ignore_join_messages = false
ignore_leave_messages = false
ignore_death_messages = true
ignore_advancement_messages = true
ignore_server_announcements = true
```

**C：仅指定玩家**

```ini
[filter]
ignore_bot_messages = true
enable_player_whitelist = true
whitelist_players = brightmoon,TaleCake
enable_player_blacklist = false
```

## CLI

CLI 用于不依赖 AstrBot 的配置检查、MCP 测试和 dry-run：

```powershell
powershell -NoProfile -Command "python cli.py --check-config --config config.example.ini"
powershell -NoProfile -Command "python cli.py --test-mcp --config config.example.ini"
powershell -NoProfile -Command "python cli.py --dry-run --config config.example.ini --message 'hello' --sender brightmoon"
powershell -NoProfile -Command "python cli.py --status --data-dir data"
powershell -NoProfile -Command "python cli.py --reload-config --config config.example.ini --data-dir data"
```

`--reload-config` 写入独立运行时的控制请求；原生 AstrBot 配置应通过 WebUI 修改并
重载插件。

## Mock MCP Server

无需真实 MCC 即可启动一个兼容 SPEC 示例协议的服务器：

```powershell
powershell -NoProfile -Command "python mock_mcp_server.py --password your_secure_password --verbose"
```

自动化测试使用 `MockMCPServer`，不会连接真实 QQ 或 MCC。

## Windows 服务

服务脚本默认使用 NSSM 包装 AstrBot 主进程，而不是把插件当成独立 AstrBot。
设置 `NSSM_EXE`、`ASTRBOT_DIR`、`ASTRBOT_EXE`、`ASTRBOT_ARGS`、`SERVICE_NAME`
等环境变量后，以管理员 PowerShell/cmd 运行：

```powershell
.\install-service.cmd
.\start-service.cmd
.\stop-service.cmd
.\uninstall-service.cmd
```

## 可靠性说明

发送成功后才将去重指纹标记为 delivered；发送异常先写入失败队列并按配置重试。因
远端接收和本地确认之间存在不可避免的崩溃窗口，插件提供的是 at-least-once
语义，进程恰好在 QQ 接收后崩溃时重启可能产生一次重复消息。

## 开发与测试

所有 Python 命令均可通过 PowerShell 调用：

```powershell
powershell -NoProfile -Command "python -m pip install -r requirements-dev.txt"
powershell -NoProfile -Command "python -m pytest -q"
powershell -NoProfile -Command "python -m ruff check ."
```

测试只依赖本地 fake context、fake sender 和 Mock MCP Server。
