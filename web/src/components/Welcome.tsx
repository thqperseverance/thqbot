import { BoltIcon, FileIcon, SparkIcon } from "./Icons";

interface Suggestion {
  title: string;
  hint: string;
  text: string;
  icon: "spark" | "bolt" | "file";
}

const SUGGESTIONS: Suggestion[] = [
  {
    title: "统计一段文本",
    hint: "调用自研技能 text_stats",
    text: "用 text_stats 工具统计这句话，并用 Markdown 表格给出结果：thqbot 是一个 agent 平台，支持多轮对话与技能调用。",
    icon: "spark",
  },
  {
    title: "检查技能是否合规",
    hint: "存量技能 check_skill",
    text: "请用 check_skill 工具检查 text_stats 这个技能是否符合 SKILL_STANDARDS 标准",
    icon: "bolt",
  },
  {
    title: "对比两份文档",
    hint: "先添加附件，再由 doc_compare 读取 MinIO",
    text: "请对比这两份文档的差异",
    icon: "file",
  },
  {
    title: "让 agent 自我介绍",
    hint: "验证模型链路",
    text: "你好，请用一句话介绍你自己，并说明你能调用哪些能力",
    icon: "spark",
  },
];

function SuggestionIcon({ name }: { name: Suggestion["icon"] }) {
  if (name === "bolt") return <BoltIcon size={15} />;
  if (name === "file") return <FileIcon size={15} />;
  return <SparkIcon size={15} />;
}

export default function Welcome({ onPick }: { onPick: (text: string) => void }) {
  return (
    <div className="hero">
      <div className="hero-mark">
        <SparkIcon size={22} />
      </div>
      <h2>开始一段对话</h2>
      <p>
        消息经 Kafka 交给 agent，阶段推进、技能与工具调用会实时记录在「轨迹」里；
        附件上传到 MinIO 后可直接交给技能使用。
      </p>

      <div className="hero-grid">
        {SUGGESTIONS.map((item) => (
          <button className="hero-card" key={item.title} onClick={() => onPick(item.text)}>
            <span className="hero-card-icon">
              <SuggestionIcon name={item.icon} />
            </span>
            <span>
              <span className="hero-card-title">{item.title}</span>
              <span className="hero-card-hint">{item.hint}</span>
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
