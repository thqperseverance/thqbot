---
name: file_to_markdown
description: 将对象存储文件提交到外部转换服务并回传 Markdown 文件；当用户需要把附件、文档或对象存储文件转成 Markdown 时触发。
metadata:
  ithqbot:
    capability: ["file", "conversion", "markdown"]
    tags: ["minio", "document", "markdown"]
    level: atomic
    planner:
      input_from: ["minio_fetch"]
      output_to: ["document_parse", "knowledge_ingest"]
    idempotent: true
    retryable: true
    cost:
      level: medium
    latency:
      expected_ms: 20000
---

# 文件转 Markdown

## 目标

调用 `file_to_markdown` 工具，把对象存储中的原始文件发送给配置好的转换服务，拿回结果压缩包后提取 Markdown 文件，并重新上传到对象存储供后续下载和消费。

## 何时使用

- 用户要求把附件、报告、制度文档或知识材料转成 Markdown
- 上游已经拿到对象存储 `bucket` 与 `object_names`
- 需要为知识入库、文档解析或后续摘要生成准备 Markdown 版本
- 需要返回可下载的标准化文件结果

## 输入约束

- `bucket` 必填，表示源文件所在的对象存储 bucket
- `object_names` 必填，为待转换对象 key 数组
- `save_dir` 选填，仅作为临时工作目录根路径
- 运行环境需配置转换服务地址和可用的对象存储连接信息
- 输入对象必须已经存在于对象存储，否则工具会直接失败

## 执行步骤

1. 从对象存储下载源文件到临时目录
2. 调用外部转换服务创建批处理任务
3. 轮询转换任务状态直到完成或失败
4. 下载转换结果压缩包并提取 Markdown 文件
5. 将 Markdown 文件上传回对象存储，并返回标准 `files[]`

## 输出约定

- 成功时返回 JSON 字符串，包含 `status`、`message`、`data`、`files`
- `data` 至少包含 `batch_no`、`source_bucket`、`source_objects`、`uploaded_count`
- `files[]` 中每个对象都应优先包含 `name`、`mime`、`size`、`storage_uri`、`download_url`、`storage_backend`、`storage_bucket`、`storage`，并兼容保留 `rel_path`、`minio_uri`、`s3_uri`
- 失败时返回可读错误信息，不暴露底层异常堆栈

## 示例

```json
{
  "bucket": "ithqbot-storage",
  "object_names": [
    "demo/input/contract.docx",
    "demo/input/specification.pdf"
  ]
}
```

## 风险与边界

- 转换质量依赖外部服务能力，复杂版式可能存在丢失或错位
- 工具仅处理对象存储中已存在的对象，不负责上传源文件
- 若转换服务、对象存储或网络异常，任务可能失败或超时
