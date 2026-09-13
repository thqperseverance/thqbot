import { formatTime } from "../format";
import type { TrajectoryEntry } from "../types";

/** 轨迹：把 SSE 进度事件累积成时间线（阶段推进 / 技能 / 工具 / 回复）。 */
export default function TrajectoryPanel({ entries }: { entries: TrajectoryEntry[] }) {
  if (entries.length === 0) {
    return (
      <div className="tl-empty">
        还没有轨迹。
        <br />
        发送一条消息后，这里会实时记录阶段推进、技能与工具调用。
      </div>
    );
  }

  return (
    <>
      <div className="tl-head">
        <span>共 {entries.length} 条事件</span>
        <span className="meta-sep">·</span>
        <span>随实时推送追加</span>
      </div>
      <div className="timeline">
        {entries.map((entry) => (
          <div className={`tl-item is-${entry.kind}`} key={entry.id}>
            <span className="tl-time">{formatTime(new Date(entry.at).toISOString())}</span>
            <span className="tl-rail">
              <span className="tl-dot" />
            </span>
            <span className="tl-body">
              <span className="tl-label">{entry.label}</span>
              {entry.detail ? <span className="tl-detail">{entry.detail}</span> : null}
            </span>
          </div>
        ))}
      </div>
    </>
  );
}
