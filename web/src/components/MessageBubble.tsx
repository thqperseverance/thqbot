import { useCallback, useEffect, useState } from "react";

import { formatSize, formatTime } from "../format";
import type { FileAttachment, Message } from "../types";
import { CheckIcon, CopyIcon, FileIcon, RetryIcon, ThumbDownIcon, ThumbUpIcon } from "./Icons";
import RichText from "./RichText";

const FEEDBACK_KEY = "thqbot:feedback";

type Vote = "up" | "down";

function readVotes(): Record<string, Vote> {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(FEEDBACK_KEY);
    return raw ? (JSON.parse(raw) as Record<string, Vote>) : {};
  } catch {
    return {};
  }
}

/** 6483 -> "6.5k" */
function compactTokens(value?: number): string {
  if (!value || value <= 0) return "0";
  if (value < 1000) return String(value);
  return `${(value / 1000).toFixed(value < 10000 ? 1 : 0)}k`;
}

function formatLatency(ms?: number): string {
  if (!ms || ms <= 0) return "";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)}s`;
}

function asFileCards(files: unknown, attachments: FileAttachment[]): FileAttachment[] {
  const collected: FileAttachment[] = [];
  if (Array.isArray(files)) {
    for (const item of files) {
      if (typeof item === "string") {
        collected.push({ file_id: item, name: item.split("/").pop() || item });
        continue;
      }
      if (item && typeof item === "object") {
        const record = item as Record<string, unknown>;
        const name = String(record.name ?? record.original_file_name ?? record.rel_path ?? "file");
        collected.push({
          file_id: String(record.file_id ?? record.rel_path ?? name),
          name,
          size: typeof record.size === "number" ? record.size : undefined,
          description: typeof record.description === "string" ? record.description : undefined,
          download_url: typeof record.download_url === "string" ? record.download_url : undefined,
        });
      }
    }
  }
  // 技能产出的文件与用户附件分开展示，这里只保留技能产物
  const attachmentIds = new Set(attachments.map((item) => item.file_id));
  return collected.filter((item) => !attachmentIds.has(item.file_id));
}

interface Props {
  message: Message;
  onRetry?: (message: Message) => void;
}

export default function MessageBubble({ message, onRetry }: Props) {
  const isUser = message.sender === "user";
  const failed = message.status === "failed";
  const skills = message.meta?.progress?.skills ?? [];
  const tools = message.meta?.progress?.tools ?? [];
  const attachments = message.meta?.attachments ?? [];
  const usage = message.meta?.usage;
  const latency = message.meta?.latency_ms;
  const interaction = message.meta?.interaction as { title?: string; type?: string } | undefined;
  const fileCards = isUser ? [] : asFileCards(message.meta?.files, attachments);

  const [copied, setCopied] = useState(false);
  const [vote, setVote] = useState<Vote | null>(null);

  useEffect(() => {
    setVote(readVotes()[message.message_id] ?? null);
  }, [message.message_id]);

  const copy = useCallback(async () => {
    if (!message.content) return;
    try {
      await navigator.clipboard.writeText(message.content);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  }, [message.content]);

  const castVote = useCallback(
    (next: Vote) => {
      const target = vote === next ? null : next;
      setVote(target);
      if (typeof window === "undefined") return;
      try {
        const votes = readVotes();
        if (target) votes[message.message_id] = target;
        else delete votes[message.message_id];
        window.localStorage.setItem(FEEDBACK_KEY, JSON.stringify(votes));
      } catch {
        /* 本地存储不可用时忽略 */
      }
    },
    [message.message_id, vote],
  );

  const metaItems: string[] = [];
  if (usage && (usage.prompt_tokens || usage.completion_tokens)) {
    metaItems.push(
      `↑${compactTokens(usage.prompt_tokens)} ↓${compactTokens(usage.completion_tokens)}`,
    );
  }
  if (typeof usage?.calls === "number" && usage.calls > 1) metaItems.push(`${usage.calls} 次调用`);
  const latencyText = formatLatency(latency);
  if (latencyText) metaItems.push(latencyText);

  return (
    <div className={`msg ${isUser ? "msg-user" : "msg-bot"}${failed ? " msg-failed" : ""}`}>
      <div className="msg-card">
        {isUser ? (
          message.content
        ) : (
          <>
            {message.content ? <RichText content={message.content} /> : null}

            {fileCards.length > 0 && (
              <div className="file-cards" data-single={fileCards.length === 1 ? "true" : undefined}>
                {fileCards.map((file) => (
                  <div className="file-card" key={`${file.file_id}-${file.name}`}>
                    <span className="file-card-icon">
                      <FileIcon size={14} />
                    </span>
                    <span className="file-card-body">
                      <span className="file-card-name">{file.name}</span>
                      <span className="file-card-desc">
                        {file.description || (file.size ? formatSize(file.size) : "技能产出文件")}
                      </span>
                    </span>
                    <a
                      className="file-card-open"
                      href={file.download_url ?? `/api/files/${file.file_id}`}
                      target="_blank"
                      rel="noreferrer"
                    >
                      打开
                    </a>
                  </div>
                ))}
              </div>
            )}

            {attachments.length > 0 && (
              <div className="msg-attachments">
                {attachments.map((item) => (
                  <a
                    className="attachment"
                    key={item.file_id}
                    href={item.download_url ?? `/api/files/${item.file_id}`}
                    download={item.name}
                  >
                    <FileIcon size={13} />
                    <span className="attachment-name">{item.name}</span>
                    {item.size ? <span className="attachment-size">{formatSize(item.size)}</span> : null}
                  </a>
                ))}
              </div>
            )}

            {(skills.length > 0 || tools.length > 0 || interaction) && (
              <div className="msg-attachments">
                {skills.map((skill) => (
                  <span className="chip chip-skill" key={`s-${skill}`}>
                    {skill}
                  </span>
                ))}
                {tools.slice(0, 8).map((tool) => (
                  <span className="chip chip-tool" key={`t-${tool}`}>
                    {tool}
                  </span>
                ))}
                {interaction && (
                  <span className="chip chip-accent">
                    {interaction.type ?? "interaction"}
                    {interaction.title ? ` · ${interaction.title}` : ""}
                  </span>
                )}
              </div>
            )}
          </>
        )}
      </div>

      <div className="meta-row">
        {metaItems.map((item) => (
          <span className="meta-item" key={item}>
            {item}
          </span>
        ))}
        {metaItems.length > 0 && <span className="meta-sep">·</span>}
        <span className="meta-item">{formatTime(message.created_at)}</span>

        <span className="action-row">
          {!isUser && (
            <>
              <button
                className={`action-button${vote === "up" ? " is-on" : ""}`}
                title="有帮助"
                onClick={() => castVote("up")}
              >
                <ThumbUpIcon size={13} />
              </button>
              <button
                className={`action-button${vote === "down" ? " is-down" : ""}`}
                title="没帮助"
                onClick={() => castVote("down")}
              >
                <ThumbDownIcon size={13} />
              </button>
            </>
          )}
          <button className="action-button" title="复制" onClick={() => void copy()}>
            {copied ? <CheckIcon size={13} /> : <CopyIcon size={13} />}
          </button>
          {!isUser && onRetry && (
            <button className="action-button" title="重试这一轮" onClick={() => onRetry(message)}>
              <RetryIcon size={13} />
            </button>
          )}
        </span>
      </div>
    </div>
  );
}
