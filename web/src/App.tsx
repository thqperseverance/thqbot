import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ApiError, api, openEventStream } from "./api";
import Composer from "./components/Composer";
import { CloseIcon } from "./components/Icons";
import LoginScreen from "./components/LoginScreen";
import MessageBubble from "./components/MessageBubble";
import Sidebar from "./components/Sidebar";
import TopBar from "./components/TopBar";
import TrajectoryPanel from "./components/TrajectoryPanel";
import Welcome from "./components/Welcome";
import { dayKey, formatDayLabel, stageLabel } from "./format";
import type {
  Conversation,
  FileAttachment,
  LiveStatus,
  Message,
  RuntimeConfig,
  StreamEvent,
  TrajectoryEntry,
  User,
} from "./types";

const FEEDBACK_KEY = "thqbot:feedback";
const TRAJECTORY_LIMIT = 300;

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [booting, setBooting] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const current = await api.me();
        if (!cancelled) setUser(current);
      } catch {
        /* 按未登录处理 */
      } finally {
        if (!cancelled) setBooting(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const handleLogout = useCallback(() => {
    void api.logout().catch(() => undefined);
    setUser(null);
  }, []);

  if (booting) {
    return (
      <div className="booting">
        <span className="boot-dot" />
        加载中…
      </div>
    );
  }
  if (!user) return <LoginScreen onSuccess={setUser} />;
  return <ChatScreen user={user} onLogout={handleLogout} />;
}

/* ------------------------------------------------------------------ 轨迹工具 */

/** 把同一会话的历史事件与实时事件合并去重（按 id），并按时间排序。 */
function mergeTrajectory(
  existing: TrajectoryEntry[],
  incoming: TrajectoryEntry[],
): TrajectoryEntry[] {
  if (incoming.length === 0) return existing;
  const map = new Map<string, TrajectoryEntry>();
  for (const entry of existing) map.set(entry.id, entry);
  for (const entry of incoming) if (!map.has(entry.id)) map.set(entry.id, entry);
  return [...map.values()].sort((a, b) => a.at - b.at).slice(-TRAJECTORY_LIMIT);
}

/** 从历史消息里补一份轨迹（技能 / 工具 / 回复），让「轨迹」Tab 不是空的。 */
function seedTrajectory(conversationId: string, messages: Message[]): TrajectoryEntry[] {
  const seeded: TrajectoryEntry[] = [];
  for (const message of messages) {
    if (message.sender !== "bot") continue;
    const at = message.created_at ? new Date(message.created_at).getTime() : Date.now();
    const turn = message.message_id || `t-${at}`;
    for (const skill of message.meta?.progress?.skills ?? []) {
      seeded.push({
        id: `${turn}-skill-${skill}`,
        at,
        conversationId,
        kind: "skill",
        label: skill,
        detail: "技能调用",
      });
    }
    for (const tool of message.meta?.progress?.tools ?? []) {
      seeded.push({
        id: `${turn}-tool-${tool}`,
        at,
        conversationId,
        kind: "tool",
        label: tool,
        detail: "工具调用",
      });
    }
    if (message.content) {
      seeded.push({
        id: `${turn}-reply`,
        at,
        conversationId,
        kind: message.status === "failed" ? "error" : "run",
        label: message.status === "failed" ? "回合失败" : "回复已生成",
        detail: message.content.slice(0, 80),
      });
    }
  }
  return seeded;
}

function readFeedbackCount(): number {
  if (typeof window === "undefined") return 0;
  try {
    const raw = window.localStorage.getItem(FEEDBACK_KEY);
    return raw ? Object.keys(JSON.parse(raw) as Record<string, string>).length : 0;
  } catch {
    return 0;
  }
}

/* ------------------------------------------------------------------ 运行中提示 */

function LiveRunner({ status }: { status?: LiveStatus }) {
  const percent =
    typeof status?.percent === "number" ? Math.max(0, Math.min(100, status.percent)) : null;
  const skills = status?.skills ?? [];
  const tools = status?.tools ?? [];

  return (
    <div className="runner">
      <div className="runner-head">
        <span className="runner-stage">{stageLabel(status?.stage)}</span>
        {percent !== null && <span>{percent}%</span>}
        {status?.message ? <span>· {status.message}</span> : null}
      </div>

      {percent !== null && (
        <div className="runner-bar">
          <span style={{ width: `${percent}%` }} />
        </div>
      )}

      {(skills.length > 0 || tools.length > 0) && (
        <div className="runner-chips">
          {skills.map((skill) => (
            <span className="chip chip-skill" key={`ls-${skill}`}>
              {skill}
            </span>
          ))}
          {tools.slice(0, 6).map((tool) => (
            <span className="chip chip-tool" key={`lt-${tool}`}>
              {tool}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ 设置面板 */

function SettingsModal({
  botId,
  tenantId,
  runtime,
  connected,
  model,
  conversationCount,
  onClose,
}: {
  botId: string;
  tenantId: string;
  runtime: RuntimeConfig | null;
  connected: boolean;
  model: string;
  conversationCount: number;
  onClose: () => void;
}) {
  const rows: Array<[string, string]> = [
    ["机器人", botId],
    ["租户", tenantId],
    ["模型", model || "—"],
    ["权限范围", runtime?.workspace_restricted ? "工作区受限" : "完全访问"],
    ["单附件上限", runtime ? `${runtime.max_upload_mb} MB` : "—"],
    ["会话数", String(conversationCount)],
    ["实时通道", connected ? "已连接（SSE）" : "重连中"],
    ["本地反馈", `${readFeedbackCount()} 条（仅存浏览器）`],
  ];

  return (
    <div className="modal" onClick={onClose}>
      <div className="modal-card" onClick={(event) => event.stopPropagation()}>
        <div className="modal-head">
          <h2>设置</h2>
          <button className="icon-button" title="关闭" onClick={onClose}>
            <CloseIcon size={15} />
          </button>
        </div>
        <div className="modal-body">
          {rows.map(([key, value]) => (
            <div className="modal-row" key={key}>
              <span className="modal-key">{key}</span>
              <span className="modal-value">{value}</span>
            </div>
          ))}
        </div>
        <div className="modal-foot">
          运行配置来自服务端环境变量（APP_*）；修改后需重启网关。
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ 主界面 */

function ChatScreen({ user, onLogout }: { user: User; onLogout: () => void }) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [botId, setBotId] = useState("bot_A");
  const [tenantId, setTenantId] = useState("");
  const [runtime, setRuntime] = useState<RuntimeConfig | null>(null);
  const [model, setModel] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [draft, setDraft] = useState("");
  const [staged, setStaged] = useState<FileAttachment[]>([]);
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [connected, setConnected] = useState(false);
  const [live, setLive] = useState<Record<string, LiveStatus>>({});
  const [awaiting, setAwaiting] = useState<Record<string, boolean>>({});
  const [trajectory, setTrajectory] = useState<Record<string, TrajectoryEntry[]>>({});
  const [atBottom, setAtBottom] = useState(true);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [tab, setTab] = useState<"chat" | "trajectory">("chat");
  const [focusSignal, setFocusSignal] = useState(0);

  const activeIdRef = useRef<string | null>(null);
  const turnIdRef = useRef<Record<string, string>>({});
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  activeIdRef.current = activeId;

  const activeConversation = useMemo(
    () => conversations.find((item) => item.conversation_id === activeId) ?? null,
    [conversations, activeId],
  );

  const activeTrajectory = activeId ? (trajectory[activeId] ?? []) : [];
  const activeLive = activeId ? live[activeId] : undefined;
  const isAwaiting = activeId ? Boolean(awaiting[activeId]) : false;

  const metrics = useMemo(() => {
    const skills = new Set<string>();
    const tools = new Set<string>();
    for (const message of messages) {
      for (const skill of message.meta?.progress?.skills ?? []) skills.add(skill);
      for (const tool of message.meta?.progress?.tools ?? []) tools.add(tool);
    }
    for (const skill of activeLive?.skills ?? []) skills.add(skill);
    for (const tool of activeLive?.tools ?? []) tools.add(tool);
    return { messageCount: messages.length, skillCount: skills.size, toolCount: tools.size };
  }, [messages, activeLive]);

  const failed = useMemo(() => {
    const last = messages[messages.length - 1];
    return Boolean(last && last.sender === "bot" && last.status === "failed");
  }, [messages]);

  const pushTrajectory = useCallback((conversationId: string, entries: TrajectoryEntry[]) => {
    if (entries.length === 0) return;
    setTrajectory((prev) => ({
      ...prev,
      [conversationId]: mergeTrajectory(prev[conversationId] ?? [], entries),
    }));
  }, []);

  const refreshConversations = useCallback(async () => {
    try {
      setConversations(await api.conversations());
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        onLogout();
        return;
      }
      setNotice("会话列表加载失败");
    }
  }, [onLogout]);

  const openConversation = useCallback(
    async (conversationId: string) => {
      setActiveId(conversationId);
      setMessages([]);
      setHasMore(false);
      setStaged([]);
      setSidebarOpen(false);
      try {
        const page = await api.messages(conversationId);
        setMessages(page.messages);
        setHasMore(page.paging.has_more);
        pushTrajectory(conversationId, seedTrajectory(conversationId, page.messages));
        await api.markRead(conversationId);
        await refreshConversations();
      } catch {
        setNotice("消息加载失败");
      }
    },
    [pushTrajectory, refreshConversations],
  );

  const loadOlder = useCallback(async () => {
    if (!activeId || messages.length === 0) return;
    const anchor = scrollRef.current;
    const previousHeight = anchor?.scrollHeight ?? 0;
    try {
      const page = await api.messages(activeId, messages[0].message_id);
      setMessages((prev) => [...page.messages, ...prev]);
      setHasMore(page.paging.has_more);
      // 保持视口位置，避免加载旧消息时跳动
      requestAnimationFrame(() => {
        const node = scrollRef.current;
        if (node) node.scrollTop = node.scrollHeight - previousHeight;
      });
    } catch {
      setNotice("加载更早的消息失败");
    }
  }, [activeId, messages]);

  /* 初始化：会话上下文 + 运行配置 + 会话列表 */
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const context = await api.sessionContext();
        if (cancelled) return;
        setBotId(context.bot_id);
        setTenantId(context.tenant_id);

        try {
          const config = await api.runtimeConfig();
          if (cancelled) return;
          setRuntime(config);
          setModel(config.model);
          setBotId(config.bot_id || context.bot_id);
        } catch {
          // 旧版网关没有 /api/config：退化成单模型
          if (!cancelled) setRuntime(null);
        }

        const list = await api.conversations();
        if (cancelled) return;
        setConversations(list);
        if (list.length > 0) await openConversation(list[0].conversation_id);
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) onLogout();
        else setNotice("初始化失败，请刷新页面");
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /* SSE 实时通道：消息 + 阶段进度 → 同时写入轨迹 */
  useEffect(() => {
    const source = openEventStream();
    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false);
    source.onmessage = (event) => {
      let payload: StreamEvent;
      try {
        payload = JSON.parse(event.data) as StreamEvent;
      } catch {
        return;
      }

      if (payload.type === "ready") {
        setConnected(true);
        return;
      }

      if (payload.type === "status" && payload.conversation_id) {
        const conversationId = payload.conversation_id;
        const status = payload.status ?? {};
        const at = Date.now();
        const turn = turnIdRef.current[conversationId] ?? `s-${at}`;
        const entries: TrajectoryEntry[] = [];

        const stage = status.stage ?? null;
        if (stage) {
          entries.push({
            id: `${turn}-stage-${stage}`,
            at,
            conversationId,
            kind: "run",
            label: stageLabel(stage),
            detail: status.message || undefined,
          });
        }
        for (const skill of status.skills ?? []) {
          entries.push({
            id: `${turn}-skill-${skill}`,
            at,
            conversationId,
            kind: "skill",
            label: skill,
            detail: "技能调用",
          });
        }
        for (const tool of status.tools ?? []) {
          entries.push({
            id: `${turn}-tool-${tool}`,
            at,
            conversationId,
            kind: "tool",
            label: tool,
            detail: "工具调用",
          });
        }
        pushTrajectory(conversationId, entries);

        setLive((prev) => ({
          ...prev,
          [conversationId]: {
            stage,
            percent: status.percent ?? null,
            message: status.message ?? "",
            skills: status.skills ?? [],
            tools: status.tools ?? [],
            updatedAt: at,
          },
        }));
        return;
      }

      if (payload.type === "message" && payload.message) {
        const incoming = payload.message;
        const target = incoming.conversation_id;
        if (target === activeIdRef.current) {
          setMessages((prev) => {
            if (prev.some((item) => item.message_id === incoming.message_id)) return prev;
            const withoutOptimistic = prev.filter(
              (item) => !(item.message_id.startsWith("local-") && item.sender === incoming.sender),
            );
            return [...withoutOptimistic, incoming];
          });
        }
        if (incoming.sender === "bot") {
          const turn = turnIdRef.current[target] ?? `r-${Date.now()}`;
          pushTrajectory(target, [
            {
              id: `${turn}-reply`,
              at: Date.now(),
              conversationId: target,
              kind: incoming.status === "failed" ? "error" : "run",
              label: incoming.status === "failed" ? "回合失败" : "回复已生成",
              detail: (incoming.content || "").slice(0, 80),
            },
          ]);
          setAwaiting((prev) => ({ ...prev, [target]: false }));
          setLive((prev) => {
            const next = { ...prev };
            delete next[target];
            return next;
          });
        }
        void refreshConversations();
      }
    };

    return () => {
      source.close();
      setConnected(false);
    };
  }, [pushTrajectory, refreshConversations]);

  /* 仅在贴近底部时自动滚动 */
  useEffect(() => {
    if (!atBottom || tab !== "chat") return;
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages.length, activeId, atBottom, tab]);

  const onScroll = useCallback(() => {
    const node = scrollRef.current;
    if (!node) return;
    const distance = node.scrollHeight - node.scrollTop - node.clientHeight;
    setAtBottom(distance < 120);
  }, []);

  const pickSuggestion = useCallback((text: string) => {
    setDraft(text);
    setTab("chat");
    setAtBottom(true);
    setFocusSignal((value) => value + 1);
  }, []);

  async function handleFiles(fileList: FileList | null) {
    if (!fileList || !activeId) return;
    setNotice(null);
    setUploading(true);
    try {
      for (const file of Array.from(fileList)) {
        try {
          const uploaded = await api.uploadFile(activeId, file);
          setStaged((prev) => [...prev, uploaded]);
        } catch (err) {
          const reason = err instanceof ApiError ? err.message : "上传失败";
          setNotice(`「${file.name}」${reason}`);
        }
      }
    } finally {
      setUploading(false);
    }
  }

  /** 真正发一轮：乐观消息 + 轨迹起点 + Kafka 投递。 */
  const submit = useCallback(
    async (content: string, fileIds: string[], attachments: FileAttachment[] = []) => {
      const conversationId = activeIdRef.current;
      if (!conversationId || busy) return;
      if (!content && fileIds.length === 0) return;

      const turn = Date.now().toString(36);
      turnIdRef.current[conversationId] = turn;
      setBusy(true);
      setNotice(null);
      setAtBottom(true);
      setTab("chat");

      const optimistic: Message = {
        message_id: `local-${Date.now()}`,
        conversation_id: conversationId,
        sender: "user",
        content,
        content_type: fileIds.length > 0 && !content ? "file" : "text",
        status: "sending",
        meta: attachments.length > 0 ? { attachments } : {},
      };
      setMessages((prev) => [...prev, optimistic]);
      setAwaiting((prev) => ({ ...prev, [conversationId]: true }));
      pushTrajectory(conversationId, [
        {
          id: `${turn}-queued`,
          at: Date.now(),
          conversationId,
          kind: "run",
          label: "消息已入队",
          detail: content.slice(0, 80) || `附件 ${fileIds.length} 个`,
        },
      ]);

      try {
        const result = await api.send(conversationId, content, fileIds);
        if (result.warning) setNotice(result.warning);
        if (result.message && result.message.message_id) {
          const sent = result.message;
          setMessages((prev) =>
            prev.map((item) => (item.message_id === optimistic.message_id ? sent : item)),
          );
        }
        // 降级投递（Kafka 不可用）或消息被判重时不会有 agent 回复，
        // 立刻收掉"运行中"，否则界面会一直转。
        if (result.degraded) {
          setAwaiting((prev) => ({ ...prev, [conversationId]: false }));
          pushTrajectory(conversationId, [
            {
              id: `${turn}-degraded`,
              at: Date.now(),
              conversationId,
              kind: "error",
              label: "投递降级：未送达 agent",
              detail: result.warning || "Kafka 不可用，消息只落库",
            },
          ]);
        }
        await refreshConversations();
      } catch (err) {
        setMessages((prev) => prev.filter((item) => item.message_id !== optimistic.message_id));
        setAwaiting((prev) => ({ ...prev, [conversationId]: false }));
        setNotice(err instanceof ApiError ? err.message : "发送失败");
        pushTrajectory(conversationId, [
          {
            id: `${turn}-failed`,
            at: Date.now(),
            conversationId,
            kind: "error",
            label: "发送失败",
            detail: err instanceof ApiError ? err.message : "未知错误",
          },
        ]);
      } finally {
        setBusy(false);
      }
    },
    [busy, pushTrajectory, refreshConversations],
  );

  async function send() {
    const content = draft.trim();
    const fileIds = staged.map((item) => item.file_id);
    if ((!content && fileIds.length === 0) || !activeId || busy || uploading) return;
    const attachments = staged;
    setDraft("");
    setStaged([]);
    await submit(content, fileIds, attachments);
  }

  /** 重试：找到该回合前面最近的一条用户消息，原样再发一次。 */
  const retryTurn = useCallback(
    (message: Message) => {
      const index = messages.findIndex((item) => item.message_id === message.message_id);
      if (index < 0) return;
      for (let cursor = index - 1; cursor >= 0; cursor -= 1) {
        const candidate = messages[cursor];
        if (candidate.sender !== "user") continue;
        const fileIds = (candidate.meta?.attachments ?? []).map((item) => item.file_id);
        void submit(candidate.content, fileIds, candidate.meta?.attachments ?? []);
        return;
      }
    },
    [messages, submit],
  );

  async function createConversation() {
    try {
      const created = await api.createConversation();
      await refreshConversations();
      await openConversation(created.conversation_id);
    } catch {
      setNotice("新建会话失败");
    }
  }

  return (
    <div className={`app${sidebarOpen ? " is-sidebar-open" : ""}`}>
      <Sidebar
        conversations={conversations}
        activeId={activeId}
        user={user}
        botId={botId}
        connected={connected}
        onSelect={(conversationId) => void openConversation(conversationId)}
        onNew={() => void createConversation()}
        onSettings={() => setSettingsOpen(true)}
        onLogout={onLogout}
        onClose={() => setSidebarOpen(false)}
      />
      {sidebarOpen && <div className="sidebar-scrim" onClick={() => setSidebarOpen(false)} />}

      <main className="main">
        <TopBar
          conversation={activeConversation}
          botId={botId}
          running={isAwaiting}
          failed={failed}
          messageCount={metrics.messageCount}
          skillCount={metrics.skillCount}
          toolCount={metrics.toolCount}
          trajectoryCount={activeTrajectory.length}
          tab={tab}
          onTabChange={setTab}
          onOpenSidebar={() => setSidebarOpen(true)}
          onPickPreset={pickSuggestion}
        />

        {tab === "chat" ? (
          <div className="scroll" ref={scrollRef} onScroll={onScroll}>
            <div className="column">
              {hasMore && (
                <button className="load-more" onClick={() => void loadOlder()}>
                  加载更早的消息
                </button>
              )}

              {messages.length === 0 && <Welcome onPick={pickSuggestion} />}

              {messages.map((message, index) => {
                const currentDay = dayKey(message.created_at);
                const previousDay = index > 0 ? dayKey(messages[index - 1].created_at) : "";
                const showDay = Boolean(currentDay) && currentDay !== previousDay;
                return (
                  <Fragment key={message.message_id}>
                    {showDay && (
                      <div className="day-sep">
                        <span>{formatDayLabel(message.created_at)}</span>
                      </div>
                    )}
                    <MessageBubble message={message} onRetry={retryTurn} />
                  </Fragment>
                );
              })}

              {isAwaiting && <LiveRunner status={activeLive} />}

              <div ref={bottomRef} />
            </div>
          </div>
        ) : (
          <div className="scroll">
            <div className="column">
              <TrajectoryPanel entries={activeTrajectory} />
            </div>
          </div>
        )}

        {notice && (
          <div className="notice-bar" onClick={() => setNotice(null)} title="点击关闭">
            {notice}
          </div>
        )}

        <Composer
          draft={draft}
          onDraftChange={setDraft}
          onSend={() => void send()}
          staged={staged}
          onRemoveStaged={(fileId) =>
            setStaged((prev) => prev.filter((item) => item.file_id !== fileId))
          }
          onPickFiles={(files) => void handleFiles(files)}
          uploading={uploading}
          busy={busy}
          disabled={!activeId}
          focusSignal={focusSignal}
          runtime={runtime}
          model={model}
          onModelChange={setModel}
        />
      </main>

      {settingsOpen && (
        <SettingsModal
          botId={botId}
          tenantId={tenantId}
          runtime={runtime}
          connected={connected}
          model={model}
          conversationCount={conversations.length}
          onClose={() => setSettingsOpen(false)}
        />
      )}
    </div>
  );
}
