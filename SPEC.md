SPEC.md — Minecraft 聊天转发插件（MCP 版）

1. 项目概述

本项目是一个 AstrBot 插件，通过 MCP (Minecraft Console Protocol) 协议连接 Minecraft Console Client（MCC）的 MCP Server，实时接收游戏内玩家聊天，经过确定性规则过滤后，通过 AstrBot 主动消息 API 转发到指定 QQ 群。

核心特性

· 基于 MCP 协议：通过 WebSocket 连接 MCC 内置 MCP Server，实时接收聊天事件，非文件轮询。
· 零 Token 消耗：所有解析、过滤、决策均由本地确定性代码完成，不调用任何 LLM 或外部推理 API。
· AstrBot 原生集成：以插件形式运行，默认利用 AstrBot Context 的主动消息能力发送到 QQ 群；可选通过 AstrBot 4.18+ HTTP OpenAPI 发送。
· 高度可配置：所有过滤规则独立开关，支持黑白名单、关键词、正则、消息类型过滤。
· 高可靠性：支持断线重连、失败队列、指数退避重试，状态持久化。

2. 技术栈

· Python 3.11+
· AstrBot Plugin Framework（参考 AstrBot 插件开发文档）
· websockets（WebSocket 客户端，连接 MCC MCP Server）
· aiohttp / httpx（异步网络请求，符合 AstrBot 开发规范）
· pywin32（Windows Service 支持，可选）

依赖安装加速

如遇 GitHub 访问缓慢，可使用以下镜像源加速：

```bash
# 使用 gh-proxy 镜像克隆插件模板
git clone https://v4.gh-proxy.org/https://github.com/Soulter/helloworld

# 或在 pip 安装时使用镜像
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

gh-proxy 配置示例参考：https://v4.gh-proxy.org/https://github.com/WJQSERVER-STUDIO/ghproxy/blob/main/config/config.toml

3. 系统架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Minecraft Server                                │
│                              │                                      │
│                              ▼ (游戏内聊天)                        │
│                    MCC (MCP Server)                               │
│                    - 端口: 25575 (可配置)                          │
│                    - 协议: WebSocket / MCP                        │
│                              │                                      │
│                              │ WebSocket 连接                     │
│                              ▼                                      │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │              AstrBot 插件: MCC Forwarder                     │  │
│  │  ┌─────────────┐    ┌─────────────┐    ┌─────────────────┐ │  │
│  │  │ MCP Client  │───▶│  Parser     │───▶│  FilterChain    │ │  │
│  │  │ (WebSocket) │    │ (事件解析)  │    │ (确定性规则)    │ │  │
│  │  └─────────────┘    └─────────────┘    └────────┬────────┘ │  │
│  │                                                  │           │  │
│  │                                                  ▼           │  │
│  │                                 ┌───────────────────────────┐│  │
│  │                                 │  AstrBot Context API      ││  │
│  │                                 │  (send_message)           ││  │
│  │                                 └───────────┬───────────────┘│  │
│  └─────────────────────────────────────────────┼─────────────────┘  │
│                                                │                    │
│                                                ▼                    │
│                                         目标 QQ 群                  │
└─────────────────────────────────────────────────────────────────────┘
```

4. 模块详细说明

4.1 MCP Client（mcp_client.py）

WebSocket 客户端，负责与 MCC MCP Server 通信。

功能：

· 连接到 ws://{host}:{port}。
· 发送认证请求（密码）。
· 订阅 PlayerMessage 事件。
· 接收并解析 MCP 消息，提取玩家名和聊天内容。
· 自动重连（网络中断或服务重启）。

接口：

```python
class MCPClient:
    def __init__(self, host: str, port: int, password: str, on_message: Callable):
        """初始化 MCP 客户端。
        on_message: 收到消息时的回调函数，签名为 (sender: str, message: str, raw: dict) -> None
        """
    
    async def connect(self):
        """启动连接并进入消息循环。"""
    
    async def disconnect(self):
        """断开连接。"""
```

协议参考：
MCC 的 MCP 协议基于 JSON over WebSocket，常见消息格式：

```json
// 认证请求
{"type": "auth", "password": "your_password"}

// 认证响应
{"status": "success"}

// 订阅事件
{"type": "subscribe", "event": "PlayerMessage"}

// 玩家消息事件
{"type": "event", "event": "PlayerMessage", "player": "brightmoon", "message": "大家好"}
```

注意： 实际字段名可能因 MCC 版本而异，应提供可配置的字段映射。

4.2 Parser（parser.py）

解析 MCP 事件，提取结构化数据。

输入： MCP 原始事件字典。
输出：

```python
{
    "sender": "brightmoon",
    "message": "大家好",
    "timestamp": "2026-08-05T21:00:00+08:00",
    "raw": {...},
    "kind": "chat"  # chat / system / join / leave / death / advancement
}
```

4.3 FilterChain（filter_chain.py）

顺序执行所有过滤器，每个过滤器独立开关。

执行顺序：

1. Bot 自身消息（ignore_bot_messages）
2. 系统消息（ignore_system_messages）
3. 加入游戏（ignore_join_messages）
4. 退出游戏（ignore_leave_messages）
5. 死亡消息（ignore_death_messages）
6. 成就/进度（ignore_advancement_messages）
7. 公告（ignore_server_announcements）
8. 命令消息（ignore_command_messages）
9. 玩家黑名单（enable_player_blacklist）—— 黑名单优先于白名单
10. 玩家白名单（enable_player_whitelist）
11. 关键词过滤（enable_keyword_filter）
12. 正则表达式过滤（enable_regex_filter）
13. 空消息（ignore_empty_messages）
14. 重复消息去重（基于指纹缓存）

4.4 插件开发规范

遵循 AstrBot 插件开发指南：

· 插件命名：以 astrbot_plugin_ 开头，全部小写。
· 元数据：必须包含 metadata.yaml 文件。
· 依赖管理：使用 requirements.txt 管理依赖。
· 持久化数据：存储于 data/ 目录下，防止更新时被覆盖。
· 网络请求：使用 aiohttp 或 httpx，不使用 requests。
· 代码格式：提交前使用 ruff 格式化。

metadata.yaml 示例：

```yaml
name: astrbot_plugin_mcc_forwarder
display_name: MCC 聊天转发插件
desc: 通过 MCP 协议连接 MCC，将聊天转发到 QQ 群
short_desc: Minecraft 聊天 → QQ 群，零 Token 消耗
version: 1.0.0
author: your_name
astrbot_version: ">=4.16,<5"  # 声明兼容的 AstrBot 版本范围[reference:17]
support_platforms:
  - aiocqhttp
  - qq_official
```

5. 配置管理

5.1 配置文件格式

使用 config.ini（INI 格式），支持多段配置。

5.2 配置项完整列表

```ini
[mcp]
# MCC MCP Server 连接配置；当前版本默认 HTTP MCP
transport = http
url = http://127.0.0.1:33333/mcp
host = 127.0.0.1
port = 25575
password = your_secure_password
; 重连间隔（秒），支持指数退避
reconnect_initial_delay = 1
reconnect_max_delay = 30

[sender]
# native 使用 Context API；openapi 需要 AstrBot 4.18+
mode = native
endpoint = http://127.0.0.1:6185/api/v1/im/message
auth_header = bearer
api_key_env = ASTRBOT_MCC_TRANSFER_OPENAPI_KEY
api_key =
timeout = 10

[target]
# 目标 QQ 群；完整 UMO 可通过 umo_override 原样指定
group_id = 170543353
umo_override =
; 消息模板
message_template = [Minecraft] <{sender}> {message}

[filter]
# 全局开关
enabled = true

; Bot 自身消息
ignore_bot_messages = true
bot_name = MCCBot

; 消息类型过滤
ignore_system_messages = true
ignore_join_messages = true
ignore_leave_messages = true
ignore_death_messages = true
ignore_advancement_messages = true
ignore_server_announcements = true
ignore_empty_messages = true
ignore_command_messages = true
command_prefixes = /,!

; 玩家名单
enable_player_blacklist = false
blacklist_players = 
enable_player_whitelist = false
whitelist_players = 

; 内容过滤
enable_keyword_filter = false
blocked_keywords = 
keyword_case_sensitive = false
keyword_whole_word = false

enable_regex_filter = false
blocked_regex = 

[dedup]
enabled = true
cache_size = 1000
ttl_seconds = 300
state_file = data/dedup_cache.json

[retry]
max_attempts = 5
initial_delay_seconds = 1
max_delay_seconds = 30
queue_file = data/failed_messages.json
replay_failed_messages = true
max_queue_size = 1000
drop_expired_messages = false
message_expire_seconds = 3600

[security]
max_message_length = 500
rate_limit_per_second = 5
split_long_messages = true
merge_messages = false
merge_window_seconds = 3

[logging]
level = INFO
log_file = logs/plugin.log
max_bytes = 10485760
backup_count = 5
```

6. 开发环境配置

P.S. 以下内容不一定准确，请以https://docs.astrbot.app/dev/star/plugin-new.html为准。

6.1 获取插件模板

```bash
# 使用 gh-proxy 加速克隆插件模板
git clone https://v4.gh-proxy.org/https://github.com/Soulter/helloworld
cd helloworld
# 重命名为你的插件名
mv helloworld astrbot_plugin_mcc_forwarder
```

6.2 克隆 AstrBot 本体

```bash
git clone https://v4.gh-proxy.org/https://github.com/AstrBotDevs/AstrBot
mkdir -p AstrBot/data/plugins
cd AstrBot/data/plugins
# 将插件目录链接或复制到这里[reference:18]
ln -s /path/to/astrbot_plugin_mcc_forwarder .
```

6.3 安装依赖

```bash
cd AstrBot
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

6.4 调试插件

AstrBot 支持热重载功能，代码修改后可在 WebUI 插件管理页点击「重载插件」即可生效。

7. 命令行工具

提供独立脚本 cli.py，用于管理插件。

命令 功能
python cli.py --check-config 验证配置文件语法
python cli.py --test-mcp 测试 MCP 连接
python cli.py --dry-run 模拟运行，不发送消息
python cli.py --status 显示当前状态
python cli.py --reload-config 热加载配置

8. Windows Service 支持

提供脚本（使用 pywin32 或 NSSM）：

· install-service.cmd：安装 Windows 服务。
· uninstall-service.cmd：卸载服务。
· start-service.cmd：启动服务。
· stop-service.cmd：停止服务。

服务配置：

· 开机自启。
· 崩溃后自动重启（服务管理器设置）。
· 固定工作目录为插件安装目录。

9. 测试要求

9.1 单元测试

覆盖以下场景：

1. MCP 消息解析（各种玩家名格式）。
2. 每个过滤器的独立开关测试。
3. 黑白名单优先级测试。
4. 去重缓存测试。
5. 消息格式化与长度截断。
6. 失败队列持久化与恢复。

9.2 集成测试

1. 模拟 MCP Server（使用 websockets 实现 mock）。
2. 模拟 AstrBot Context API。
3. 端到端测试：消息 → 过滤 → 发送。
4. 断线重连测试。
5. 配置文件热加载测试。

10. 交付物

1. 完整插件源代码。
2. config.example.ini（含详细注释）。
3. README.md（含安装、配置、三种典型配置示例）。
4. Windows 服务脚本（install-service.cmd 等）。
5. 单元测试和集成测试代码。
6. requirements.txt。
7. Mock MCP Server（用于测试）。
8. 测试报告。

11. 附录：典型配置示例

示例 A：仅转发玩家聊天

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

示例 B：转发玩家聊天 + 加入/退出

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

示例 C：仅转发指定玩家的聊天

```ini
[filter]
ignore_bot_messages = true
ignore_system_messages = true
enable_player_whitelist = true
whitelist_players = brightmoon,TaleCake
enable_player_blacklist = false
```

12. 参考文档

· AstrBot 插件开发指南：https://docs.astrbot.app/dev/star/plugin-new.html
· MCC Chat Bots 文档（含 MCP Server）：https://mccteam.github.io/guide/chat-bots.html#mcp-server
· gh-proxy 镜像加速配置示例：https://v4.gh-proxy.org/https://github.com/WJQSERVER-STUDIO/ghproxy/blob/main/config/config.toml

---

本 SPEC 定义了完整的、基于 MCP 协议的 Minecraft 聊天转发插件，所有功能均基于确定性代码，零 LLM 调用，零 Token 消耗。