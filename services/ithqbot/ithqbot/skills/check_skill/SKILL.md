---
name: check_skill
description: 检查指定 skill 是否满足 SKILL_STANDARDS.md 标准；当用户要求技能合规审计、发布前验收或改造回归时使用。
metadata: {"ithqbot":{"emoji":"✅","always":false}}
---

# Skill 合规检查

## 目标

使用 `check_skill` 工具对 `ithqbot/skills/<skill_name>` 或打包 skill 文件做结构化审计，判断是否符合 `ithqbot/docs/SKILL_STANDARDS.md`，并补充代码缺陷与安全风险检测。

## 何时使用

- 用户要求“检查某个 skill 是否符合标准”
- 新建 skill 后做发布前验收
- 改造老 skill 后做回归审计
- 批量治理 skills 时做统一质量门槛检查

## 输入约束

- `skill_name` 与 `bundle_path` 至少提供一个
- `skill_name`：`ithqbot/skills` 下的目录名
- `bundle_path`：待上线 skill 压缩包路径，支持 `.zip/.tar/.tar.gz/.tgz/.tar.bz2/.tbz2/.tar.xz/.txz`
- `strict` 可选，默认 `false`
- `strict=true` 时，推荐项也按阻塞项处理

## 执行步骤

1. 调用 `check_skill(skill_name=..., strict=...)` 或 `check_skill(bundle_path=..., skill_name=..., strict=...)`
2. 读取 `PASS/WARN/FAIL` 分组结果
3. 若存在 `FAIL`，按 `Fix Plan` 逐项修复
4. 修复后再次调用工具，直到无阻塞项

## 输出约定

- 返回纯文本审计报告
- 必含 `PASS`、`WARN`、`FAIL`、`Fix Plan`
- `FAIL` 代表必须整改项；`WARN` 代表建议项
- 对代码会附加静态风险扫描结果（缺陷模式、安全风险、外部链接风险）
- 对工具进度上报会附加“内部状态可追踪”检查（如 `progress_stage/status_details/tool_name/skill_name/call_type`）

## 示例

```json
{
  "bundle_path": "/tmp/doc_compare.tar.gz",
  "skill_name": "doc_compare",
  "strict": false
}
```

```json
{
  "skill_name": "doc_compare",
  "strict": true
}
```

结果解读：
- `strict=false`：建议项进入 `WARN`，阻塞项进入 `FAIL`
- `strict=true`：建议项会升级为阻塞项，进入 `FAIL`
- 若报告出现“内部状态可追踪字段缺失”，优先补齐 `progress_stage/status_details/tool_name/skill_name/call_type`

## 风险与边界

- 本工具以静态检查为主，无法覆盖全部运行时分支
- 对“语义质量”仅做规则化近似判断，最终以评审结论为准
- 压缩包检查会拒绝路径穿越与链接文件
