import ast
import ipaddress
import json
import re
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from loguru import logger
import yaml

from ithqbot.agent.tools.base import Tool


class CheckSkillTool(Tool):
    def __init__(self, workspace: Any = None, config: Any = None, **kwargs: Any):
        super().__init__()
        _ = kwargs
        self._workspace = Path(workspace).resolve() if workspace else Path.cwd()
        self._config = config

    @property
    def name(self) -> str:
        return "check_skill"

    @property
    def description(self) -> str:
        return "检查 skill 是否符合 SKILL_STANDARDS.md，返回 PASS/WARN/FAIL 报告。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "description": "要检查的 skill 名称（ithqbot/skills 下目录名）",
                },
                "strict": {
                    "type": "boolean",
                    "default": False,
                    "description": "严格模式：推荐项也按失败处理",
                },
                "bundle_path": {
                    "type": "string",
                    "description": "待检测的 skill 打包文件路径，支持 .zip/.tar/.tar.gz/.tgz/.tar.bz2/.tbz2/.tar.xz/.txz",
                },
            },
            "required": [],
        }

    async def execute(self, *args: Any, **kwargs: Any) -> str:
        payload: dict[str, Any] = {}
        if args and isinstance(args[0], dict):
            payload.update(args[0])
        payload.update(kwargs or {})

        skill_name = payload.get("skill_name")
        if not skill_name and args and isinstance(args[0], str):
            skill_name = args[0]
        strict = self._coerce_bool(payload.get("strict", False))
        bundle_path = payload.get("bundle_path") or payload.get("archive_path")
        context = payload.get("context")
        await self._emit_progress(
            context,
            8,
            "initializing",
            "正在准备技能规范检查",
            status_details={
                "execution": {
                    "tool_name": "check_skill",
                    "has_bundle_path": bool(bundle_path),
                    "target_skill": str(skill_name or ""),
                }
            },
            progress_stage="skill_call",
            call_type="skill",
            tool_name="check_skill",
            skill_name="check_skill",
        )
        if isinstance(bundle_path, str) and bundle_path.strip():
            result = self._check_bundle(bundle_path.strip(), skill_name, strict)
            await self._emit_progress(
                context,
                98,
                "finalizing",
                "技能规范检查完成",
                status_details={"execution": {"tool_name": "check_skill", "target_skill": str(skill_name or "")}},
                progress_stage="skill_call",
                call_type="skill",
                tool_name="check_skill",
                skill_name="check_skill",
            )
            return result
        if not isinstance(skill_name, str) or not skill_name.strip():
            return (
                "FAIL:\n- 缺少必要参数 skill_name 或 bundle_path。\n\nFix Plan:\n"
                "1) 传入 skill_name（检查本地已安装 skill）\n"
                "2) 或传入 bundle_path（检查待上线打包 skill）\n"
                "3) 可选传 strict=true"
            )
        skill_name = skill_name.strip()
        result = self._check_skill(skill_name, strict)
        await self._emit_progress(
            context,
            98,
            "finalizing",
            "技能规范检查完成",
            status_details={"execution": {"tool_name": "check_skill", "target_skill": skill_name}},
            progress_stage="skill_call",
            call_type="skill",
            tool_name="check_skill",
            skill_name="check_skill",
        )
        return self._render_report(skill_name, strict, result)

    async def _emit_progress(
        self,
        context: Any,
        percent: int,
        stage: str,
        message: str,
        **kwargs: Any,
    ) -> None:
        if not context:
            return
        emitter = getattr(context, "emit_progress", None)
        if not callable(emitter):
            return
        try:
            await emitter(percent, stage, message, **kwargs)
        except TypeError:
            try:
                await emitter(percent, stage, message)
            except Exception as exc:
                logger.warning("check_skill progress callback failed in fallback mode: {}", exc)
        except Exception as exc:
            logger.warning("check_skill progress callback failed: {}", exc)

    def _coerce_bool(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "y", "on"}:
                return True
            if normalized in {"0", "false", "no", "n", "off"}:
                return False
        return bool(value)

    def _check_bundle(self, bundle_path: str, skill_name: Any, strict: bool) -> str:
        archive = self._resolve_bundle_path(bundle_path)
        if archive is None:
            return (
                "FAIL:\n- bundle_path 不存在或不可访问。\n\nFix Plan:\n"
                "1) 传入正确的绝对路径\n"
                "2) 或将压缩包放在工作目录后传相对路径"
            )
        if not self._is_supported_bundle(archive):
            return (
                "FAIL:\n- 不支持的压缩格式。\n\nFix Plan:\n"
                "1) 使用 .zip/.tar/.tar.gz/.tgz/.tar.bz2/.tbz2/.tar.xz/.txz"
            )

        with tempfile.TemporaryDirectory(prefix="check-skill-") as tmp:
            temp_root = Path(tmp)
            try:
                self._extract_bundle_securely(archive, temp_root)
            except Exception as exc:
                return (
                    f"FAIL:\n- 解压失败：{exc}\n\nFix Plan:\n"
                    "1) 确认压缩包完整可读\n"
                    "2) 移除软链接或非法路径后重新打包"
                )

            preferred_name = str(skill_name).strip() if isinstance(skill_name, str) and skill_name.strip() else None
            located = self._locate_skill_dir_from_bundle(temp_root, preferred_name=preferred_name)
            if located is None:
                return (
                    "FAIL:\n- 在压缩包中未找到有效 skill 目录（缺少 SKILL.md）。\n\nFix Plan:\n"
                    "1) 确保包内包含 <skill_name>/SKILL.md\n"
                    "2) 可选传 skill_name 指定目标目录"
                )
            skill_dir, detected_name = located
            effective_name = preferred_name or detected_name
            result = self._check_skill(effective_name, strict, skill_dir=skill_dir, source=f"bundle:{archive.name}")
            return self._render_report(effective_name, strict, result)

    def _check_skill(
        self,
        skill_name: str,
        strict: bool,
        skill_dir: Path | None = None,
        source: str = "local",
    ) -> dict[str, list[str]]:
        passed: list[str] = []
        warned: list[str] = []
        failed: list[str] = []
        fixes: list[str] = []

        standards_file = self._resolve_standards_file()
        if standards_file:
            passed.append(f"已定位标准文档：{standards_file}")
        else:
            warned.append("未找到 SKILL_STANDARDS.md，使用内置规则执行检查。")

        target_dir = skill_dir or self._resolve_skill_dir(skill_name)
        if not target_dir:
            failed.append(f"未找到技能目录：{skill_name}")
            fixes.append("确认目录位于 ithqbot/skills/<skill_name>")
            return {"pass": passed, "warn": warned, "fail": failed, "fix": fixes}

        passed.append(f"已定位技能目录：{target_dir}（来源：{source}）")

        skill_md = target_dir / "SKILL.md"
        if not skill_md.exists():
            failed.append("缺少 SKILL.md")
            fixes.append("补齐 SKILL.md，并提供 frontmatter 与正文结构")
            return {"pass": passed, "warn": warned, "fail": failed, "fix": fixes}
        passed.append("SKILL.md 存在")

        skill_md_text = skill_md.read_text(encoding="utf-8")
        self._check_skill_markdown(
            skill_name=skill_name,
            content=skill_md_text,
            strict=strict,
            passed=passed,
            warned=warned,
            failed=failed,
            fixes=fixes,
        )

        tool_py = target_dir / "tool" / "tool.py"
        runtime_py = target_dir / "runtime" / "implementation.py"
        executable = tool_py.exists() or runtime_py.exists()
        if executable:
            passed.append("识别为 Executable Skill")
        else:
            passed.append("识别为 Prompt Skill（无执行代码）")

        if tool_py.exists():
            runtime_name = self._check_tool_python(
                tool_py=tool_py,
                strict=strict,
                passed=passed,
                warned=warned,
                failed=failed,
                fixes=fixes,
            )
            self._check_contract_file(
                skill_dir=target_dir,
                runtime_tool_name=runtime_name,
                strict=strict,
                passed=passed,
                warned=warned,
                failed=failed,
                fixes=fixes,
            )
            self._check_code_risks(
                skill_dir=target_dir,
                strict=strict,
                passed=passed,
                warned=warned,
                failed=failed,
                fixes=fixes,
            )
            self._check_external_link_risks(
                skill_dir=target_dir,
                strict=strict,
                passed=passed,
                warned=warned,
                failed=failed,
                fixes=fixes,
            )
        elif executable:
            warned.append("未发现 tool/tool.py，仅检测到 runtime/implementation.py。")

        return {"pass": passed, "warn": warned, "fail": failed, "fix": fixes}

    def _resolve_bundle_path(self, bundle_path: str) -> Path | None:
        candidate = Path(bundle_path).expanduser()
        if not candidate.is_absolute():
            candidate = (self._workspace / candidate).resolve()
        if candidate.exists() and candidate.is_file():
            return candidate
        return None

    def _is_supported_bundle(self, archive: Path) -> bool:
        name = archive.name.lower()
        suffixes = tuple(archive.suffixes)
        if name.endswith(".zip"):
            return True
        if name.endswith(".tar"):
            return True
        if name.endswith(".tar.gz") or name.endswith(".tgz"):
            return True
        if name.endswith(".tar.bz2") or name.endswith(".tbz2"):
            return True
        if name.endswith(".tar.xz") or name.endswith(".txz"):
            return True
        return suffixes in {(".tar",), (".zip",)}

    def _extract_bundle_securely(self, archive: Path, dest: Path) -> None:
        lower_name = archive.name.lower()
        if lower_name.endswith(".zip"):
            self._safe_extract_zip(archive, dest)
            return
        self._safe_extract_tar(archive, dest)

    def _safe_extract_zip(self, archive: Path, dest: Path) -> None:
        with zipfile.ZipFile(archive) as zf:
            for member in zf.infolist():
                target = self._safe_target_path(dest, member.filename)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member, "r") as src, target.open("wb") as dst:
                    dst.write(src.read())

    def _safe_extract_tar(self, archive: Path, dest: Path) -> None:
        with tarfile.open(archive, mode="r:*") as tf:
            for member in tf.getmembers():
                if member.issym() or member.islnk():
                    raise ValueError("不允许包含软链接或硬链接")
                target = self._safe_target_path(dest, member.name)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                extracted = tf.extractfile(member)
                if extracted is None:
                    continue
                with extracted, target.open("wb") as dst:
                    dst.write(extracted.read())

    def _safe_target_path(self, root: Path, member_name: str) -> Path:
        cleaned = member_name.replace("\\", "/").lstrip("/")
        target = (root / cleaned).resolve()
        root_resolved = root.resolve()
        if target == root_resolved or root_resolved in target.parents:
            return target
        raise ValueError(f"检测到非法路径穿越：{member_name}")

    def _locate_skill_dir_from_bundle(self, root: Path, preferred_name: str | None = None) -> tuple[Path, str] | None:
        if preferred_name:
            preferred = [d for d in root.rglob(preferred_name) if d.is_dir() and (d / "SKILL.md").exists()]
            if preferred:
                return preferred[0], preferred_name

        candidates: list[Path] = []
        for skill_md in root.rglob("SKILL.md"):
            skill_dir = skill_md.parent
            if skill_dir.is_dir():
                candidates.append(skill_dir)
        if not candidates:
            return None
        candidates.sort(key=lambda p: len(str(p)))
        selected = candidates[0]
        return selected, selected.name

    def _resolve_standards_file(self) -> Path | None:
        candidates: list[Path] = []
        for root in self._search_roots():
            candidates.extend(
                [
                    root / "docs" / "SKILL_STANDARDS.md",
                    root / "ithqbot" / "docs" / "SKILL_STANDARDS.md",
                    root / "services" / "ithqbot" / "ithqbot" / "docs" / "SKILL_STANDARDS.md",
                ]
            )
        for file_path in candidates:
            if file_path.exists():
                return file_path
        return None

    def _resolve_skill_dir(self, skill_name: str) -> Path | None:
        candidates: list[Path] = []
        for root in self._search_roots():
            candidates.extend(
                [
                    root / "skills" / skill_name,
                    root / "ithqbot" / "skills" / skill_name,
                    root / "services" / "ithqbot" / "ithqbot" / "skills" / skill_name,
                ]
            )
        for skill_dir in candidates:
            if skill_dir.exists() and skill_dir.is_dir():
                return skill_dir
        return None

    def _search_roots(self) -> list[Path]:
        seeds = [self._workspace.resolve(), Path.cwd().resolve()]
        try:
            seeds.append(Path(__file__).resolve())
        except OSError as exc:
            logger.debug("resolve check_skill __file__ failed: {}", exc)
        roots: list[Path] = []
        seen: set[str] = set()
        for seed in seeds:
            chain = [seed, *seed.parents]
            for root in chain[:8]:
                key = str(root)
                if key in seen:
                    continue
                seen.add(key)
                roots.append(root)
        return roots

    def _check_skill_markdown(
        self,
        skill_name: str,
        content: str,
        strict: bool,
        passed: list[str],
        warned: list[str],
        failed: list[str],
        fixes: list[str],
    ) -> None:
        frontmatter = self._extract_frontmatter(content)
        if frontmatter is None:
            failed.append("SKILL.md 缺少 YAML frontmatter")
            fixes.append("按 --- frontmatter --- 结构补齐 name/description")
            return
        passed.append("SKILL.md frontmatter 存在")
        frontmatter_data = self._parse_frontmatter_yaml(frontmatter)

        fm_name = self._extract_frontmatter_field(frontmatter, "name")
        if fm_name == skill_name:
            passed.append("frontmatter.name 与目录名一致")
        else:
            failed.append(f"frontmatter.name 不一致：当前为 {fm_name or '<empty>'}")
            fixes.append(f"将 frontmatter.name 改为 {skill_name}")

        if fm_name and re.fullmatch(r"[a-z0-9_-]+", fm_name):
            passed.append("frontmatter.name 命名格式合法")
        else:
            failed.append("frontmatter.name 命名不合法（仅允许小写字母/数字/_/-）")
            fixes.append("修正 frontmatter.name 命名格式")

        description = self._extract_frontmatter_field(frontmatter, "description")
        if description:
            passed.append("frontmatter.description 存在")
            trigger_hint = bool(re.search(r"当|时|用于|invoke|when|use", description, re.IGNORECASE))
            if trigger_hint:
                passed.append("frontmatter.description 包含触发语义")
            elif strict:
                failed.append("frontmatter.description 缺少“何时触发”语义")
                fixes.append("将 description 写为“做什么 + 何时触发”")
            else:
                warned.append("frontmatter.description 建议补充“何时触发”语义")
        else:
            failed.append("frontmatter.description 缺失")
            fixes.append("补齐 frontmatter.description，包含“做什么 + 何时触发”")

        ithqbot_meta = {}
        metadata_block = frontmatter_data.get("metadata")
        if isinstance(metadata_block, dict):
            ithqbot_meta = metadata_block.get("ithqbot", metadata_block.get("openclaw", {}))
            if not isinstance(ithqbot_meta, dict):
                ithqbot_meta = {}
        if ithqbot_meta:
            self._check_skill_metadata_contract(ithqbot_meta, passed, warned, failed, fixes)
        else:
            warned.append("建议在 frontmatter.metadata.ithqbot 中声明 capability/tags/level/planner")

        section_titles = ["## 目标", "## 何时使用", "## 输入约束", "## 执行步骤", "## 输出约定", "## 风险与边界"]
        missing = [title for title in section_titles if title not in content]
        if not missing:
            passed.append("SKILL.md 推荐正文结构完整")
        elif strict:
            failed.append(f"SKILL.md 缺少推荐章节：{', '.join(missing)}")
            fixes.append("补齐推荐章节：目标/何时使用/输入约束/执行步骤/输出约定/风险与边界")
        else:
            warned.append(f"SKILL.md 建议补齐章节：{', '.join(missing)}")

    def _extract_frontmatter(self, content: str) -> str | None:
        match = re.match(r"^---\n(.*?)\n---\n?", content, flags=re.DOTALL)
        if not match:
            return None
        return match.group(1)

    def _parse_frontmatter_yaml(self, frontmatter: str) -> dict[str, Any]:
        try:
            parsed = yaml.safe_load(frontmatter)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _extract_frontmatter_field(self, frontmatter: str, field: str) -> str:
        pattern = rf"^{re.escape(field)}\s*:\s*(.+?)\s*$"
        match = re.search(pattern, frontmatter, flags=re.MULTILINE)
        if not match:
            return ""
        value = match.group(1).strip()
        if len(value) >= 2 and ((value[0] == value[-1]) and value[0] in {"'", '"'}):
            return value[1:-1].strip()
        return value

    def _check_skill_metadata_contract(
        self,
        meta: Any,
        passed: list[str],
        warned: list[str],
        failed: list[str],
        fixes: list[str],
    ) -> None:
        if not isinstance(meta, dict):
            warned.append("metadata.ithqbot 应为 object；当前值已按缺省处理")
            meta = {}
        capability = meta.get("capability")
        if capability is None:
            warned.append("建议声明 metadata.ithqbot.capability，便于 Router/Planner 做能力选择")
        elif self._is_string_list(capability):
            passed.append("metadata.ithqbot.capability 格式合法")
        else:
            failed.append("metadata.ithqbot.capability 必须为字符串数组")
            fixes.append("将 capability 改为字符串数组，例如 [\"document\", \"analysis\"]")

        tags = meta.get("tags")
        if tags is None:
            warned.append("建议声明 metadata.ithqbot.tags，便于检索与相似 Skill 选择")
        elif self._is_string_list(tags):
            passed.append("metadata.ithqbot.tags 格式合法")
        else:
            failed.append("metadata.ithqbot.tags 必须为字符串数组")
            fixes.append("将 tags 改为字符串数组，例如 [\"summary\", \"nlp\"]")

        level = meta.get("level")
        if level is None:
            warned.append("建议声明 metadata.ithqbot.level（atomic/composite）")
        elif isinstance(level, str) and level in {"atomic", "composite"}:
            passed.append("metadata.ithqbot.level 合法")
        else:
            failed.append("metadata.ithqbot.level 仅允许 atomic 或 composite")
            fixes.append("将 level 改为 atomic 或 composite")

        planner = meta.get("planner")
        if planner is None:
            warned.append("建议声明 metadata.ithqbot.planner 约束，提升自动编排成功率")
        elif isinstance(planner, dict):
            passed.append("metadata.ithqbot.planner 存在")
            for key in ("input_from", "output_to", "incompatible_with", "preferred_after"):
                value = planner.get(key)
                if value is None:
                    continue
                if self._is_string_list(value):
                    passed.append(f"metadata.ithqbot.planner.{key} 格式合法")
                else:
                    failed.append(f"metadata.ithqbot.planner.{key} 必须为字符串数组")
                    fixes.append(f"将 planner.{key} 改为字符串数组")
        else:
            failed.append("metadata.ithqbot.planner 必须为 object")
            fixes.append("将 metadata.ithqbot.planner 改为对象，包含 input_from/output_to 等字段")

        for key in ("idempotent", "retryable"):
            value = meta.get(key)
            if value is None:
                warned.append(f"建议声明 metadata.ithqbot.{key}，明确重试与副作用语义")
            elif isinstance(value, bool):
                passed.append(f"metadata.ithqbot.{key} 合法")
            else:
                failed.append(f"metadata.ithqbot.{key} 必须为布尔值")
                fixes.append(f"将 metadata.ithqbot.{key} 改为 true 或 false")

        cost = meta.get("cost")
        if cost is None:
            warned.append("建议声明 metadata.ithqbot.cost.level，便于成本路由")
        elif isinstance(cost, dict) and cost.get("level") in {"low", "medium", "high"}:
            passed.append("metadata.ithqbot.cost.level 合法")
        else:
            failed.append("metadata.ithqbot.cost.level 仅允许 low/medium/high")
            fixes.append("将 metadata.ithqbot.cost.level 改为 low、medium 或 high")

        latency = meta.get("latency")
        if latency is None:
            warned.append("建议声明 metadata.ithqbot.latency.expected_ms，便于 SLA 评估")
        elif isinstance(latency, dict) and isinstance(latency.get("expected_ms"), int) and latency["expected_ms"] >= 0:
            passed.append("metadata.ithqbot.latency.expected_ms 合法")
        else:
            failed.append("metadata.ithqbot.latency.expected_ms 必须为非负整数")
            fixes.append("将 metadata.ithqbot.latency.expected_ms 改为非负整数")

    def _is_string_list(self, value: Any) -> bool:
        return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)

    def _check_tool_python(
        self,
        tool_py: Path,
        strict: bool,
        passed: list[str],
        warned: list[str],
        failed: list[str],
        fixes: list[str],
    ) -> str:
        source = tool_py.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            failed.append(f"tool.py 语法错误：{exc}")
            fixes.append("修复 tool.py 语法错误后重试")
            return ""

        tool_class: ast.ClassDef | None = None
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            base_names = {self._base_name(base) for base in node.bases}
            if "Tool" in base_names:
                tool_class = node
                break

        if tool_class is None:
            failed.append("tool.py 未发现继承 Tool 的类")
            fixes.append("在 tool.py 中实现 class Xxx(Tool)")
            return ""
        passed.append(f"tool.py 发现 Tool 子类：{tool_class.name}")

        property_names = {
            child.name
            for child in tool_class.body
            if isinstance(child, ast.FunctionDef) and any(self._is_property_decorator(d) for d in child.decorator_list)
        }
        for required_property in ("name", "description", "parameters"):
            if required_property in property_names:
                passed.append(f"tool.py 包含属性：{required_property}")
            else:
                failed.append(f"tool.py 缺少属性：{required_property}")
                fixes.append(f"在 Tool 子类中补齐 @{required_property} 属性")

        execute_fn = next(
            (
                child
                for child in tool_class.body
                if isinstance(child, ast.AsyncFunctionDef) and child.name == "execute"
            ),
            None,
        )
        if execute_fn is None:
            failed.append("tool.py 缺少 async execute")
            fixes.append("实现 async def execute(self, **kwargs)")
            return ""

        passed.append("tool.py 包含 async execute")
        if execute_fn.args.kwarg is None:
            failed.append("execute 未声明 **kwargs，不符合标准签名")
            fixes.append("将 execute 签名改为包含 **kwargs")

        if execute_fn.args.vararg is not None:
            if strict:
                failed.append("严格模式下 execute 不应使用 *args")
                fixes.append("移除 execute 的 *args，仅保留 **kwargs")
            else:
                warned.append("execute 使用了 *args；建议仅保留 **kwargs")

        runtime_name = self._extract_runtime_tool_name(tool_class)
        if runtime_name:
            passed.append(f"运行时工具名：{runtime_name}")
        else:
            warned.append("未能静态提取运行时工具名，跳过与契约 name 的一致性比对。")

        has_progress_report = "emit_progress(" in source or "record_trace_event(" in source
        if has_progress_report:
            passed.append("tool.py 包含进度或可观测上报逻辑")
        else:
            warned.append("tool.py 未检测到 emit_progress/record_trace_event，建议补充。")

        if has_progress_report:
            trace_tokens = (
                "progress_stage=",
                "status_details=",
                "call_type=",
                "tool_name=",
                "skill_name=",
                '"stage":',
                "'stage':",
                '"status_text":',
                "'status_text':",
            )
            if any(token in source for token in trace_tokens):
                passed.append("tool.py 包含内部状态可追踪字段")
            elif strict:
                failed.append("tool.py 缺少内部状态追踪字段")
                fixes.append("为进度上报补充 progress_stage/status_details/tool_name/call_type 等字段")
            else:
                warned.append("tool.py 建议补充内部状态追踪字段（progress_stage/status_details/tool_name/call_type）")

        if re.search(r"api[_-]?key\s*=", source, flags=re.IGNORECASE):
            failed.append("疑似硬编码 API Key")
            fixes.append("移除硬编码密钥，改用 context.get_llm 或统一配置")

        return runtime_name

    def _check_contract_file(
        self,
        skill_dir: Path,
        runtime_tool_name: str,
        strict: bool,
        passed: list[str],
        warned: list[str],
        failed: list[str],
        fixes: list[str],
    ) -> None:
        candidates = [skill_dir / "schema.json", skill_dir / "tool" / "tool_def.json"]
        contract_file = next((p for p in candidates if p.exists()), None)
        if contract_file is None:
            if strict:
                failed.append("严格模式下必须提供 schema.json 或 tool/tool_def.json")
                fixes.append("新增 schema.json 或 tool/tool_def.json 并声明 input_schema")
            else:
                warned.append("建议提供 schema.json 或 tool/tool_def.json")
            return

        passed.append(f"已找到契约文件：{contract_file.name}")
        try:
            raw = json.loads(contract_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            failed.append(f"{contract_file.name} 不是合法 JSON")
            fixes.append(f"修复 {contract_file.name} 的 JSON 格式")
            return

        if not isinstance(raw, dict):
            failed.append(f"{contract_file.name} 顶层必须是 object")
            fixes.append(f"将 {contract_file.name} 顶层改为 JSON object")
            return

        contract_name = raw.get("name")
        if isinstance(contract_name, str) and contract_name:
            passed.append("契约 name 存在")
            if runtime_tool_name and contract_name != runtime_tool_name:
                failed.append(f"契约 name 与运行时工具名不一致：{contract_name} != {runtime_tool_name}")
                fixes.append("统一契约 name 与 Tool.name")
        else:
            failed.append("契约缺少 name")
            fixes.append("在契约文件中补齐 name 字段")

        schema = raw.get("input_schema")
        legacy_schema = raw.get("parameters")
        if isinstance(schema, dict):
            if schema.get("type") == "object":
                passed.append("契约 input_schema.type=object")
            else:
                failed.append("契约 input_schema.type 必须为 object")
                fixes.append("修正 input_schema.type 为 object")
        elif isinstance(legacy_schema, dict):
            if strict:
                failed.append("严格模式下不接受 legacy parameters，需迁移到 input_schema")
                fixes.append("将 parameters 迁移为 input_schema")
            else:
                warned.append("检测到 legacy parameters，建议迁移到 input_schema")
        else:
            failed.append("契约缺少 input_schema/parameters")
            fixes.append("补齐 input_schema（JSON Schema object）")

        output_schema = raw.get("output_schema")
        if output_schema is None:
            warned.append("建议声明 output_schema，便于 Graph Planner 推导下游节点")
        elif isinstance(output_schema, dict):
            if output_schema.get("type") == "object":
                passed.append("契约 output_schema.type=object")
            else:
                failed.append("契约 output_schema.type 必须为 object")
                fixes.append("修正 output_schema.type 为 object")
        else:
            failed.append("契约 output_schema 必须为 object")
            fixes.append("将 output_schema 改为 JSON Schema object")

        semantic = raw.get("semantic")
        if semantic is None:
            warned.append("建议声明 semantic.produces/consumes，提升 Skill 可组合性")
        elif isinstance(semantic, dict):
            passed.append("契约 semantic 存在")
            for key in ("produces", "consumes"):
                value = semantic.get(key)
                if value is None:
                    warned.append(f"建议声明 semantic.{key}")
                    continue
                if self._is_string_list(value):
                    passed.append(f"契约 semantic.{key} 格式合法")
                else:
                    failed.append(f"契约 semantic.{key} 必须为字符串数组")
                    fixes.append(f"将 semantic.{key} 改为字符串数组")
        else:
            failed.append("契约 semantic 必须为 object")
            fixes.append("将 semantic 改为对象，并声明 produces/consumes")

    def _check_code_risks(
        self,
        skill_dir: Path,
        strict: bool,
        passed: list[str],
        warned: list[str],
        failed: list[str],
        fixes: list[str],
    ) -> None:
        py_files = [
            p for p in skill_dir.rglob("*.py")
            if p.is_file() and not self._is_test_artifact(p, skill_dir)
        ]
        if not py_files:
            warned.append("未发现 Python 代码文件，跳过代码缺陷与安全扫描。")
            return

        risk_rules: list[tuple[re.Pattern[str], str, str, str]] = [
            (re.compile(r"\beval\s*\(", re.IGNORECASE), "高", "检测到 eval 调用", "避免 eval，改用显式解析逻辑"),
            (re.compile(r"\bexec\s*\(", re.IGNORECASE), "高", "检测到 exec 调用", "避免 exec，改用受控分派"),
            (
                re.compile(r"subprocess\.(?:Popen|run|call)\(.*shell\s*=\s*True", re.IGNORECASE | re.DOTALL),
                "高",
                "检测到 subprocess shell=True",
                "改用参数数组并关闭 shell=True",
            ),
            (re.compile(r"\bos\.system\s*\(", re.IGNORECASE), "高", "检测到 os.system 调用", "改用受控 subprocess 参数调用"),
            (
                re.compile(r"yaml\.load\s*\(", re.IGNORECASE),
                "中",
                "检测到 yaml.load 调用",
                "优先使用 yaml.safe_load",
            ),
            (
                re.compile(r"requests\.[a-z_]+\([^)]*verify\s*=\s*False", re.IGNORECASE | re.DOTALL),
                "高",
                "检测到 requests verify=False",
                "启用 TLS 证书校验，避免 verify=False",
            ),
            (
                re.compile(r"ssl\._create_unverified_context\s*\(", re.IGNORECASE),
                "高",
                "检测到 ssl._create_unverified_context",
                "移除不安全 SSL 上下文，启用证书校验",
            ),
            (
                re.compile(r"urllib3\.disable_warnings\s*\(", re.IGNORECASE),
                "中",
                "检测到 urllib3.disable_warnings",
                "避免全局关闭 TLS 告警，改为修复证书配置",
            ),
            (
                re.compile(
                    r"jwt\.decode\s*\([^)]*(?:verify_signature\s*=\s*False|['\"]verify_signature['\"]\s*:\s*False)",
                    re.IGNORECASE | re.DOTALL,
                ),
                "高",
                "检测到 JWT 关闭签名校验",
                "启用 JWT 签名校验并校验 issuer/audience",
            ),
            (
                re.compile(r"tempfile\.mktemp\s*\(", re.IGNORECASE),
                "中",
                "检测到 tempfile.mktemp 调用",
                "改用 NamedTemporaryFile 或 mkstemp",
            ),
            (
                re.compile(r"pickle\.(?:load|loads)\s*\(", re.IGNORECASE),
                "高",
                "检测到不安全 pickle 反序列化",
                "避免反序列化不可信输入",
            ),
            (
                re.compile(r"marshal\.(?:load|loads)\s*\(", re.IGNORECASE),
                "高",
                "检测到 marshal 反序列化",
                "避免加载不可信二进制数据",
            ),
            (
                re.compile(r"(?m)^\s*except\s+Exception\s*:\s*pass\s*$", re.IGNORECASE),
                "中",
                "检测到吞异常写法 except Exception: pass",
                "记录异常并返回可读错误",
            ),
            (
                re.compile(r"(?m)^\s*except\s*:\s*$"),
                "中",
                "检测到裸 except",
                "改为捕获具体异常类型",
            ),
            (re.compile(r"hashlib\.(md5|sha1)\s*\(", re.IGNORECASE), "中", "检测到弱哈希算法", "改用 sha256 及以上"),
            (
                re.compile(r"hashlib\.new\s*\(\s*['\"](?:md5|sha1)['\"]", re.IGNORECASE),
                "中",
                "检测到弱哈希算法",
                "改用 sha256 及以上",
            ),
            (
                re.compile(r"requests\.[a-z_]+\((?:(?!timeout\s*=).)*\)", re.IGNORECASE | re.DOTALL),
                "中",
                "检测到 requests 调用可能缺少 timeout",
                "为外部请求显式设置 timeout，避免阻塞与资源耗尽",
            ),
            (
                re.compile(r"(api[_-]?key|secret|token)\s*=\s*['\"][^'\"]+['\"]", re.IGNORECASE),
                "高",
                "检测到疑似硬编码密钥",
                "改用安全注入或 context/config 提供",
            ),
        ]

        issues: list[tuple[str, str, str, str, int]] = []
        for py_file in py_files:
            try:
                content = py_file.read_text(encoding="utf-8")
            except Exception:
                continue
            for pattern, level, title, suggestion in risk_rules:
                for match in pattern.finditer(content):
                    line_no = content.count("\n", 0, match.start()) + 1
                    issues.append((level, title, suggestion, str(py_file), line_no))

        if not issues:
            passed.append("代码缺陷与安全风险扫描未发现高危问题")
            return

        high_issues = [item for item in issues if item[0] == "高"]
        medium_issues = [item for item in issues if item[0] == "中"]

        for _, title, suggestion, file_path, line_no in high_issues[:10]:
            failed.append(f"高危风险：{title}（{file_path}:{line_no}）")
            fixes.append(suggestion)

        if medium_issues:
            msg_target = failed if strict else warned
            prefix = "中风险（严格模式按失败）" if strict else "中风险"
            for _, title, suggestion, file_path, line_no in medium_issues[:10]:
                msg_target.append(f"{prefix}：{title}（{file_path}:{line_no}）")
                fixes.append(suggestion)

    def _check_external_link_risks(
        self,
        skill_dir: Path,
        strict: bool,
        passed: list[str],
        warned: list[str],
        failed: list[str],
        fixes: list[str],
    ) -> None:
        text_files = [
            p for p in skill_dir.rglob("*")
            if p.is_file()
            and p.suffix.lower() in {".py", ".md", ".json", ".yml", ".yaml", ".toml", ".ini", ".txt"}
            and not self._is_test_artifact(p, skill_dir)
        ]
        if not text_files:
            warned.append("未发现可扫描的文本文件，跳过外部链接安全检查。")
            return

        url_pattern = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
        local_hosts = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
        trusted_demo_hosts = {
            "example.com",
            "summarize.sh",
            "youtu.be",
            "youtube.com",
            "www.youtube.com",
            "aibase.com",
            "www.aibase.com",
        }
        issues: list[tuple[str, str, str, str, int]] = []
        seen: set[tuple[str, str, int, str]] = set()

        for file in text_files:
            try:
                content = file.read_text(encoding="utf-8")
            except Exception:
                continue
            for match in url_pattern.finditer(content):
                url = match.group(0).rstrip(".,;)")
                parsed = urlparse(url)
                host = (parsed.hostname or "").lower()
                if not host:
                    continue
                line_no = content.count("\n", 0, match.start()) + 1
                if parsed.scheme.lower() == "http" and host not in local_hosts:
                    key = ("高", str(file), line_no, "http")
                    if key not in seen:
                        seen.add(key)
                        issues.append(("高", "检测到明文 HTTP 外链", "优先改用 HTTPS 或内网受控地址", str(file), line_no))
                try:
                    _ = ipaddress.ip_address(host)
                    is_ip_host = True
                except ValueError:
                    is_ip_host = False
                if is_ip_host and host not in local_hosts:
                    key = ("高", str(file), line_no, "ip")
                    if key not in seen:
                        seen.add(key)
                        issues.append(("高", "检测到 IP 直连外链", "改用域名并配置可信证书校验与白名单", str(file), line_no))
                if host not in local_hosts and host not in trusted_demo_hosts:
                    key = ("中", str(file), line_no, host)
                    if key not in seen:
                        seen.add(key)
                        issues.append(("中", "检测到硬编码第三方外链（需评审）", "将外链改为配置注入并建立域名白名单评审流程", str(file), line_no))

        if not issues:
            passed.append("外部链接安全检查未发现风险")
            return

        high_issues = [item for item in issues if item[0] == "高"]
        medium_issues = [item for item in issues if item[0] == "中"]

        for _, title, suggestion, file_path, line_no in high_issues[:10]:
            failed.append(f"高危风险：{title}（{file_path}:{line_no}）")
            fixes.append(suggestion)

        if medium_issues:
            msg_target = failed if strict else warned
            prefix = "中风险（严格模式按失败）" if strict else "中风险"
            for _, title, suggestion, file_path, line_no in medium_issues[:10]:
                msg_target.append(f"{prefix}：{title}（{file_path}:{line_no}）")
                fixes.append(suggestion)

    def _is_test_artifact(self, file_path: Path, skill_dir: Path) -> bool:
        try:
            relative_parts = file_path.relative_to(skill_dir).parts
        except ValueError:
            relative_parts = file_path.parts
        return any(part.lower() in {"tests", "test"} for part in relative_parts)

    def _extract_runtime_tool_name(self, tool_class: ast.ClassDef) -> str:
        for child in tool_class.body:
            if not isinstance(child, ast.FunctionDef) or child.name != "name":
                continue
            for stmt in child.body:
                if isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                    return stmt.value.value
        return ""

    def _is_property_decorator(self, node: ast.expr) -> bool:
        return isinstance(node, ast.Name) and node.id == "property"

    def _base_name(self, node: ast.expr) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return ""

    def _render_report(self, skill_name: str, strict: bool, result: dict[str, list[str]]) -> str:
        mode = "strict" if strict else "normal"
        lines = [f"Skill: {skill_name}", f"Mode: {mode}", "", "PASS:"]
        lines.extend([f"- {item}" for item in result["pass"]] or ["- (none)"])
        lines.append("")
        lines.append("WARN:")
        lines.extend([f"- {item}" for item in result["warn"]] or ["- (none)"])
        lines.append("")
        lines.append("FAIL:")
        lines.extend([f"- {item}" for item in result["fail"]] or ["- (none)"])
        lines.append("")
        lines.append("Fix Plan:")
        dedup_fixes = list(dict.fromkeys(result["fix"]))
        if dedup_fixes:
            lines.extend([f"{idx}) {item}" for idx, item in enumerate(dedup_fixes, start=1)])
        else:
            lines.append("1) 无需修复")
        return "\n".join(lines)
