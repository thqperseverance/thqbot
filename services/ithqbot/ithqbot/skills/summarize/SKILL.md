---
name: summarize
description: 总结网页、文件、播客或 YouTube 内容；当用户要求快速提炼链接/视频/文档内容时立即使用。
homepage: https://summarize.sh
metadata: {"ithqbot":{"emoji":"🧾","requires":{"bins":["summarize"]},"install":[{"id":"brew","kind":"brew","formula":"steipete/tap/summarize","bins":["summarize"],"label":"Install summarize (brew)"}]}}
---

# 内容总结

## 目标

使用 `summarize` CLI 快速提炼网页、文章、PDF、本地文件、播客和 YouTube 视频内容。

## 何时使用

- “总结这个链接/网页/文章”
- “这个视频讲了什么”
- “帮我提炼 PDF 的重点”
- “转写这个 YouTube 视频”
- 用户明确说使用 `summarize.sh`

## 输入约束

- 输入可以是 URL、本地文件路径或 YouTube 链接
- 如环境中没有 `summarize` 可执行文件，该 skill 不可用
- 如果用户要完整逐字稿，先尝试 `--extract-only`；结果过长时先返回摘要，再按片段展开

## 执行步骤

1. 识别输入是网页、文件还是 YouTube 链接
2. 根据内容类型与用户目标选择合适参数，如 `--extract-only`、`--json`、`--youtube auto`
3. 执行 `summarize ...` 命令并提炼核心结果
4. 默认先返回摘要；如果用户继续追问，再展开结构化信息或逐段内容

## 快速命令

```bash
summarize "https://example.com" --model google/gemini-3-flash-preview
summarize "/path/to/file.pdf" --model google/gemini-3-flash-preview
summarize "https://youtu.be/dQw4w9WgXcQ" --youtube auto
```

## YouTube 转写

尽力提取转写文本：

```bash
summarize "https://youtu.be/dQw4w9WgXcQ" --youtube auto --extract-only
```

## 输出约定

- 默认返回紧凑摘要
- 如果使用 `--json`，可以进一步做结构化提取
- 如果用户请求“逐段展开”，先给总览，再补充局部细节

## 模型与密钥

按所选服务商配置 API Key：

- OpenAI: `OPENAI_API_KEY`
- Anthropic: `ANTHROPIC_API_KEY`
- xAI: `XAI_API_KEY`
- Google: `GEMINI_API_KEY` (aliases: `GOOGLE_GENERATIVE_AI_API_KEY`, `GOOGLE_API_KEY`)

若未设置，默认模型为 `google/gemini-3-flash-preview`。

## 常用参数

- `--length short|medium|long|xl|xxl|<chars>`
- `--max-output-tokens <count>`
- `--extract-only` (URLs only)
- `--json` (machine readable)
- `--firecrawl auto|off|always` (fallback extraction)
- `--youtube auto` (Apify fallback if `APIFY_API_TOKEN` set)

## 配置

Optional config file: `~/.summarize/config.json`

```json
{ "model": "openai/gpt-5.2" }
```

可选服务：
- `FIRECRAWL_API_KEY` for blocked sites
- `APIFY_API_TOKEN` for YouTube fallback

## 风险与边界

- 该 skill 依赖本机 CLI，不存在时不要假装已经执行
- 视频与播客的转写是尽力而为，不保证逐字精确
- 长文档或长视频优先给摘要，再按用户需要展开
