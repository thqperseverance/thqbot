# ithqbot 技能说明

本目录存放用于扩展 ithqbot 能力的内置技能。

## 目录结构

每个技能对应一个目录。最小兼容结构如下：

```text
ithqbot/skills/<skill_name>/
  └── SKILL.md
```

可执行技能还可以包含：

```text
ithqbot/skills/<skill_name>/
  ├── SKILL.md
  ├── tool_def.json
  ├── tool.py
  ├── implementation.py
  ├── requirements.txt
  ├── plugin.json
  └── tests/
```

约定如下：

- `SKILL.md` 为必需文件，沿用 Claude Skills 风格的 frontmatter。
- `tool_def.json` 建议为可执行技能提供，契约格式与 aiexcel 风格保持一致。
- `tool.py` 用于动态注册运行时工具。
- `<workspace>/skills/<skill_name>/` 下的工作区技能会覆盖同名内置技能。

## Frontmatter

`SKILL.md` frontmatter 建议至少包含：

```yaml
---
name: "<skill_name>"
description: "<what it does + when to invoke it>"
metadata: {"ithqbot": {...}}
---
```

`metadata` 支持 ithqbot 特有字段，例如依赖要求、安装提示、表情符号以及常驻加载配置。

## 兼容性

技能入口格式兼容 Claude / OpenClaw 风格的 `SKILL.md` 发现机制，同时扩展了 ithqbot 的运行时约定：

- metadata 透传
- 通过 `agents.purposes` 选择模型
- 基于 MinIO 的文件结果回传
- 支持 OTP / 选择 / 表单类交互载荷

统一标准请参见 `ithqbot/docs/SKILL_STANDARDS.md`，兼容性说明请参见 `ithqbot/docs/SKILL_SPEC.md`。
