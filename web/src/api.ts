import type {
  Conversation,
  FileAttachment,
  MessagePage,
  RuntimeConfig,
  SendResponse,
  SessionContext,
  User,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  // FormData 必须让浏览器自己设置带 boundary 的 Content-Type
  const isFormData = typeof FormData !== "undefined" && init?.body instanceof FormData;
  const response = await fetch(path, {
    credentials: "include",
    ...init,
    headers: {
      ...(init?.body && !isFormData ? { "Content-Type": "application/json" } : {}),
      ...(init?.headers ?? {}),
    },
  });

  if (!response.ok) {
    let detail = `请求失败（HTTP ${response.status}）`;
    try {
      const payload = (await response.json()) as { detail?: string };
      if (payload?.detail) detail = payload.detail;
    } catch {
      /* 忽略非 JSON 响应 */
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  async me(): Promise<User | null> {
    try {
      const payload = await request<{ user: User }>("/api/auth/me");
      return payload.user;
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) return null;
      throw error;
    }
  },

  async login(username: string, password: string): Promise<User> {
    const payload = await request<{ user: User }>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    return payload.user;
  },

  async logout(): Promise<void> {
    await request<{ status: string }>("/api/auth/logout", { method: "POST" });
  },

  sessionContext(): Promise<SessionContext> {
    return request<SessionContext>("/api/session/context");
  },

  /** 运行配置：模型清单、租户、权限范围、附件上限（缺失时前端降级为单模型）。 */
  runtimeConfig(): Promise<RuntimeConfig> {
    return request<RuntimeConfig>("/api/config");
  },

  conversations(): Promise<Conversation[]> {
    return request<Conversation[]>("/api/conversations");
  },

  createConversation(title?: string): Promise<Conversation> {
    return request<Conversation>("/api/conversations", {
      method: "POST",
      body: JSON.stringify({ title: title ?? null }),
    });
  },

  messages(conversationId: string, beforeId?: string | null, limit = 50): Promise<MessagePage> {
    const params = new URLSearchParams({ limit: String(limit) });
    if (beforeId) params.set("before_id", beforeId);
    return request<MessagePage>(
      `/api/conversations/${encodeURIComponent(conversationId)}/messages?${params.toString()}`,
    );
  },

  send(conversationId: string, content: string, fileIds: string[] = []): Promise<SendResponse> {
    return request<SendResponse>(
      `/api/conversations/${encodeURIComponent(conversationId)}/messages`,
      { method: "POST", body: JSON.stringify({ content, file_ids: fileIds }) },
    );
  },

  uploadFile(conversationId: string, file: File): Promise<FileAttachment> {
    const form = new FormData();
    form.append("file", file, file.name);
    return request<FileAttachment>(
      `/api/conversations/${encodeURIComponent(conversationId)}/files`,
      { method: "POST", body: form },
    );
  },

  deleteFile(fileId: string): Promise<{ status: string; file_id: string }> {
    return request<{ status: string; file_id: string }>(
      `/api/files/${encodeURIComponent(fileId)}`,
      { method: "DELETE" },
    );
  },

  markRead(conversationId: string): Promise<Conversation> {
    return request<Conversation>(
      `/api/conversations/${encodeURIComponent(conversationId)}/read`,
      { method: "POST" },
    );
  },

  deleteMessages(conversationId: string, messageIds: string[]): Promise<{ deleted: number }> {
    return request<{ deleted: number }>(
      `/api/conversations/${encodeURIComponent(conversationId)}/messages`,
      { method: "DELETE", body: JSON.stringify({ message_ids: messageIds }) },
    );
  },
};

export function openEventStream(): EventSource {
  // 同源部署，Cookie 自动携带
  return new EventSource("/api/stream", { withCredentials: true });
}
