---
name: doc_compare
description: 比较两份本地或对象存储文档并生成差异报告；当用户要求对比附件、配置、合同或版本文件时立即使用。
metadata:
  ithqbot:
    capability: ["document", "compare", "diff"]
    tags: ["doc-compare", "file-diff", "report"]
    level: atomic
    planner:
      input_from: ["minio_fetch"]
      output_to: ["answer_generation", "file_download"]
    idempotent: true
    retryable: true
    cost:
      level: medium
    latency:
      expected_ms: 12000
---

# 文档对比

## 目标

使用 `doc_compare` 工具比较两份文档，输出中文差异摘要，并在需要时生成可下载的对象存储报告文件。

## 何时使用

当用户表达以下意图时优先使用：

- 比较两个文档、两个配置、两个版本
- 对比刚上传的附件或对象存储文件
- 检查合同、方案、代码片段、制度文件的差异
- 生成可下载的差异报告

## 输入约束

- `path1` / `path2` 必填，可为绝对路径、相对路径或对象存储 URI（如 `minio://bucket/path`、`s3://bucket/path`）
- 若是飞书或 Web 上传文件，优先从消息元数据里的 `attachments`、`files`、`file_meta` 中提取 `storage_uri`，再回退到 `s3_uri`、`minio_uri` 或 `rel_path`
- 如果用户给的是默认桶下的相对路径，也可以直接传给工具，由工具补全为默认对象存储路径
- 首次调用默认返回摘要与 OTP 交互载荷；只有在明确需要下载报告时，才继续走下载流程

## 执行步骤

1. 收集两份文档路径，尽量保留原始来源
2. 调用 `doc_compare(path1=..., path2=...)`
3. 直接向用户转述工具返回的对比摘要
4. 若用户需要下载报告，再调用 `doc_compare(download=true, otp_code=...)`
5. 下载阶段若返回 `files[]`，明确告知用户这是可下载结果文件

## 输出约定

- 摘要阶段：返回中文差异说明，并附带 OTP 交互载荷
- 下载阶段：返回 `files[]`，其中优先包含 `storage_uri`、`download_url`，并兼容返回 `rel_path`、`minio_uri`、`s3_uri`、`storage_backend`、`storage_bucket`
- 如果验证码错误，应提示用户重新输入，不要伪造下载结果

## 示例

```json
{
  "path1": "minio://ithqbot-storage/user_a/chat_1/old_ver.txt",
  "path2": "minio://ithqbot-storage/user_a/chat_1/new_ver.txt"
}
```

下载报告：

```json
{
  "path1": "minio://ithqbot-storage/user_a/chat_1/old_ver.txt",
  "path2": "minio://ithqbot-storage/user_a/chat_1/new_ver.txt",
  "download": true,
  "otp_code": "246810"
}
```

## 风险与边界

- 不要在未拿到两份有效文档路径前盲目调用
- 超大文件可能被截断后再比较，应向用户说明结果是摘要级别
- 当前下载验证码为固定值，仅用于演示链路，不代表生产级鉴权方案
