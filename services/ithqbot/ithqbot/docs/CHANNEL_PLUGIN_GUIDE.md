# 频道插件指南

按下面三个步骤即可构建自定义 ithqbot 频道插件：继承、打包、安装。

## 工作原理

ithqbot 通过 Python 的 [entry points](https://packaging.python.org/en/latest/specifications/entry-points/) 发现频道插件。执行 `ithqbot gateway` 启动网关时，会扫描：

1. `ithqbot/channels/` 下的内置频道
2. 注册到 `ithqbot.channels` entry point 分组的外部 Python 包

如果对应配置段存在且 `"enabled": true`，频道实例就会被创建并启动。

## 快速开始

下面以一个最小可用的 Webhook 频道为例，它通过 HTTP POST 接收消息，并把回复发回调用方。

### 项目结构

```
ithqbot-channel-webhook/
├── ithqbot_channel_webhook/
│   ├── __init__.py          # 导出 WebhookChannel
│   └── channel.py           # 频道实现
└── pyproject.toml
```

### 1. 创建频道类

```python
# ithqbot_channel_webhook/__init__.py
from ithqbot_channel_webhook.channel import WebhookChannel

__all__ = ["WebhookChannel"]
```

```python
# ithqbot_channel_webhook/channel.py
import asyncio
from typing import Any

from aiohttp import web
from loguru import logger

from ithqbot.channels.base import BaseChannel
from ithqbot.bus.events import OutboundMessage


class WebhookChannel(BaseChannel):
    name = "webhook"
    display_name = "Webhook"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return {"enabled": False, "port": 9000, "allowFrom": []}

    async def start(self) -> None:
        """启动用于接收消息的 HTTP 服务。

        注意：start() 必须一直阻塞运行，直到 stop() 被调用。
        如果该方法直接返回，系统会认为频道已经失效。
        """
        self._running = True
        port = self.config.get("port", 9000)

        app = web.Application()
        app.router.add_post("/message", self._on_request)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        logger.info("Webhook 已监听端口 :{}", port)

        while self._running:
            await asyncio.sleep(1)

        await runner.cleanup()

    async def stop(self) -> None:
        self._running = False

    async def send(self, msg: OutboundMessage) -> None:
        """发送出站消息。

        msg.content  表示 Markdown 文本，可按目标平台要求转换格式
        msg.media    表示待发送的本地文件路径列表
        msg.chat_id  表示接收方，需与 _handle_message 中传入的 chat_id 对应
        msg.metadata 可能包含 "_progress": True，用于流式输出分片
        """
        logger.info("[webhook] -> {}: {}", msg.chat_id, msg.content[:80])
        # 真正接入平台时，可以在这里回调业务接口或调用平台 SDK。

    async def _on_request(self, request: web.Request) -> web.Response:
        """处理传入的 HTTP POST 请求。"""
        body = await request.json()
        sender = body.get("sender", "unknown")
        chat_id = body.get("chat_id", sender)
        text = body.get("text", "")
        media = body.get("media", [])

        # 关键调用：这里会先校验 allowFrom，再将消息投递到 bus。
        await self._handle_message(
            sender_id=sender,
            chat_id=chat_id,
            content=text,
            media=media,
        )

        return web.json_response({"ok": True})
```

### 2. 注册 Entry Point

```toml
# pyproject.toml
[project]
name = "ithqbot-channel-webhook"
version = "0.1.0"
dependencies = ["ithqbot", "aiohttp"]

[project.entry-points."ithqbot.channels"]
webhook = "ithqbot_channel_webhook:WebhookChannel"

[build-system]
requires = ["setuptools"]
build-backend = "setuptools.backends._legacy:_Backend"
```

这里的键名 `webhook` 会成为配置段名称，值则指向你的 `BaseChannel` 子类。

### 3. 安装并配置

```bash
pip install -e .
ithqbot plugins list      # 确认 "Webhook" 显示为 "plugin"
ithqbot onboard           # 自动为已发现插件补充默认配置
```

然后编辑 `~/.ithqbot/config.json`：

```json
{
  "channels": {
    "webhook": {
      "enabled": true,
      "port": 9000,
      "allowFrom": ["*"]
    }
  }
}
```

### 4. 运行并验证

```bash
ithqbot gateway
```

在另一个终端中执行：

```bash
curl -X POST http://localhost:9000/message \
  -H "Content-Type: application/json" \
  -d '{"sender": "user1", "chat_id": "user1", "text": "你好！"}'
```

此时 agent 会收到并处理该消息，最终回复会回到你的 `send()` 方法中。

## BaseChannel API

### 必需实现的方法

| 方法 | 说明 |
|--------|-------------|
| `async start()` | **必须持续阻塞运行。** 负责连接平台、监听消息，并在收到消息后调用 `_handle_message()`；若方法返回，说明该频道已失效。 |
| `async stop()` | 将 `self._running` 设为 `False` 并完成清理；网关退出时会调用。 |
| `async send(msg: OutboundMessage)` | 将出站消息发送到目标平台。 |

### BaseChannel 已提供的能力

| 方法 / 属性 | 说明 |
|-------------------|-------------|
| `_handle_message(sender_id, chat_id, content, media?, metadata?, session_key?)` | **收到消息时应调用此方法。** 它会先执行 `is_allowed()` 校验，再把消息发布到 bus。 |
| `is_allowed(sender_id)` | 按 `config["allowFrom"]` 校验来源；`"*"` 表示全部允许，`[]` 表示全部拒绝。 |
| `default_config()`（classmethod） | 为 `ithqbot onboard` 返回默认配置字典；如需声明字段，请覆盖此方法。 |
| `transcribe_audio(file_path)` | 使用 Groq Whisper 执行音频转写（前提是已配置）。 |
| `is_running` | 返回当前 `self._running` 状态。 |

### 消息类型

```python
@dataclass
class OutboundMessage:
    channel: str        # 频道名称
    chat_id: str        # 接收方，需与 _handle_message 传入值一致
    content: str        # Markdown 文本，可按平台格式进行转换
    media: list[str]    # 需要附带发送的本地文件路径（图片、音频、文档等）
    metadata: dict      # 可能包含 "_progress"（布尔值）用于流式分片，
                        # 也可能包含 "message_id" 用于回复串联
```

## 配置

频道实例拿到的是普通 `dict` 配置对象，通常通过 `.get()` 读取：

```python
async def start(self) -> None:
    port = self.config.get("port", 9000)
    token = self.config.get("token", "")
```

`allowFrom` 会由 `_handle_message()` 自动处理，频道自身通常不需要重复校验。

建议覆盖 `default_config()`，这样 `ithqbot onboard` 就能自动写入默认配置：

```python
@classmethod
def default_config(cls) -> dict[str, Any]:
    return {"enabled": False, "port": 9000, "allowFrom": []}
```

如果不覆盖，基类默认返回 `{"enabled": false}`。

## 命名约定

| 项目 | 格式 | 示例 |
|------|--------|---------|
| PyPI 包名 | `ithqbot-channel-{name}` | `ithqbot-channel-webhook` |
| Entry Point 键名 | `{name}` | `webhook` |
| 配置段 | `channels.{name}` | `channels.webhook` |
| Python 包名 | `ithqbot_channel_{name}` | `ithqbot_channel_webhook` |

## 本地开发

```bash
git clone https://github.com/you/ithqbot-channel-webhook
cd ithqbot-channel-webhook
pip install -e .
ithqbot plugins list    # 应看到 "Webhook" 显示为 "plugin"
ithqbot gateway         # 进行端到端验证
```

## 验证结果

```bash
$ ithqbot plugins list

  Name       Source    Enabled
  telegram   builtin  yes
  discord    builtin  no
  webhook    plugin   yes
```
