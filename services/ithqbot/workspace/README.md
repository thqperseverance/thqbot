# thqbot agent 工作区

该目录会以 bind mount 方式挂载到 ithqbot 容器的 `/workspace/workspace`。

- `docs/`：演示用的样例文档（用于验证 `doc_compare` 技能）。
- `skills/`：工作区级技能（同名会覆盖内置技能）。
- 其它文件：agent 可读写的工作区内容（`tools.restrictToWorkspace=true` 限制了边界）。

演示提示词示例：

- 「对比 docs/spec-v1.md 和 docs/spec-v2.md 的差异」
- 「检查 text_stats 这个技能是否符合标准」
- 「统计下面这段话的字数和关键词：……」
