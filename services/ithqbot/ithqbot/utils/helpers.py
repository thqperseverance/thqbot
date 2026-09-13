"""Utility functions for ithqbot."""

import json
import os
import re
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo

import tiktoken
from io import BytesIO
try:
    import docx
except ImportError:
    docx = None
try:
    import pypdf
except ImportError:
    pypdf = None


def detect_image_mime(data: bytes) -> str | None:
    """Detect image MIME type from magic bytes, ignoring file extension."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def ensure_dir(path: Path) -> Path:
    """Ensure directory exists, return it."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def timestamp() -> str:
    """Current ISO timestamp."""
    return datetime.now().isoformat()


def default_timezone_name() -> str:
    """Configured default timezone name for user-facing and scheduling logic."""
    return os.getenv("ITHQBOT_TIMEZONE", "Asia/Shanghai")


def default_timezone() -> ZoneInfo:
    """Configured default timezone, falling back to Asia/Shanghai."""
    tz_name = default_timezone_name()
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def current_time_str() -> str:
    """Human-readable current time with weekday and timezone, e.g. '2026-03-15 22:30 (Saturday) (CST)'."""
    tz_name = default_timezone_name()
    try:
        now_dt = datetime.now(default_timezone())
        tz = tz_name
    except Exception:
        now_dt = datetime.now()
        tz = time.strftime("%Z") or "UTC"
    now = now_dt.strftime("%Y-%m-%d %H:%M (%A)")
    return f"{now} ({tz})"


_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*]')
_SECRET_TEXT_PATTERNS = (
    r"password",
    r"passwd",
    r"pwd",
    r"secret",
    r"token",
    r"access[_-]?key",
    r"secret[_-]?key",
    r"sasl[_-]?plain[_-]?password",
    r"api[_-]?key",
)
_SECRET_KEY_RE = re.compile(rf"(?i)^({'|'.join(_SECRET_TEXT_PATTERNS)})$")
_SECRET_ASSIGNMENT_RE = re.compile(
    rf"(?i)\b({'|'.join(_SECRET_TEXT_PATTERNS)})\b(\s*[:=]\s*)([^,\s]+)"
)

def safe_filename(name: str) -> str:
    """Replace unsafe path characters with underscores."""
    return _UNSAFE_CHARS.sub("_", name).strip()


def sanitize_connection_uri(uri: str | None) -> str:
    value = str(uri or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if not parsed.scheme:
        return value
    username = parsed.username or ""
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    auth = ""
    if username:
        auth = username
        if parsed.password:
            auth = f"{auth}:***"
        auth = f"{auth}@"
    elif parsed.password is not None:
        auth = ":***@"
    netloc = f"{auth}{host}{port}"
    if parsed.query:
        query_pairs = []
        for key, val in parse_qsl(parsed.query, keep_blank_values=True):
            query_pairs.append((key, "***" if _SECRET_KEY_RE.match(key or "") else val))
        query = urlencode(query_pairs)
    else:
        query = parsed.query
    return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, query, parsed.fragment))


def sanitize_log_message(value: Any) -> str:
    text = str(value)
    text = re.sub(
        r"([a-zA-Z][a-zA-Z0-9+\-.]*://[^\s:@/]*):([^@\s/]*)@",
        lambda m: f"{m.group(1)}:***@" if m.group(2) else m.group(0),
        text,
    )
    return _SECRET_ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)


def split_message(content: str, max_len: int = 2000) -> list[str]:
    """
    Split content into chunks within max_len, preferring line breaks.

    Args:
        content: The text content to split.
        max_len: Maximum length per chunk (default 2000 for Discord compatibility).

    Returns:
        List of message chunks, each within max_len.
    """
    if not content:
        return []
    if len(content) <= max_len:
        return [content]
    chunks: list[str] = []
    while content:
        if len(content) <= max_len:
            chunks.append(content)
            break
        cut = content[:max_len]
        # Try to break at newline first, then space, then hard break
        pos = cut.rfind('\n')
        if pos <= 0:
            pos = cut.rfind(' ')
        if pos <= 0:
            pos = max_len
        chunks.append(content[:pos])
        content = content[pos:].lstrip()
    return chunks


def build_assistant_message(
    content: str | None,
    tool_calls: list[dict[str, Any]] | None = None,
    reasoning_content: str | None = None,
    thinking_blocks: list[dict] | None = None,
) -> dict[str, Any]:
    """Build a provider-safe assistant message with optional reasoning fields."""
    msg: dict[str, Any] = {"role": "assistant"}
    
    # Only include content if it's non-empty, or if there are no tool_calls.
    # Some providers (like SiliconFlow) reject {"content": ""} when tool_calls are present.
    actual_content = content or ""
    if actual_content or not tool_calls:
        msg["content"] = actual_content

    if tool_calls:
        msg["tool_calls"] = tool_calls
    if reasoning_content is not None:
        msg["reasoning_content"] = reasoning_content
    if thinking_blocks:
        msg["thinking_blocks"] = thinking_blocks
    return msg


def estimate_prompt_tokens(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> int:
    """Estimate prompt tokens with tiktoken."""
    try:
        enc = tiktoken.get_encoding("cl100k_base")
        parts: list[str] = []
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        txt = part.get("text", "")
                        if txt:
                            parts.append(txt)
        if tools:
            parts.append(json.dumps(tools, ensure_ascii=False))
        return len(enc.encode("\n".join(parts)))
    except Exception:
        return 0


def estimate_message_tokens(message: dict[str, Any]) -> int:
    """Estimate prompt tokens contributed by one persisted message."""
    content = message.get("content")
    parts: list[str] = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text", "")
                if text:
                    parts.append(text)
            else:
                parts.append(json.dumps(part, ensure_ascii=False))
    elif content is not None:
        parts.append(json.dumps(content, ensure_ascii=False))

    for key in ("name", "tool_call_id"):
        value = message.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    if message.get("tool_calls"):
        parts.append(json.dumps(message["tool_calls"], ensure_ascii=False))

    payload = "\n".join(parts)
    if not payload:
        return 1
    try:
        enc = tiktoken.get_encoding("cl100k_base")
        return max(1, len(enc.encode(payload)))
    except Exception:
        return max(1, len(payload) // 4)


def estimate_prompt_tokens_chain(
    provider: Any,
    model: str | None,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> tuple[int, str]:
    """Estimate prompt tokens via provider counter first, then tiktoken fallback."""
    provider_counter = getattr(provider, "estimate_prompt_tokens", None)
    if callable(provider_counter):
        try:
            tokens, source = provider_counter(messages, tools, model)
            if isinstance(tokens, (int, float)) and tokens > 0:
                return int(tokens), str(source or "provider_counter")
        except Exception:
            pass

    estimated = estimate_prompt_tokens(messages, tools)
    if estimated > 0:
        return int(estimated), "tiktoken"
    return 0, "none"


def sync_workspace_templates(workspace: Path, silent: bool = False) -> list[str]:
    """Sync bundled templates to workspace. Only creates missing files."""
    from importlib.resources import files as pkg_files
    try:
        tpl = pkg_files("ithqbot") / "templates"
    except Exception:
        return []
    if not tpl.is_dir():
        return []

    added: list[str] = []

    def _write(src, dest: Path):
        if dest.exists():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(src.read_text(encoding="utf-8") if src else "", encoding="utf-8")
        added.append(str(dest.relative_to(workspace)))

    for item in tpl.iterdir():
        if item.name.endswith(".md") and not item.name.startswith("."):
            _write(item, workspace / item.name)
    _write(tpl / "memory" / "MEMORY.md", workspace / "memory" / "MEMORY.md")
    _write(None, workspace / "memory" / "HISTORY.md")
    (workspace / "skills").mkdir(exist_ok=True)

    if added and not silent:
        from rich.console import Console
        for name in added:
            Console().print(f"  [dim]Created {name}[/dim]")
    return added


def extract_text_from_file_bytes(data: bytes, filename: str) -> str:
    """
    Extract text content from file bytes based on the filename extension.
    Supports .docx, .pdf, and fallback to plain text.
    """
    if not data:
        return ""
        
    ext = Path(filename).suffix.lower()
    
    if ext == ".docx":
        try:
            if docx:
                doc = docx.Document(BytesIO(data))
                return "\n".join([para.text for para in doc.paragraphs])
            return _extract_docx_text_from_ooxml(data)
        except Exception:
            try:
                return _extract_docx_text_from_ooxml(data)
            except Exception as e:
                return f"[Error extracting .docx: {e}]"
            
    # 2. Handle PDF
    if ext == ".pdf" and pypdf:
        try:
            reader = pypdf.PdfReader(BytesIO(data))
            text = ""
            for page in reader.pages:
                text += page.extract_text() or ""
            return text
        except Exception as e:
            return f"[Error extracting .pdf: {e}]"
            
    # 3. Fallback to Plain Text
    try:
        # Use chardet or similar if available, but for now simple UTF-8 with fallback
        return data.decode("utf-8", errors="replace")
    except Exception as e:
        return f"[Error decoding as text: {e}]"


def _extract_docx_text_from_ooxml(data: bytes) -> str:
    with zipfile.ZipFile(BytesIO(data)) as archive:
        xml_data = archive.read("word/document.xml")
    root = ET.fromstring(xml_data)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs: list[str] = []
    for paragraph in root.findall(".//w:p", ns):
        parts = []
        for text_node in paragraph.findall(".//w:t", ns):
            value = text_node.text or ""
            if value:
                parts.append(value)
        if parts:
            paragraphs.append("".join(parts))
    return "\n".join(paragraphs).strip()
