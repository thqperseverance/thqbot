---
name: official_doc_review
description: 审查上传公文的格式与内容质量并给出修改建议；当用户上传 OA 文档后直接说“帮我审查这个公文”、要求智审、格式审查、错别字检查或敏感词排查时触发。
metadata:
  ithqbot:
    capability: ["document", "review", "oa"]
    tags: ["official-doc", "quality-check", "format-review", "oa"]
    level: atomic
    planner:
      input_from: ["document_file", "minio_fetch"]
      output_to: ["answer_generation", "file_download"]
      preferred_after: ["file_upload"]
    idempotent: true
    retryable: true
    cost:
      level: medium
    latency:
      expected_ms: 15000
---

# 公文智审

## 目标

调用 `official_doc_review` 工具，对用户上传的 OA 文档进行多维度质量审查，输出问题清单、风险提示和修改建议；在需要时生成可下载的审查报告。

## 何时使用

- 用户上传文档后，明确要求做公文格式审查、智审、质检、审核或校对
- 用户询问 OA 文、请示、通知、报告、纪要等是否符合公文规范
- 用户希望检查错别字、政治敏感词、标点、序号层级、重复表述、术语一致性
- 用户要求“帮我看这个文档有没有问题”“检查一下这个公文格式对不对”
- 用户先上传附件，下一句直接说“帮我审查这个公文”“看下这份 OA 文有没有问题”“把刚上传的通知审一下”

## 输入约束

- 优先从消息里的 `attachments`、`files`、`file_meta` 中提取文件来源
- 优先使用 `storage_uri`，其次才回退到 `file_id`、`s3_uri`、`minio_uri`、`rel_path`
- 如果已经拿到平台文件 `file_id`，优先直接传给工具读取
- 推荐输入 `.docx`，因为只有 `.docx` 能做较完整的版式审查；`.pdf`、`.txt`、`.md` 主要做文本质量审查
- 默认审查以下维度：`format`、`typo`、`political`、`punctuation`、`sequence`、`redundancy`、`terminology`

## 执行步骤

1. 收集上传文件的 `storage_uri` 或 `file_id`
2. 调用 `official_doc_review(...)` 读取原文内容
3. 若文件为 `.docx`，补充检查标题层级、字体、字号、段落、表格字体、彩色字、加粗等基础版式问题
4. 结合规则与模型分析错别字、政治敏感词、标点、序号逻辑、重复表达和术语一致性
5. 将问题按维度输出，并给出可执行的修改建议
6. 若用户需要报告文件，再生成并返回 `files[]`

## 输出约定

- 成功时返回 JSON 字符串，至少包含 `status`、`message`、`summary`、`problems`
- `problems[]` 中每项包含 `dimension`、`severity`、`location`、`description`、`suggestion`
- 如果生成报告文件，返回 `files[]`，其中优先包含 `storage_uri`、`download_url`，并兼容 `rel_path`、`minio_uri`、`s3_uri`
- 如果文件无法读取或格式暂不支持，应返回可读错误信息，不暴露底层堆栈

## 示例

常见触发话术：

- 帮我审查这个公文
- 请检查下刚上传这份 OA 文的格式
- 这个通知有没有错别字和敏感词
- 把附件里的请示按公文规范审一遍
- 看看这份纪要的标题层级和序号有没有问题

直接审查上传文件：

```json
{
  "storage_uri": "s3://tenant-files/user123/chat456/oa/notice.docx"
}
```

使用平台文件 ID 审查，并生成报告：

```json
{
  "file_id": "f_1234567890",
  "generate_report": true
}
```

指定维度审查：

```json
{
  "storage_uri": "minio://ithqbot-storage/user123/chat456/oa/report.docx",
  "review_dimensions": ["format", "sequence", "terminology"]
}
```

## 风险与边界

- `.docx` 之外的格式无法可靠识别字号、字体、段距、行距等版式属性
- 即使是 `.docx`，也可能因样式继承、模板异常或复杂嵌套表格导致部分版式判断为启发式结果
- 错别字、政治敏感词、语义重复和术语一致性带有语言理解特征，应以人工复核为准
- 当前工具输出的是问题发现与修改建议，不直接改写原文版式
