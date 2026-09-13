"""Skills loader for agent capabilities."""

import fnmatch
import json
import os
import re
import shutil
import yaml
from pathlib import Path
from typing import Any

# Default builtin skills directory (relative to this file)
BUILTIN_SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"


class SkillsLoader:
    """
    Loader for agent skills.

    Skills are markdown files (SKILL.md) that teach the agent how to use
    specific tools or perform certain tasks.
    """

    def __init__(
        self,
        workspace: Path,
        builtin_skills_dir: Path | None = None,
        enabled_skills: list[str] | None = None,
    ):
        self.workspace = workspace
        self.workspace_skills = self.workspace / "skills"
        self.builtin_skills = builtin_skills_dir or BUILTIN_SKILLS_DIR
        self.enabled_skills = list(enabled_skills or [])

    def list_skills(self, filter_unavailable: bool = True) -> list[dict[str, str]]:
        """
        List all available skills.

        Args:
            filter_unavailable: If True, filter out skills with unmet requirements.

        Returns:
            List of skill info dicts with 'name', 'path', 'source'.
        """
        skills = []

        for source, skill_dir in self._iter_skill_dirs():
            skill_file = skill_dir / "SKILL.md"
            if skill_file.exists() and not any(s["name"] == skill_dir.name for s in skills):
                skills.append({"name": skill_dir.name, "path": str(skill_file), "source": source})

        skills = self._apply_enabled_filter(skills)
        # Filter by requirements
        if filter_unavailable:
            return [s for s in skills if self._check_requirements(self._get_skill_meta(s["name"]))]
        return skills

    def load_skill(self, name: str) -> str | None:
        """
        Load a skill by name.

        Args:
            name: Skill name (directory name).

        Returns:
            Skill content or None if not found.
        """
        skill_file = self._resolve_skill_file(name)
        if skill_file:
            return skill_file.read_text(encoding="utf-8")

        return None

    def load_skills_for_context(self, skill_names: list[str]) -> str:
        """
        Load specific skills for inclusion in agent context.

        Args:
            skill_names: List of skill names to load.

        Returns:
            Formatted skills content.
        """
        parts = []
        for name in skill_names:
            content = self.load_skill(name)
            if content:
                content = self._strip_frontmatter(content)
                parts.append(f"### Skill: {name}\n\n{content}")

        return "\n\n---\n\n".join(parts) if parts else ""

    def build_skills_summary(self) -> str:
        """
        Build a summary of all skills (name, description, path, availability).

        This is used for progressive loading - the agent can read the full
        skill content using read_file when needed.

        Returns:
            XML-formatted skills summary.
        """
        all_skills = self.list_skills(filter_unavailable=False)
        if not all_skills:
            return ""

        def escape_xml(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        lines = ["<skills>"]
        for s in all_skills:
            name = escape_xml(s["name"])
            path = s["path"]
            desc = escape_xml(self._get_skill_description(s["name"]))
            skill_meta = self._get_skill_meta(s["name"])
            tool_def = self._load_tool_def(s["name"])
            available = self._check_requirements(skill_meta)

            lines.append(f"  <skill available=\"{str(available).lower()}\">")
            lines.append(f"    <name>{name}</name>")
            lines.append(f"    <description>{desc}</description>")
            lines.append(f"    <location>{path}</location>")
            lines.append(f"    <source>{escape_xml(s['source'])}</source>")

            entrypoint = self._get_skill_entrypoint(s["name"])
            if entrypoint:
                lines.append(f"    <entrypoint>{escape_xml(entrypoint)}</entrypoint>")

            tool_name = tool_def.get("name")
            if isinstance(tool_name, str) and tool_name.strip():
                lines.append(f"    <tool>{escape_xml(tool_name.strip())}</tool>")

            input_hint = self._summarize_input_schema(tool_def.get("input_schema"))
            if input_hint:
                lines.append(f"    <input_hint>{escape_xml(input_hint)}</input_hint>")

            # Show missing requirements for unavailable skills
            if not available:
                missing = self._get_missing_requirements(skill_meta)
                if missing:
                    lines.append(f"    <requires>{escape_xml(missing)}</requires>")

            lines.append("  </skill>")
        lines.append("</skills>")

        return "\n".join(lines)

    def _apply_enabled_filter(self, skills: list[dict[str, str]]) -> list[dict[str, str]]:
        enabled = {name.strip() for name in self.enabled_skills if isinstance(name, str) and name.strip()}
        if not enabled or "*" in enabled:
            return skills
        patterns = list(enabled)
        return [s for s in skills if any(fnmatch.fnmatch(s["name"], p) for p in patterns)]

    def _get_missing_requirements(self, skill_meta: dict) -> str:
        """Get a description of missing requirements."""
        if not isinstance(skill_meta, dict):
            return ""
        missing = []
        requires = self._get_skill_requires(skill_meta)
        for b in requires.get("bins", []):
            if not shutil.which(b):
                missing.append(f"CLI: {b}")
        for env in requires.get("env", []):
            if not os.environ.get(env):
                missing.append(f"ENV: {env}")
        return ", ".join(missing)

    def _get_skill_description(self, name: str) -> str:
        """Get the description of a skill from its frontmatter."""
        meta = self.get_skill_metadata(name)
        if meta and meta.get("description"):
            return meta["description"]
        tool_def = self._load_tool_def(name)
        description = tool_def.get("description")
        if isinstance(description, str) and description.strip():
            return description.strip()
        return name  # Fallback to skill name

    def _strip_frontmatter(self, content: str) -> str:
        """Remove YAML frontmatter from markdown content."""
        if content.startswith("---"):
            match = re.match(r"^---\n.*?\n---\n", content, re.DOTALL)
            if match:
                return content[match.end():].strip()
        return content

    def _parse_ithqbot_metadata(self, raw: Any) -> dict:
        """Parse skill metadata from frontmatter (supports ithqbot and openclaw keys)."""
        if isinstance(raw, dict):
            # Already parsed by YAML
            if "ithqbot" in raw:
                nested = raw["ithqbot"]
                return nested if isinstance(nested, dict) else {}
            if "openclaw" in raw:
                nested = raw["openclaw"]
                return nested if isinstance(nested, dict) else {}
            # Check for legacy aliases
            for alias in ["clawdbot", "clawdis"]:
                if alias in raw:
                    nested = raw[alias]
                    return nested if isinstance(nested, dict) else {}
            return raw

        try:
            # Fallback if raw is still a string (legacy)
            data = json.loads(raw)
            if isinstance(data, dict):
                nested = data.get("ithqbot", data.get("openclaw", data))
                return nested if isinstance(nested, dict) else {}
            return {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def _check_requirements(self, skill_meta: dict) -> bool:
        """Check if skill requirements are met (bins, env vars)."""
        if not isinstance(skill_meta, dict):
            return True
        requires = self._get_skill_requires(skill_meta)
        for b in requires.get("bins", []):
            if not shutil.which(b):
                return False
        for env in requires.get("env", []):
            if not os.environ.get(env):
                return False
        return True

    def _get_skill_requires(self, skill_meta: dict) -> dict[str, Any]:
        """Normalize optional requires metadata to a mapping."""
        requires = skill_meta.get("requires")
        return requires if isinstance(requires, dict) else {}

    def _get_skill_meta(self, name: str) -> dict:
        """Get ithqbot metadata for a skill (cached in frontmatter)."""
        meta = self.get_skill_metadata(name) or {}
        return self._parse_ithqbot_metadata(meta.get("metadata", ""))

    def _iter_skill_dirs(self) -> list[tuple[str, Path]]:
        skill_dirs: list[tuple[str, Path]] = []
        if self.workspace_skills.exists():
            for skill_dir in self.workspace_skills.iterdir():
                if skill_dir.is_dir():
                    skill_dirs.append(("workspace", skill_dir))
        if self.builtin_skills and self.builtin_skills.exists():
            for skill_dir in self.builtin_skills.iterdir():
                if skill_dir.is_dir():
                    skill_dirs.append(("builtin", skill_dir))
        return skill_dirs

    def _iter_enabled_skill_dirs(self) -> list[tuple[str, Path]]:
        enabled_names = {
            item["name"]
            for item in self.list_skills(filter_unavailable=False)
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        return [
            (source, skill_dir)
            for source, skill_dir in self._iter_skill_dirs()
            if skill_dir.name in enabled_names
        ]

    def _resolve_skill_dir(self, name: str) -> Path | None:
        for _, skill_dir in self._iter_skill_dirs():
            if skill_dir.name == name:
                return skill_dir
        return None

    def _resolve_skill_file(self, name: str) -> Path | None:
        skill_dir = self._resolve_skill_dir(name)
        if not skill_dir:
            return None
        skill_file = skill_dir / "SKILL.md"
        return skill_file if skill_file.exists() else None

    def _load_json_file(self, path: Path) -> dict[str, Any]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _normalize_semantic_items(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        normalized: list[str] = []
        for item in value:
            if isinstance(item, str):
                name = item.strip()
            elif isinstance(item, dict):
                name = str(item.get("name") or "").strip()
            else:
                name = ""
            if name and name not in normalized:
                normalized.append(name)
        return normalized

    def _normalize_tool_contract(self, raw: dict[str, Any], *, fallback_name: str) -> dict[str, Any]:
        normalized = dict(raw)
        input_schema = normalized.get("input_schema")
        if not isinstance(input_schema, dict):
            parameters = normalized.get("parameters")
            if isinstance(parameters, dict):
                input_schema = dict(parameters)
        if not isinstance(input_schema, dict):
            input_schema = {"type": "object", "properties": {}}
        normalized["input_schema"] = input_schema

        semantic = normalized.get("semantic")
        if isinstance(semantic, dict):
            normalized["semantic"] = {
                "produces": self._normalize_semantic_items(semantic.get("produces")),
                "consumes": self._normalize_semantic_items(semantic.get("consumes")),
            }

        if not isinstance(normalized.get("name"), str) or not str(normalized.get("name")).strip():
            normalized["name"] = fallback_name
        if not isinstance(normalized.get("description"), str) or not str(normalized.get("description")).strip():
            normalized["description"] = fallback_name
        return normalized

    def _load_tool_def(self, name: str) -> dict[str, Any]:
        skill_dir = self._resolve_skill_dir(name)
        if not skill_dir:
            return {}

        # Preferred: formal capability declaration.
        # Compatible fallback: schema.json, tool/tool_def.json, tool_def.json.
        candidates = [
            skill_dir / "capability.json",
            skill_dir / "schema.json",
            skill_dir / "tool" / "tool_def.json",
            skill_dir / "tool_def.json"
        ]

        for tool_def_file in candidates:
            if tool_def_file.exists():
                raw = self._load_json_file(tool_def_file)
                if raw:
                    return self._normalize_tool_contract(raw, fallback_name=name)
        return {}

    def _get_skill_entrypoint(self, name: str) -> str:
        skill_dir = self._resolve_skill_dir(name)
        if not skill_dir:
            return ""
        
        # New structure: tool/tool.py, runtime/implementation.py
        # Legacy: tool.py, implementation.py
        candidates = [
            ("tool/tool.py", skill_dir / "tool" / "tool.py"),
            ("runtime/implementation.py", skill_dir / "runtime" / "implementation.py"),
            ("tool.py", skill_dir / "tool.py"),
            ("implementation.py", skill_dir / "implementation.py"),
            ("SKILL.md", skill_dir / "SKILL.md")
        ]
        
        for name_str, path in candidates:
            if path.exists():
                return name_str
        return ""

    def _summarize_input_schema(self, schema: Any) -> str:
        if not isinstance(schema, dict):
            return ""
        properties = schema.get("properties")
        if not isinstance(properties, dict) or not properties:
            return ""
        required = {
            item for item in schema.get("required", [])
            if isinstance(item, str) and item.strip()
        }
        items = []
        for key, prop in list(properties.items())[:6]:
            if not isinstance(prop, dict):
                continue
            prop_type = prop.get("type", "any")
            suffix = " required" if key in required else ""
            items.append(f"{key}:{prop_type}{suffix}")
        return ", ".join(items)

    def get_always_skills(self) -> list[str]:
        """Get skills marked as always=true that meet requirements."""
        result = []
        for s in self.list_skills(filter_unavailable=True):
            meta = self.get_skill_metadata(s["name"]) or {}
            skill_meta = self._parse_ithqbot_metadata(meta.get("metadata", ""))
            if skill_meta.get("always") or meta.get("always"):
                result.append(s["name"])
        return result

    def discover_python_tools(self, registry: Any, **kwargs) -> None:
        """
        Scan skill directories for tool.py and register any Tool subclasses found.
        
        Args:
            registry: The ToolRegistry to register tools into.
            **kwargs: Arguments to pass to the tool constructors (e.g. workspace, config).
        """
        import importlib.util
        import inspect
        import sys
        from loguru import logger
        from ithqbot.agent.tools.base import Tool

        for source, skill_dir in self._iter_enabled_skill_dirs():
            # Check both new and legacy locations
            tool_py = None
            if (skill_dir / "tool" / "tool.py").exists():
                tool_py = skill_dir / "tool" / "tool.py"
            elif (skill_dir / "tool.py").exists():
                tool_py = skill_dir / "tool.py"
            
            if not tool_py:
                continue

            try:
                # Use a unique module name to avoid collisions
                module_name = f"ithqbot.skills.dynamic.{source}.{skill_dir.name}"
                spec = importlib.util.spec_from_file_location(module_name, str(tool_py))
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[module_name] = module
                    try:
                        spec.loader.exec_module(module)
                    except Exception:
                        sys.modules.pop(module_name, None)
                        raise
                    
                    found_count = 0
                    for _, obj in inspect.getmembers(module):
                        if (inspect.isclass(obj) and 
                            issubclass(obj, Tool) and 
                            obj is not Tool and
                            obj.__module__ == module_name):
                            
                            # Inspect constructor to pass only relevant kwargs
                            sig = inspect.signature(obj.__init__)
                            valid_params = sig.parameters
                            
                            tool_kwargs = {}
                            for k, v in kwargs.items():
                                if k in valid_params:
                                    tool_kwargs[k] = v
                                    
                            tool_instance = obj(**tool_kwargs)
                            registry.register(
                                tool_instance,
                                direct_handler=getattr(tool_instance, "direct_handler", None),
                            )
                            found_count += 1
                    
                    if found_count > 0:
                        logger.info(f"Loaded {found_count} tool(s) from skill '{skill_dir.name}' ({source})")
            except Exception as e:
                logger.error(f"Failed to load dynamic tool from {tool_py}: {e}")

    def get_skill_metadata(self, name: str) -> dict | None:
        """
        Get metadata from a skill's frontmatter.

        Args:
            name: Skill name.

        Returns:
            Metadata dict or None.
        """
        content = self.load_skill(name)
        if not content:
            return None

        if content.startswith("---"):
            match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
            if match:
                try:
                    metadata = yaml.safe_load(match.group(1))
                    return metadata if isinstance(metadata, dict) else {}
                except Exception:
                    # Simple YAML parsing fallback
                    metadata = {}
                    for line in match.group(1).split("\n"):
                        if ":" in line:
                            key, value = line.split(":", 1)
                            metadata[key.strip()] = value.strip().strip('"\'')
                    return metadata

        return None
