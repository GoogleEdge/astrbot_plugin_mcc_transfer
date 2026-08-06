# AstrBot MCC 聊天转发插件

这是一个不调用 LLM 的 AstrBot 插件：通过 WebSocket 连接 Minecraft Console
Client（MCC）MCP Server，使用本地确定性规则解析/过滤游戏事件，并通过
AstrBot 主动消息 API 转发到 QQ 群。

## 特性

- WebSocket JSON auth、`PlayerMessage` 订阅与断线指数退避重连
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

## 配置

`config.example.ini` 是独立 CLI/测试模式的 INI 示例；它与 AstrBot 配置映射到
同一个内部配置模型。核心设置如下：

```ini
[mcp]
host = 127.0.0.1
port = 25575
password = your_secure_password

[target]
platform_name = aiocqhttp
platform_instance = default
message_type = GroupMessage
group_id = 170543353
message_template = [Minecraft] <{sender}> {message}
```

`platform_instance=default` 只适合目标平台实例唯一的部署；多个实例时应填写明确
的实例 ID。UMO 由适配层集中构造为
`platform:instance:message_type:group_id`，也可配置 `umo_override`。

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
