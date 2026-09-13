export interface User {
  user_id: string;
  username: string;
  display_name: string;
}

export interface Conversation {
  conversation_id: string;
  title: string;
  bot_id: string;
  unread_count: number;
  last_message_preview: string;
  created_at?: string | null;
  updated_at?: string | null;
  last_message_at?: string | null;
}

export interface MessageProgress {
  event?: string;
  stage?: string | null;
  percent?: number | null;
  message?: string;
  skills?: string[];
  tools?: string[];
  call_types?: string[];
  updated_at?: string;
}

export interface FileAttachment {
  file_id: string;
  name: string;
  mime?: string;
  size?: number;
  storage_uri?: string;
  download_url?: string;
  /** 技能产出的文件（files[] 契约）可能带简介 */
  description?: string;
}

/** ithqbot 在回合结束时写入的真实 token 用量 */
export interface TurnUsage {
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  calls?: number;
}

export interface MessageMeta {
  request_msg_id?: string;
  trace_id?: string;
  event?: string;
  interaction?: Record<string, unknown>;
  files?: unknown[];
  attachments?: FileAttachment[];
  progress?: MessageProgress;
  usage?: TurnUsage;
  latency_ms?: number;
  error?: string;
}

/** 轨迹面板的一条事件（由 SSE 进度事件累积而来） */
export interface TrajectoryEntry {
  id: string;
  at: number;
  conversationId: string;
  kind: "run" | "skill" | "tool" | "error";
  label: string;
  detail?: string;
}

export interface RuntimeConfig {
  model: string;
  models: string[];
  bot_id: string;
  tenant_id: string;
  workspace_restricted: boolean;
  max_upload_mb: number;
}

export interface Message {
  message_id: string;
  conversation_id: string;
  sender: "user" | "bot";
  content: string;
  content_type: string;
  status: string;
  request_msg_id?: string | null;
  reply_to?: string | null;
  created_at?: string | null;
  meta: MessageMeta;
}

export interface MessagePage {
  messages: Message[];
  paging: {
    limit: number;
    has_more: boolean;
    oldest_message_id?: string | null;
    newest_message_id?: string | null;
  };
}

export interface SessionContext {
  user: User;
  bot_id: string;
  tenant_id: string;
  total_unread_count: number;
  server_time?: string | null;
}

export interface SendResponse {
  message: Message | null;
  queued: boolean;
  degraded: boolean;
  warning?: string | null;
}

/** 实时进度（agent 正在做什么、调用了哪些 skill） */
export interface LiveStatus {
  stage?: string | null;
  percent?: number | null;
  message: string;
  skills: string[];
  tools: string[];
  updatedAt: number;
}

export interface StreamEvent {
  type: "ready" | "message" | "status";
  conversation_id?: string;
  message?: Message;
  status?: MessageProgress;
  request_msg_id?: string | null;
  user_id?: string;
}
