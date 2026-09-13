---
name: knowledge_retrieval
description: 从已配置知识库检索证据并生成中文回答；当用户询问已收录业务知识、制度说明或产品规则时触发。
metadata:
  ithqbot:
    capability: ["knowledge", "retrieval", "qa"]
    tags: ["search", "knowledge-base", "answer"]
    level: atomic
    planner:
      output_to: ["answer_generation"]
      incompatible_with: ["raw_llm"]
    idempotent: true
    retryable: true
    cost:
      level: low
    latency:
      expected_ms: 3000
---

# 知识库检索

## 目标

调用 `retrieve_knowledge` 工具，从配置好的知识库召回相关片段，并输出面向用户的中文答案与关键证据摘要。

## 何时使用

- 用户提问内容明显依赖企业私有知识库、业务知识库或制度文档
- 需要先检索证据，再据此给出回答
- 需要避免模型脱离知识库自由发挥
- 用户指定了知识域、专题库或业务范围

## 输入约束

- `query` 必填，应为用户问题或检索语句
- `domain` 选填；若提供，优先作为知识库名称使用
- 运行环境需配置 `ITHQBOT_KNOWLEDGE_API_URL`
- 若知识库无命中，应明确告知而不是编造答案

## 执行步骤

1. 读取用户问题，整理为检索语句
2. 调用 `retrieve_knowledge(query=..., domain=...)`
3. 获取知识库召回片段并进行证据整理
4. 基于召回证据生成中文回答
5. 将回答与关键片段返回给用户

## 输出约定

- 成功时返回 JSON 字符串，包含 `status`、`message`、`data`
- `data` 至少包含 `query`、`domain`、`hit_count`、`answer`
- `data.passages` 返回最多 5 条关键证据片段，便于下游复用
- 失败时返回可读错误信息，不暴露原始堆栈

## 示例

```json
{
  "query": "请介绍当前套餐包含哪些权益",
  "domain": "marketkgpool"
}
```

## 风险与边界

- 检索结果质量依赖知识库收录内容与召回质量
- 未命中时只能返回“未检索到”，不能替代知识录入
- 若外部知识检索接口未配置或不可用，应直接返回明确错误
