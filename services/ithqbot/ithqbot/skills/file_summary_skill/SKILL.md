---
name: file_summary_skill
description: 读取 file_id 对应文件内容并生成摘要，再写回为新文件。
---

# 文件摘要技能

## 功能

- 输入 `file_id`
- 调用 `context.read_file` 读取文件
- 调用 `context.call_llm` 生成摘要
- 调用 `context.save_file` 保存摘要
- 返回新的 `file_id`

## 输入

- `file_id` (string, required)
- `output_name` (string, optional, 默认 `summary.txt`)
- `output_mime` (string, optional, 默认 `text/plain`)

## 输出

- `new_file_id` (string)
- `source_file_id` (string)
