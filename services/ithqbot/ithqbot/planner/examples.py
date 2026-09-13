EXAMPLES = """
用户需求：分析告警并给出根因

输出：
{
  "graph_id": "fault_analysis",
  "nodes": [
    {"id": "step1", "skill": "alarm_query"},
    {"id": "step2", "skill": "log_analysis", "input_mapping": {"alarm_id": "state.alarm_id"}},
    {"id": "step3", "skill": "root_cause_summary", "input_mapping": {"logs": "state.logs"}}
  ],
  "edges": [
    {"from": "step1", "to": "step2"},
    {"from": "step2", "to": "step3"}
  ]
}

用户需求：对比两个文档并生成报告

输出：
{
  "graph_id": "doc_compare_flow",
  "nodes": [
    {"id": "step1", "skill": "doc_load"},
    {"id": "step2", "skill": "doc_compare", "input_mapping": {"left_doc": "state.left_doc", "right_doc": "state.right_doc"}},
    {"id": "step3", "skill": "report_generate", "input_mapping": {"diff": "state.diff"}}
  ],
  "edges": [
    {"from": "step1", "to": "step2"},
    {"from": "step2", "to": "step3"}
  ]
}

用户需求：根据审批结果选择执行路径并汇总结果

输出：
{
  "graph_id": "approval_flow",
  "nodes": [
    {"id": "step1", "skill": "approval_check"},
    {"id": "step2", "skill": "approve_action", "input_mapping": {"request_id": "state.request_id"}, "output_mapping": {"branch_result": "data.message"}},
    {"id": "step3", "skill": "reject_action", "input_mapping": {"request_id": "state.request_id"}, "output_mapping": {"branch_result": "data.message"}},
    {"id": "step4", "skill": "result_summary", "input_mapping": {"result": "state.branch_result"}}
  ],
  "edges": [
    {"from": "step1", "to": "step2", "condition": "state.approved == true"},
    {"from": "step1", "to": "step3", "condition": "state.approved == false"},
    {"from": "step2", "to": "step4"},
    {"from": "step3", "to": "step4"}
  ]
}
""".strip()
