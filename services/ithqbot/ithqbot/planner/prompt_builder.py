def build_prompt(user_query: str, skills_text: str, examples: str, repair_hint: str | None = None) -> str:
    repair_section = ""
    if repair_hint:
        repair_section = f"""

【上一轮失败原因】
{repair_hint}

请修复上述问题后重新生成合法 DAG，不要重复同样错误。
""".rstrip()
    return f"""
你是一个AI工作流规划器（Graph Planner）。

目标：
将用户需求拆解为多个Skill，并生成DAG（有向无环图）。

【用户需求】
{user_query}

【可用Skills】
{skills_text}

【要求】
1. 输出JSON，不要解释
2. 必须包含：
   - graph_id
   - nodes（每个节点包含 id 和 skill）
   - edges（from → to）
3. 节点必须使用已有skill名称
4. DAG必须无环
5. 尽量优先复用已有 Skill，并按最少必要节点完成任务，避免无意义拆分
6. 如果存在条件分支，可添加 condition
7. 输入输出通过“state变量”传递
8. 节点 ID 必须唯一
9. 多节点图中不要输出孤立节点
10. 如需变量映射，优先使用 input_mapping / output_mapping
11. 优先参考每个 Skill 的 Input/Output/Semantic/Planner 约束来决定上下游连接
12. 如果某个 Skill 的 `Planner.input_from` 或 `Planner.output_to` 给出限制，必须遵守
13. 如果上下游 Skill 的 `Semantic.produces` 与 `Semantic.consumes` 明显不匹配，不要强行连边
14. 避免把标记为 `level=composite` 的 Skill 再拆成其内部等价子步骤，除非用户明确要求细化

【输出格式】
{{
  "graph_id": "...",
  "nodes": [
    {{
      "id": "step1",
      "skill": "...",
      "input_mapping": {{"foo": "state.bar"}},
      "output_mapping": {{"bar": "data.foo"}}
    }}
  ],
  "edges": [
    {{
      "from": "step1",
      "to": "step2",
      "condition": "state.approved == true"
    }}
  ]
}}

【示例】
{examples}
{repair_section}

请生成DAG：
""".strip()
