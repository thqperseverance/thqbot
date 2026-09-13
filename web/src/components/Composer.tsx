import { useEffect, useRef } from "react";

import { formatSize } from "../format";
import type { FileAttachment, RuntimeConfig } from "../types";
import { ChevronDownIcon, CloseIcon, FileIcon, PaperclipIcon, SendIcon, ShieldIcon } from "./Icons";

interface Props {
  draft: string;
  onDraftChange: (value: string) => void;
  onSend: () => void;
  staged: FileAttachment[];
  onRemoveStaged: (fileId: string) => void;
  onPickFiles: (files: FileList | null) => void;
  uploading: boolean;
  busy: boolean;
  disabled: boolean;
  focusSignal: number;
  runtime: RuntimeConfig | null;
  model: string;
  onModelChange: (model: string) => void;
}

/** 底部输入区：多行自适应 + 附件 + 权限提示 + 模型下拉 + 发送。 */
export default function Composer({
  draft,
  onDraftChange,
  onSend,
  staged,
  onRemoveStaged,
  onPickFiles,
  uploading,
  busy,
  disabled,
  focusSignal,
  runtime,
  model,
  onModelChange,
}: Props) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    const node = textareaRef.current;
    if (!node) return;
    node.style.height = "auto";
    node.style.height = `${Math.min(node.scrollHeight, 200)}px`;
  }, [draft]);

  useEffect(() => {
    if (focusSignal === 0) return;
    const node = textareaRef.current;
    if (!node) return;
    node.focus();
    const end = node.value.length;
    node.setSelectionRange(end, end);
  }, [focusSignal]);

  const canSend = !disabled && !busy && !uploading && (draft.trim().length > 0 || staged.length > 0);
  const models = runtime?.models?.length ? runtime.models : runtime?.model ? [runtime.model] : [];
  const permissionText = runtime?.workspace_restricted ? "工作区受限" : "完全访问";
  const maxUpload = runtime?.max_upload_mb ? `· 单附件 ≤ ${runtime.max_upload_mb}MB` : "";

  return (
    <footer className="composer">
      <div className="composer-inner">
        {staged.length > 0 && (
          <div className="composer-staged">
            {staged.map((item) => (
              <span className="staged" key={item.file_id}>
                <FileIcon size={12} />
                <span className="staged-name">{item.name}</span>
                {item.size ? <span className="staged-size">{formatSize(item.size)}</span> : null}
                <button
                  className="staged-remove"
                  title="移除附件"
                  onClick={() => onRemoveStaged(item.file_id)}
                >
                  <CloseIcon size={11} />
                </button>
              </span>
            ))}
          </div>
        )}

        <div className="composer-card">
          <textarea
            ref={textareaRef}
            value={draft}
            rows={1}
            placeholder={disabled ? "请先选择会话" : "给 thqbot 发消息…（Enter 发送 / Shift+Enter 换行）"}
            onChange={(event) => onDraftChange(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                if (canSend) onSend();
              }
            }}
          />

          <div className="composer-toolbar">
            <input
              ref={inputRef}
              type="file"
              multiple
              hidden
              onChange={(event) => {
                onPickFiles(event.target.files);
                event.target.value = "";
              }}
            />
            <button
              className="toolbar-button"
              disabled={disabled || uploading || busy}
              title="添加附件（上传到 MinIO）"
              onClick={() => inputRef.current?.click()}
            >
              <PaperclipIcon size={14} />
              <span>{uploading ? "上传中…" : "附件"}</span>
            </button>

            <span className="toolbar-button" title="当前权限范围（由服务端配置决定）">
              <ShieldIcon size={13} />
              <span className="toolbar-label">{permissionText}</span>
            </span>

            <span className="toolbar-spacer" />

            <label className="toolbar-select" title={models.length > 1 ? "选择模型" : "模型由服务端配置"}>
              <select
                value={model}
                disabled={models.length <= 1}
                onChange={(event) => onModelChange(event.target.value)}
                style={{
                  appearance: "none",
                  background: "none",
                  border: "none",
                  outline: "none",
                  color: "inherit",
                  font: "inherit",
                  cursor: "inherit",
                }}
              >
                {(models.length ? models : ["—"]).map((item) => (
                  <option key={item} value={item} style={{ background: "#1a1a1f", color: "#eaeaef" }}>
                    {item}
                  </option>
                ))}
              </select>
              <ChevronDownIcon size={12} />
            </label>

            <button className="send-button" disabled={!canSend} title="发送" onClick={onSend}>
              <SendIcon size={15} />
            </button>
          </div>
        </div>

        <div className="composer-foot">
          <span>Enter 发送 · Shift+Enter 换行</span>
          <span>{maxUpload}</span>
        </div>
      </div>
    </footer>
  );
}
