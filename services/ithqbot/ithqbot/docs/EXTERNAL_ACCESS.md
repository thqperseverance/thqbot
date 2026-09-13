# 外部网络访问清单

本文档列出 ithqbot 会访问的外部网站与 API，并说明其用途与类别。

## 1. LLM 提供方（核心推理链路）

以下提供方用于推理、工具调用与内容生成。

| 提供方 | 接口地址 | 用途 |
| :--- | :--- | :--- |
| **OpenRouter** | `https://openrouter.ai/api/v1` | 统一接入多种模型 |
| **SiliconFlow** | `https://api.siliconflow.cn/v1` | 高性能模型托管（适配国内网络） |
| **Volcengine (Ark)** | `https://ark.cn-beijing.volces.com` | 火山引擎模型服务 |
| **DeepSeek** | `https://api.deepseek.com` | DeepSeek 模型 |
| **Moonshot** | `https://api.moonshot.ai/v1` | Moonshot / Kimi 模型 |
| **MiniMax** | `https://api.minimax.io/v1` | MiniMax 模型 |
| **NVIDIA** | `https://integrate.api.nvidia.com/v1` | NVIDIA NIM 模型 |
| **Aihubmix** | `https://aihubmix.com/v1` | 聚合式模型服务 |
| **ChatGPT（浏览器）** | `https://chatgpt.com/backend-api` | OpenAI 网页端后端访问 |

## 2. 联网工具与搜索

这些目标会在代理执行联网搜索或网页抓取时访问。

| 目标 | 接口地址 | 用途 |
| :--- | :--- | :--- |
| **Brave Search** | `https://api.search.brave.com` | 返回网页搜索结果 |
| **Tavily** | `https://api.tavily.com` | 面向 AI 的搜索接口 |
| **Jina Reader** | `https://r.jina.ai`、`https://s.jina.ai` | 从 URL 提取 Markdown / 文本内容 |
| **Wttr.in** | `https://wttr.in` | 天气信息（无需 API Key） |
| **SearxNG** | （可配置） | 自建或公共元搜索引擎 |

## 3. 周边服务

| 服务 | 接口地址 | 用途 |
| :--- | :--- | :--- |
| **Groq（音频）** | `https://api.groq.com/.../transcriptions` | 基于 Whisper 的语音转写 |
| **Lark / 飞书** | `https://open.feishu.cn` | 消息投递与机器人集成 |

## 4. 文档与资源

- `https://openrouter.ai/keys`：获取 API Key。
