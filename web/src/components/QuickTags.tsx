import { TOOL_PRESETS } from "../presets";

interface Props {
  onPick: (prompt: string) => void;
  disabled?: boolean;
}

/** 顶部功能胶囊：点击把对应提示词填进输入框（调试快捷入口，DSH Pill 规格）。 */
export default function QuickTags({ onPick, disabled }: Props) {
  return (
    <div className="capsules">
      {TOOL_PRESETS.map((preset) => (
        <button
          className="capsule"
          key={preset.id}
          title={preset.hint}
          disabled={disabled}
          onClick={() => onPick(preset.prompt)}
        >
          {preset.label}
        </button>
      ))}
    </div>
  );
}
