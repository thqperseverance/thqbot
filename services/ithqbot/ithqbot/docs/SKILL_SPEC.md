# Skill 兼容性规范（SKILL_SPEC）

`SKILL_SPEC.md` 保留为兼容性与迁移索引文档。

完整标准请以 `ithqbot/docs/SKILL_STANDARDS.md` 为准。

## 1. 定位

- `SKILL_STANDARDS.md`：唯一权威规范（目录、契约、上下文、进度、文件、质量门槛）
- `SKILL_SPEC.md`：外部格式兼容说明（Claude/OpenClaw 风格接入）

## 2. 兼容性结论

ithqbot 兼容 Claude/OpenClaw 风格 `SKILL.md`，同时增加工程化运行能力：
- 身份与元数据透传（`tenant_id/account_id/request_msg_id/metadata`）
- 模型用途化调用（`context.call_llm(task=...)`）
- 文件回传标准化（`files[]` + MinIO 语义）
- 交互式消息（`message.interaction`）

## 3. 最小可迁移要求

外部 Skill 迁入 ithqbot 时，建议最少补齐：
1. `SKILL.md` frontmatter：`name/description`
2. 可执行 Skill 的输入契约：`schema.json` 或 `tool/tool_def.json`
3. 结果引用链路：`request_msg_id/reply_to`
4. 文件返回结构：优先 `files[]`

## 4. 推荐迁移路径

1. 保留原有 `SKILL.md` 文案结构
2. 增加 ithqbot 所需 metadata 透传意识
3. 将执行逻辑收敛到 `tool/tool.py`（或保留 runtime 并逐步迁移）
4. 按 `SKILL_STANDARDS.md` 补测试与质量门槛
