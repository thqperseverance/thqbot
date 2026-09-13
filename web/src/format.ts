/** 展示层格式化工具。 */

export function formatTime(value?: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

export function formatSize(bytes?: number): string {
  if (!bytes || bytes <= 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** 用于按天分组的键（本地时区）。 */
export function dayKey(value?: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

function startOfDay(date: Date): number {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

/** 今天 / 昨天 / 具体日期。 */
export function formatDayLabel(value?: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const today = startOfDay(new Date());
  const target = startOfDay(date);
  const dayMs = 24 * 60 * 60 * 1000;
  if (target === today) return "今天";
  if (target === today - dayMs) return "昨天";
  const sameYear = date.getFullYear() === new Date().getFullYear();
  return date.toLocaleDateString("zh-CN", {
    year: sameYear ? undefined : "numeric",
    month: "long",
    day: "numeric",
  });
}

/** 列表里的时间：今天显示时刻，昨天/更早显示日期。 */
export function formatListTime(value?: string | null): string {
  if (!value) return "";
  const label = formatDayLabel(value);
  if (label === "今天") return formatTime(value);
  return label;
}

const STAGE_LABELS: Record<string, string> = {
  queued: "排队中",
  parsing: "理解请求",
  extracting: "整理上下文",
  tool_call: "执行步骤",
  skill_call: "执行技能",
  finalizing: "整理结果",
};

/** 把后端返回的进度阶段码翻译成中文。 */
export function stageLabel(stage?: string | null): string {
  if (!stage) return "处理中";
  return STAGE_LABELS[stage] ?? stage;
}
