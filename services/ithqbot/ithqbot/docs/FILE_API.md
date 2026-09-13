# File API 设计与使用

## 目标

在 Skill 直接读写文件的同时，保证严格多租户隔离（`tenant_id/account_id/bot_id`）与全链路可观测。

## 核心接口

`SkillContext` 现提供以下 API：

- `save_file(name, content, mime="application/octet-stream") -> dict`
- `read_file(file_id) -> str | bytes`
- `get_file(file_id) -> dict`
- `list_files(limit=50) -> list[dict]`

返回模型统一使用 `file_id`，不暴露底层存储路径。

## 隔离与路径规范

每个文件必须绑定：

- `tenant_id`
- `account_id`
- `bot_id`
- `chat_id`
- `request_msg_id`
- `trace_id`

底层对象路径强制为：

`<tenant_id>/<account_id>/<bot_id>/<chat_id>/<request_msg_id>/<file_id>/<filename>`

`file_id` 使用 `UUID + SHA256` 生成，避免可枚举扫描。

## 安全策略

- 强制作用域校验：`tenant/account/bot` 任一不匹配即拒绝访问。
- 默认大小上限：`10MB`（可配置）。
- 支持流式读取：`FileService.read_stream()`，避免大文件一次性加载。
- 下载链接使用签名 URL，默认 10 分钟有效。

## 兼容策略

对外附件元数据优先使用 `storage_uri` 作为统一对象存储地址，例如 `minio://bucket/path` 或 `s3://bucket/path`。`minio_uri`、`s3_uri`、`rel_path` 仅作为兼容字段保留给旧调用方。

旧技能若返回：

```json
{
  "files": [
    {"name": "x.md", "minio_uri": "minio://bucket/path"}
  ]
}
```

运行时会自动转换为：

```json
{
  "files": [
    {"file_id": "f_xxx"}
  ]
}
```

如果运行时已经拿到标准化文件元数据，推荐返回：

```json
{
  "files": [
    {
      "name": "x.md",
      "storage_uri": "s3://bucket/path",
      "download_url": "/chat/files/download?p=token"
    }
  ]
}
```

## 可观测性

文件读写会记录 trace 事件，至少包含：

- `trace_id`
- `request_msg_id`
- `file_id`
- `operation` (`read` / `write`)

Skill 侧可通过 `context.emit_progress()` 推送文件处理进度。

## 配置示例

```yaml
tools:
  object_storage:
    backend: "minio" # 也可切换为 s3
    endpoint: "127.0.0.1:9000"
    access_key: "minioadmin"
    secret_key: "minioadmin"
    bucket: "ithqbot-storage"
    secure: false
  file_api:
    max_file_size_bytes: 10485760
    download_url_ttl_seconds: 600
    metadata_prefix: "_meta/files"
```

## 扩展预留

文件元数据包含可扩展字段：

- `tags`
- `source_skill`
- `expire_at`

后续能力可基于该结构扩展：

- 同租户跨 Skill 文件共享
- 文件向量化与 RAG
- RBAC 细粒度权限控制
