---
name: text_stats
description: 统计一段文本的字数、行数、段落数、中英文词数与高频关键词；当用户要求"统计文本/字数统计/关键词提取/文本概览"时使用。
metadata: {"ithqbot":{"emoji":"📊","capability":["analysis"],"tags":["text","stats"],"level":"atomic","idempotent":true,"retryable":true,"cost":{"level":"low"},"latency":{"expected_ms":50}}}
---

# 文本统计

## 目标

对一段纯文本做确定性统计：字符数、行数、段落数、中英文词数、预计朗读时长，以及去掉停用词后的高频关键词。

## 何时使用

- 用户要求"统计这段文字的字数 / 行数"
- 用户要求"提取关键词 / 看看这段文本的主题"
- 在生成摘要之前，先了解文本规模

## 输入约束

- `text`：必填，待统计的文本。
- `top_n`：可选，返回的高频关键词个数，默认 5，范围 1~50。

## 执行步骤

1. 调用 `text_stats(text=..., top_n=...)`
2. 读取返回的 `data`：`chars_total`、`lines`、`paragraphs`、`words_cjk`、`words_latin`、`reading_seconds`、`keywords`
3. 用中文向用户汇报，不要原样抛 JSON

## 输出约定

返回结构：

```json
{
  "status": "success",
  "message": "统计完成",
  "data": {
    "chars_total": 120,
    "chars_no_space": 108,
    "lines": 6,
    "paragraphs": 3,
    "words_cjk": 42,
    "words_latin": 18,
    "words_total": 60,
    "reading_seconds": 20,
    "keywords": [{"word": "平台", "count": 5}]
  }
}
```

## 示例

用户："帮我统计一下这段话有多少字，主要讲什么"
→ `text_stats(text="<用户提供的文本>", top_n=5)`

## 风险与边界

- 纯本地确定性计算，不调用模型、不访问网络。
- 中文按"每字一词"统计，英文按空白/标点切分。
- 关键词仅做词频统计，不做语义聚类。
