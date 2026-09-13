import type { Conversation } from "../types";
import { ChatIcon, MenuIcon, TrajectoryIcon } from "./Icons";
import QuickTags from "./QuickTags";

export type TabKey = "chat" | "trajectory";

interface Props {
  conversation: Conversation | null;
  botId: string;
  running: boolean;
  failed: boolean;
  messageCount: number;
  skillCount: number;
  toolCount: number;
  trajectoryCount: number;
  tab: TabKey;
  onTabChange: (tab: TabKey) => void;
  onOpenSidebar: () => void;
  onPickPreset: (prompt: string) => void;
}

/**
 * 顶部：面包屑（任务名 + 状态胶囊）与 对话/轨迹 Tab。
 *
 * 对齐 DSH ConversationSessionHeader：`.titleRow`（min-height 44px、左侧 20px）
 * + `.tabs`（gap 36px、13px/16px、选中 2px 蓝色下划线压在同一条分隔线上）。
 * 「子代理数量」在 thqbot 里没有真实数据，这里换成真实可得的运行指标，
 * 不伪造数字。
 */
export default function TopBar({
  conversation,
  botId,
  running,
  failed,
  messageCount,
  skillCount,
  toolCount,
  trajectoryCount,
  tab,
  onTabChange,
  onOpenSidebar,
  onPickPreset,
}: Props) {
  const statusClass = failed ? "is-error" : running ? "is-running" : "is-idle";
  const statusText = failed ? "运行失败" : running ? "运行中" : "空闲";

  return (
    <header className="topbar">
      <div className="topbar-row">
        <button className="icon-button only-mobile" title="会话列表" onClick={onOpenSidebar}>
          <MenuIcon size={16} />
        </button>

        <div className="breadcrumb">
          <span className="breadcrumb-root">thqbot</span>
          <span className="breadcrumb-sep">/</span>
          <span className="breadcrumb-current" title={conversation?.title || "新会话"}>
            {conversation?.title || "新会话"}
          </span>
        </div>

        <QuickTags onPick={onPickPreset} disabled={!conversation} />

        <div className="topbar-meta">
          <span className={`status-pill ${statusClass}`}>
            <span className="status-dot" />
            {statusText}
          </span>
          <span className="capsule capsule-optional">{botId}</span>
          <span className="capsule capsule-optional">消息 {messageCount}</span>
          <span className="capsule capsule-optional">技能 {skillCount}</span>
          <span className="capsule capsule-optional">工具 {toolCount}</span>
        </div>
      </div>

      <div className="tabs">
        <button
          className={`tab${tab === "chat" ? " is-active" : ""}`}
          onClick={() => onTabChange("chat")}
        >
          <ChatIcon size={13} />
          <span style={{ marginLeft: 6 }}>对话</span>
        </button>
        <button
          className={`tab${tab === "trajectory" ? " is-active" : ""}`}
          onClick={() => onTabChange("trajectory")}
        >
          <TrajectoryIcon size={13} />
          <span style={{ marginLeft: 6 }}>轨迹</span>
          {trajectoryCount > 0 && <span className="tab-count">{trajectoryCount}</span>}
        </button>
      </div>
    </header>
  );
}
