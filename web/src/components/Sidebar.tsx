import { useMemo, useState } from "react";

import { formatListTime } from "../format";
import type { Conversation, User } from "../types";
import { CloseIcon, LogoutIcon, PlusIcon, SearchIcon, SettingsIcon, SparkIcon } from "./Icons";

interface Props {
  conversations: Conversation[];
  activeId: string | null;
  user: User | null;
  botId: string;
  connected: boolean;
  onSelect: (conversationId: string) => void;
  onNew: () => void;
  onSettings: () => void;
  onLogout: () => void;
  onClose: () => void;
}

/** 左侧固定窄侧边栏：品牌 / 搜索 / 会话列表 / 底部设置。 */
export default function Sidebar({
  conversations,
  activeId,
  user,
  botId,
  connected,
  onSelect,
  onNew,
  onSettings,
  onLogout,
  onClose,
}: Props) {
  const [filter, setFilter] = useState("");

  const visible = useMemo(() => {
    const keyword = filter.trim().toLowerCase();
    if (!keyword) return conversations;
    return conversations.filter(
      (item) =>
        item.title.toLowerCase().includes(keyword) ||
        (item.last_message_preview ?? "").toLowerCase().includes(keyword),
    );
  }, [conversations, filter]);

  return (
    <aside className="sidebar">
      <div className="sidebar-head">
        <span className="brand">
          <span className="brand-mark">
            <SparkIcon size={13} />
          </span>
          <span className="brand-text">thqbot</span>
        </span>
        <button className="icon-button sidebar-close" title="收起" onClick={onClose}>
          <CloseIcon size={14} />
        </button>
      </div>

      <div className="sidebar-actions">
        <label className="search">
          <SearchIcon size={13} />
          <input
            value={filter}
            placeholder="搜索会话"
            onChange={(event) => setFilter(event.target.value)}
          />
        </label>
        <button className="icon-button" title="新建会话" onClick={onNew}>
          <PlusIcon size={15} />
        </button>
      </div>

      <nav className="nav-list">
        {visible.map((item) => (
          <button
            key={item.conversation_id}
            className={`nav-item${item.conversation_id === activeId ? " is-active" : ""}`}
            onClick={() => onSelect(item.conversation_id)}
          >
            <span className="nav-item-top">
              <span className="nav-title">{item.title || "新会话"}</span>
              <span className="nav-time">
                {formatListTime(item.last_message_at ?? item.updated_at)}
              </span>
            </span>
            <span className="nav-sub">
              <span className="nav-preview">{item.last_message_preview || "暂无消息"}</span>
              {item.unread_count > 0 && <span className="nav-badge">{item.unread_count}</span>}
            </span>
          </button>
        ))}
        {visible.length === 0 && (
          <div className="nav-empty">{filter ? "没有匹配的会话" : "还没有会话"}</div>
        )}
      </nav>

      <div className="sidebar-foot">
        <span className="who">
          <span className="who-avatar">
            {(user?.display_name || user?.username || "?").slice(0, 1).toUpperCase()}
          </span>
          <span className="who-text">
            <span className="who-name">{user?.display_name || user?.username || "未登录"}</span>
            <span className={`conn ${connected ? "is-on" : "is-off"}`}>
              <span className="conn-dot" />
              {botId}
            </span>
          </span>
        </span>
        <button className="icon-button" title="设置" onClick={onSettings}>
          <SettingsIcon size={15} />
        </button>
        <button className="icon-button" title="退出登录" onClick={onLogout}>
          <LogoutIcon size={15} />
        </button>
      </div>
    </aside>
  );
}
