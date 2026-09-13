/**
 * 调试用快捷预设：顶部胶囊标签点击后把提示词填进输入框。
 * 面向 Agent 调试场景 —— 常用能力一键起手，省去手打长提示词。
 */

export interface ToolPreset {
  id: string;
  label: string;
  hint: string;
  prompt: string;
}

export const TOOL_PRESETS: ToolPreset[] = [
  {
    id: "text_stats",
    label: "text_stats",
    hint: "调用自研技能做确定性文本统计",
    prompt: "用 text_stats 工具统计这段话，并用 Markdown 表格给出结果：\n",
  },
  {
    id: "doc_compare",
    label: "doc_compare",
    hint: "对比两份文档（需先添加两个附件）",
    prompt: "请对比这两份文档的差异",
  },
  {
    id: "check_skill",
    label: "check_skill",
    hint: "技能合规审计（PASS / WARN / FAIL）",
    prompt: "请用 check_skill 工具检查 text_stats 这个技能是否符合 SKILL_STANDARDS 标准",
  },
  {
    id: "read_file",
    label: "read_file",
    hint: "读取技能说明或工作区文件",
    prompt: "请用 read_file 读取 text_stats 技能的 SKILL.md，并说明它具备什么能力",
  },
  {
    id: "otp",
    label: "otp",
    hint: "包含人机校验（OTP）的交互式流程",
    prompt: "请生成对比报告；如果需要下载，请提示我输入验证码后再继续",
  },
];
