/** SSR 烟测入口：被 render.test.mjs 用 esbuild 打包后执行，用来真实渲染组件。 */
import { renderToString } from "react-dom/server";

import LoginScreen from "../src/components/LoginScreen";
import MessageBubble from "../src/components/MessageBubble";
import QuickTags from "../src/components/QuickTags";
import RichText from "../src/components/RichText";
import Sidebar from "../src/components/Sidebar";
import TopBar from "../src/components/TopBar";
import TrajectoryPanel from "../src/components/TrajectoryPanel";
import Welcome from "../src/components/Welcome";
import type { Conversation, Message, TrajectoryEntry, User } from "../src/types";

const SAMPLE_MARKDOWN = [
  "统计完成 ✅",
  "",
  "## 结果",
  "",
  "**文本统计**",
  "",
  "- 总字符数：45",
  "  - 其中中文 12",
  "- 英文词数：4",
  "",
  "1. 先看规模",
  "2. 再抽关键词",
  "",
  "> 提示：中文按字计词",
  "",
  "| 指标 | 值 |",
  "| --- | --- |",
  "| chars | 45 |",
  "",
  "```json",
  '{"chars_total": 45}',
  "```",
  "",
  "危险链接：[点我](javascript:alert(1))，安全链接：[文档](https://example.com)",
].join("\n");

const MESSAGE: Message = {
  message_id: "m-1",
  conversation_id: "c-1",
  sender: "bot",
  content: SAMPLE_MARKDOWN,
  content_type: "text",
  status: "received",
  created_at: new Date().toISOString(),
  meta: {
    attachments: [{ file_id: "f-1", name: "spec-v1.md", size: 1190, download_url: "/api/files/f-1" }],
    // 技能产出文件（files[] 契约）：渲染成文件变更卡片
    files: [{ name: "report.md", description: "对比报告" }],
    usage: { prompt_tokens: 6483, completion_tokens: 340, calls: 3 },
    latency_ms: 1230,
    progress: { skills: ["text_stats"], tools: ["read_file"] },
  },
};

export function renderAll(): Record<string, string> {
  const conversation: Conversation = {
    conversation_id: "c-1",
    title: "统计文本",
    bot_id: "bot_A",
    unread_count: 2,
    last_message_preview: "总字符数 45",
    last_message_at: new Date().toISOString(),
  };
  const user: User = { user_id: "u-1", username: "admin", display_name: "管理员" };
  const trajectory: TrajectoryEntry[] = [
    { id: "t1", at: Date.now(), conversationId: "c-1", kind: "run", label: "消息已入队" },
    {
      id: "t2",
      at: Date.now(),
      conversationId: "c-1",
      kind: "skill",
      label: "text_stats",
      detail: "技能调用",
    },
    {
      id: "t3",
      at: Date.now(),
      conversationId: "c-1",
      kind: "tool",
      label: "read_file",
      detail: "工具调用",
    },
  ];

  return {
    rich: renderToString(<RichText content={SAMPLE_MARKDOWN} />),
    welcome: renderToString(<Welcome onPick={() => undefined} />),
    login: renderToString(<LoginScreen onSuccess={() => undefined} />),
    bubble: renderToString(<MessageBubble message={MESSAGE} />),
    tags: renderToString(<QuickTags onPick={() => undefined} />),
    topbar: renderToString(
      <TopBar
        conversation={conversation}
        botId="bot_A"
        running
        failed={false}
        messageCount={6}
        skillCount={1}
        toolCount={2}
        trajectoryCount={3}
        tab="chat"
        onTabChange={() => undefined}
        onOpenSidebar={() => undefined}
        onPickPreset={() => undefined}
      />,
    ),
    trajectory: renderToString(<TrajectoryPanel entries={trajectory} />),
    sidebar: renderToString(
      <Sidebar
        conversations={[conversation]}
        activeId="c-1"
        user={user}
        botId="bot_A"
        connected
        onSelect={() => undefined}
        onNew={() => undefined}
        onSettings={() => undefined}
        onLogout={() => undefined}
        onClose={() => undefined}
      />,
    ),
  };
}
