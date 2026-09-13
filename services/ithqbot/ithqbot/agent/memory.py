"""Memory system for persistent agent memory."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import weakref
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from typing import TYPE_CHECKING, Any, Callable

from loguru import logger

from ithqbot.utils.helpers import ensure_dir, estimate_message_tokens, estimate_prompt_tokens_chain

if TYPE_CHECKING:
    from ithqbot.providers.base import LLMProvider
    from ithqbot.session.manager import Session, SessionManager


_SAVE_MEMORY_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Save the memory consolidation result to persistent storage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "history_entry": {
                        "type": "string",
                        "description": "A paragraph summarizing key events/decisions/topics. "
                        "Start with [YYYY-MM-DD HH:MM]. Include detail useful for grep search.",
                    },
                    "memory_update": {
                        "type": "string",
                        "description": "Full updated long-term memory as markdown. Include all existing "
                        "facts plus new ones. Return unchanged if nothing new.",
                    },
                },
                "required": ["history_entry", "memory_update"],
            },
        },
    }
]


def _ensure_text(value: Any) -> str:
    """Normalize tool-call payload values to text for file storage."""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _normalize_save_memory_args(args: Any) -> dict[str, Any] | None:
    """Normalize provider tool-call arguments to the expected dict shape."""
    if isinstance(args, str):
        args = json.loads(args)
    if isinstance(args, list):
        return args[0] if args and isinstance(args[0], dict) else None
    return args if isinstance(args, dict) else None

_TOOL_CHOICE_ERROR_MARKERS = (
    "tool_choice",
    "toolchoice",
    "does not support",
    'should be ["none", "auto"]',
)

_EXPLICIT_NAME_PATTERNS = (
    re.compile(r"(?:请)?记住(?:一下)?我叫[“\"']?(?P<name>[^“”\"'，。！？\n]{1,12})[”\"']?", re.IGNORECASE),
    re.compile(r"(?:请)?记住(?:一下)?我是[“\"']?(?P<name>[^“”\"'，。！？\n]{1,12})[”\"']?", re.IGNORECASE),
    re.compile(r"我叫[“\"']?(?P<name>[^“”\"'，。！？\n]{1,12})[”\"']?", re.IGNORECASE),
    re.compile(r"你可以叫我[“\"']?(?P<name>[^“”\"'，。！？\n]{1,12})[”\"']?", re.IGNORECASE),
)


def _is_tool_choice_unsupported(content: str | None) -> bool:
    """Detect provider errors caused by forced tool_choice being unsupported."""
    text = (content or "").lower()
    return any(m in text for m in _TOOL_CHOICE_ERROR_MARKERS)


class MemoryStore:
    """Two-layer memory: MEMORY.md (long-term facts) + HISTORY.md (grep-searchable log)."""

    _MAX_FAILURES_BEFORE_RAW_ARCHIVE = 3

    def __init__(self, workspace: Path):
        self.memory_dir = ensure_dir(workspace / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "HISTORY.md"
        self._consecutive_failures = 0

    def read_long_term(self) -> str:
        if self.memory_file.exists():
            return self.memory_file.read_text(encoding="utf-8")
        return ""

    def write_long_term(self, content: str) -> None:
        self.memory_file.write_text(content, encoding="utf-8")

    def append_history(self, entry: str) -> None:
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(entry.rstrip() + "\n\n")

    def get_memory_context(self) -> str:
        long_term = self.read_long_term()
        return f"## Long-term Memory\n{long_term}" if long_term else ""

    def persist_explicit_facts(self, facts: list[str]) -> bool:
        normalized: list[str] = []
        seen: set[str] = set()
        for item in facts:
            fact = str(item or "").strip()
            if not fact or fact in seen:
                continue
            seen.add(fact)
            normalized.append(fact)
        if not normalized:
            return False

        current_memory = self.read_long_term()
        new_facts = [fact for fact in normalized if fact not in current_memory]
        if not new_facts:
            return False

        updated_memory = self._merge_explicit_facts_into_memory(current_memory, new_facts)
        self.write_long_term(updated_memory)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.append_history(f"[{timestamp}] [EXPLICIT_MEMORY] {'；'.join(new_facts)}")
        logger.info("Persisted {} explicit memory facts", len(new_facts))
        return True

    @staticmethod
    def _merge_explicit_facts_into_memory(current_memory: str, facts: list[str]) -> str:
        explicit_section = "## User Facts"
        fact_lines = "\n".join(f"- {fact}" for fact in facts)
        body = current_memory.rstrip()
        if not body:
            return f"{explicit_section}\n{fact_lines}\n"
        if explicit_section in body:
            return f"{body}\n{fact_lines}\n"
        return f"{body}\n\n{explicit_section}\n{fact_lines}\n"

    @staticmethod
    def _format_messages(messages: list[dict]) -> str:
        lines = []
        for message in messages:
            if not message.get("content"):
                continue
            tools = f" [tools: {', '.join(message['tools_used'])}]" if message.get("tools_used") else ""
            lines.append(
                f"[{message.get('timestamp', '?')[:16]}] {message['role'].upper()}{tools}: {message['content']}"
            )
        return "\n".join(lines)

    async def consolidate(
        self,
        messages: list[dict],
        provider: LLMProvider,
        model: str,
    ) -> bool:
        """Consolidate the provided message chunk into MEMORY.md + HISTORY.md."""
        if not messages:
            return True

        current_memory = self.read_long_term()
        prompt = f"""Process this conversation and call the save_memory tool with your consolidation.

## Current Long-term Memory
{current_memory or "(empty)"}

## Conversation to Process
{self._format_messages(messages)}"""

        chat_messages = [
            {
                "role": "system", 
                "content": (
                    "You are a memory consolidation expert for a personal AI assistant. "
                    "Your goal is to extract and update long-term knowledge from the provided conversation.\n\n"
                    "RULES:\n"
                    "1. **Identify Stable Personal Facts**: Always capture and PRESERVE a user's name, role, preferences, family/friends, and history. Never delete these unless they are explicitly updated or corrected.\n"
                    "2. **Relationship Tracking**: Note recurring topics, ongoing projects, and established interaction patterns.\n"
                    "3. **Prune Transient Noise**: Do not save specific one-off questions or temporary status updates unless they have long-term relevance.\n"
                    "4. **Consistency**: Ensure the 'memory_update' is a complete, well-organized markdown snapshot of everything the agent should remember about this user/session.\n"
                    "5. **Grep-Friendly History**: The 'history_entry' should be a concise but detail-rich summary of the *new* turn, prefixed with [YYYY-MM-DD HH:MM]."
                )
            },
            {"role": "user", "content": prompt},
        ]

        try:
            forced = {"type": "function", "function": {"name": "save_memory"}}
            response = await provider.chat_with_retry(
                messages=chat_messages,
                tools=_SAVE_MEMORY_TOOL,
                model=model,
                tool_choice=forced,
            )

            if response.finish_reason == "error" and _is_tool_choice_unsupported(
                response.content
            ):
                logger.warning("Forced tool_choice unsupported, retrying with auto")
                response = await provider.chat_with_retry(
                    messages=chat_messages,
                    tools=_SAVE_MEMORY_TOOL,
                    model=model,
                    tool_choice="auto",
                )

            if not response.has_tool_calls:
                logger.warning(
                    "Memory consolidation: LLM did not call save_memory "
                    "(finish_reason={}, content_len={}, content_preview={})",
                    response.finish_reason,
                    len(response.content or ""),
                    (response.content or "")[:200],
                )
                return self._fail_or_raw_archive(messages)

            args = _normalize_save_memory_args(response.tool_calls[0].arguments)
            if args is None:
                logger.warning("Memory consolidation: unexpected save_memory arguments")
                return self._fail_or_raw_archive(messages)

            if "history_entry" not in args or "memory_update" not in args:
                logger.warning("Memory consolidation: save_memory payload missing required fields")
                return self._fail_or_raw_archive(messages)

            entry = args["history_entry"]
            update = args["memory_update"]

            if entry is None or update is None:
                logger.warning("Memory consolidation: save_memory payload contains null required fields")
                return self._fail_or_raw_archive(messages)

            entry = _ensure_text(entry).strip()
            if not entry:
                logger.warning("Memory consolidation: history_entry is empty after normalization")
                return self._fail_or_raw_archive(messages)

            self.append_history(entry)
            update = _ensure_text(update)
            if update != current_memory:
                self.write_long_term(update)

            self._consecutive_failures = 0
            logger.info("Memory consolidation done for {} messages", len(messages))
            return True
        except Exception:
            logger.exception("Memory consolidation failed")
            return self._fail_or_raw_archive(messages)

    def _fail_or_raw_archive(self, messages: list[dict]) -> bool:
        """Increment failure count; after threshold, raw-archive messages and return True."""
        self._consecutive_failures += 1
        if self._consecutive_failures < self._MAX_FAILURES_BEFORE_RAW_ARCHIVE:
            return False
        self._raw_archive(messages)
        self._consecutive_failures = 0
        return True

    def _raw_archive(self, messages: list[dict]) -> None:
        """Fallback: dump raw messages to HISTORY.md without LLM summarization."""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.append_history(
            f"[{ts}] [RAW] {len(messages)} messages\n"
            f"{self._format_messages(messages)}"
        )
        logger.warning(
            "Memory consolidation degraded: raw-archived {} messages", len(messages)
        )


class RedisMemoryStore(MemoryStore):
    """Redis-backed memory store for multi-instance persistence."""

    def __init__(self, workspace: Path, uri: str):
        import redis
        self.uri = uri
        self.client = redis.Redis.from_url(uri, decode_responses=True)
        self.prefix = "ithqbot:memory:"
        self._consecutive_failures = 0
        self._max_attempts = 3
        self._base_delay_seconds = 0.2

    def _reconnect(self) -> None:
        import redis

        self.client = redis.Redis.from_url(self.uri, decode_responses=True)

    def _run(self, operation: str, fn):
        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                return fn()
            except Exception as e:
                last_error = e
                if attempt >= self._max_attempts:
                    break
                delay = min(self._base_delay_seconds * (2 ** (attempt - 1)), 2.0)
                logger.warning(
                    "Redis memory store {} failed on attempt {}/{}: {}",
                    operation,
                    attempt,
                    self._max_attempts,
                    e,
                )
                time.sleep(delay)
                self._reconnect()
        if last_error:
            raise last_error

    def _base_key(self) -> str:
        from ithqbot import context
        tenant = context.tenant_id.get() or "default"
        account = context.account_id.get() or "default"
        bot = context.bot_id.get() or "default"
        return f"{self.prefix}{tenant}:{account}:{bot}"

    def read_long_term(self) -> str:
        try:
            return self._run("read_long_term", lambda: self.client.get(f"{self._base_key()}:memory")) or ""
        except Exception as e:
            logger.error("Failed to read long-term memory from Redis: {}", e)
            return ""

    def write_long_term(self, content: str) -> None:
        self._run("write_long_term", lambda: self.client.set(f"{self._base_key()}:memory", content))

    def append_history(self, entry: str) -> None:
        key = f"{self._base_key()}:history"
        self._run("append_history", lambda: self.client.rpush(key, entry.rstrip()))
        self._run("trim_history", lambda: self.client.ltrim(key, -5000, -1))

    def get_memory_context(self) -> str:
        long_term = self.read_long_term()
        return f"## Long-term Memory\n{long_term}" if long_term else ""

    def _raw_archive(self, messages: list[dict]) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.append_history(
            f"[{ts}] [RAW] {len(messages)} messages\n"
            f"{self._format_messages(messages)}"
        )
        logger.warning(
            "Memory consolidation degraded: raw-archived {} messages (redis)", len(messages)
        )


class PostgreSQLMemoryStore(MemoryStore):
    def __init__(self, workspace: Path, uri: str):
        self.uri = uri
        self._driver_name = ""
        try:
            self._conn = self._connect(uri)
        except Exception as exc:
            safe_uri = self._sanitize_store_uri(uri)
            raise RuntimeError(
                f"Failed to connect memory store using memoryStoreUri={safe_uri}. "
                "Please check database host/port/user/password and database permissions."
            ) from exc
        self._max_attempts = 3
        self._base_delay_seconds = 0.2
        self._consecutive_failures = 0
        self._init_tables()

    @staticmethod
    def _sanitize_store_uri(uri: str) -> str:
        parsed = urlparse(uri)
        if not parsed.scheme:
            return uri
        auth = ""
        if parsed.username:
            auth = parsed.username
            if parsed.password:
                auth = f"{auth}:***"
            auth = f"{auth}@"
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        netloc = f"{auth}{host}{port}"
        return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))

    def _connect(self, uri: str):
        import psycopg

        self._driver_name = "psycopg"
        return psycopg.connect(uri, autocommit=True)

    def _reconnect(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
        self._conn = self._connect(self.uri)

    def _run(self, operation: str, fn):
        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                return fn()
            except Exception as e:
                last_error = e
                if attempt >= self._max_attempts:
                    break
                delay = min(self._base_delay_seconds * (2 ** (attempt - 1)), 2.0)
                logger.warning(
                    "PostgreSQL memory store {} failed on attempt {}/{}: {}",
                    operation,
                    attempt,
                    self._max_attempts,
                    e,
                )
                time.sleep(delay)
                self._reconnect()
        if last_error:
            raise last_error

    @contextmanager
    def _cursor(self):
        cur = self._conn.cursor()
        try:
            yield cur
        finally:
            cur.close()

    def _base_scope(self) -> tuple[str, str, str]:
        from ithqbot import context

        tenant = context.tenant_id.get() or "default"
        account = context.account_id.get() or "default"
        bot = context.bot_id.get() or "default"
        return tenant, account, bot

    def _init_tables(self) -> None:
        def _op() -> None:
            with self._cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ithqbot_memory (
                        tenant_id TEXT NOT NULL,
                        account_id TEXT NOT NULL,
                        bot_id TEXT NOT NULL,
                        memory_markdown TEXT NOT NULL DEFAULT '',
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (tenant_id, account_id, bot_id)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ithqbot_memory_history (
                        seq BIGSERIAL PRIMARY KEY,
                        tenant_id TEXT NOT NULL,
                        account_id TEXT NOT NULL,
                        bot_id TEXT NOT NULL,
                        entry TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_ithqbot_memory_history_scope_seq
                    ON ithqbot_memory_history(tenant_id, account_id, bot_id, seq DESC)
                    """
                )

        self._run("init_tables", _op)

    def read_long_term(self) -> str:
        tenant, account, bot = self._base_scope()
        try:
            def _op():
                with self._cursor() as cur:
                    cur.execute(
                        """
                        SELECT memory_markdown
                        FROM ithqbot_memory
                        WHERE tenant_id = %s AND account_id = %s AND bot_id = %s
                        """,
                        (tenant, account, bot),
                    )
                    return cur.fetchone()

            row = self._run("read_long_term", _op)
            return row[0] if row and row[0] else ""
        except Exception as e:
            logger.error("Failed to read long-term memory from PostgreSQL: {}", e)
            return ""

    def write_long_term(self, content: str) -> None:
        tenant, account, bot = self._base_scope()
        self._run(
            "write_long_term",
            lambda: self._write_long_term(tenant, account, bot, content),
        )

    def _write_long_term(self, tenant: str, account: str, bot: str, content: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO ithqbot_memory (tenant_id, account_id, bot_id, memory_markdown)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (tenant_id, account_id, bot_id)
                DO UPDATE SET memory_markdown = EXCLUDED.memory_markdown, updated_at = NOW()
                """,
                (tenant, account, bot, content),
            )

    def append_history(self, entry: str) -> None:
        tenant, account, bot = self._base_scope()
        normalized_entry = entry.rstrip()
        self._run(
            "append_history",
            lambda: self._append_history(tenant, account, bot, normalized_entry),
        )

    def _append_history(self, tenant: str, account: str, bot: str, entry: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO ithqbot_memory_history (tenant_id, account_id, bot_id, entry)
                VALUES (%s, %s, %s, %s)
                """,
                (tenant, account, bot, entry),
            )
            cur.execute(
                """
                DELETE FROM ithqbot_memory_history
                WHERE tenant_id = %s
                  AND account_id = %s
                  AND bot_id = %s
                  AND seq < COALESCE(
                    (
                        SELECT MIN(seq)
                        FROM (
                            SELECT seq
                            FROM ithqbot_memory_history
                            WHERE tenant_id = %s
                              AND account_id = %s
                              AND bot_id = %s
                            ORDER BY seq DESC
                            OFFSET 5000
                        ) AS keep_tail
                    ),
                    0
                  )
                """,
                (tenant, account, bot, tenant, account, bot),
            )

    def _raw_archive(self, messages: list[dict]) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.append_history(
            f"[{ts}] [RAW] {len(messages)} messages\n"
            f"{self._format_messages(messages)}"
        )
        logger.warning(
            "Memory consolidation degraded: raw-archived {} messages (postgresql)", len(messages)
        )


MemoryStoreFactory = Callable[[Path, str], MemoryStore]
_MEMORY_STORE_FACTORIES: dict[str, MemoryStoreFactory] = {}


def register_memory_store_backend(scheme: str, factory: MemoryStoreFactory) -> None:
    normalized = (scheme or "").lower().strip()
    if normalized:
        _MEMORY_STORE_FACTORIES[normalized] = factory


register_memory_store_backend("redis", lambda workspace, uri: RedisMemoryStore(workspace=workspace, uri=uri))
register_memory_store_backend("rediss", lambda workspace, uri: RedisMemoryStore(workspace=workspace, uri=uri))
register_memory_store_backend(
    "postgresql", lambda workspace, uri: PostgreSQLMemoryStore(workspace=workspace, uri=uri)
)
register_memory_store_backend(
    "postgres", lambda workspace, uri: PostgreSQLMemoryStore(workspace=workspace, uri=uri)
)
register_memory_store_backend(
    "file",
    lambda workspace, uri: MemoryStore(Path(uri[7:]) if uri.startswith("file://") else workspace),
)


def _sanitize_store_uri(uri: str) -> str:
    parsed = urlparse(uri)
    if not parsed.scheme:
        return uri
    auth = ""
    if parsed.username:
        auth = parsed.username
        if parsed.password:
            auth = f"{auth}:***"
        auth = f"{auth}@"
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"{auth}{host}{port}"
    return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


def _probe_memory_store_health(store: MemoryStore) -> None:
    client = getattr(store, "client", None)
    if client is not None and hasattr(client, "ping"):
        client.ping()
        return
    conn = getattr(store, "_conn", None)
    if conn is not None:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        finally:
            cursor.close()


def create_memory_store(
    workspace: Path,
    uri: str | None = None,
    allow_local_fallback: bool = True,
) -> MemoryStore:
    _ = allow_local_fallback
    resolved_uri = uri or os.getenv("ITHQBOT_MEMORY_STORE")
    if resolved_uri:
        raw_scheme = (urlparse(resolved_uri).scheme or "").lower()
        scheme = raw_scheme.split("+", 1)[0]
        factory = _MEMORY_STORE_FACTORIES.get(scheme)
        if factory:
            safe_uri = _sanitize_store_uri(resolved_uri)
            last_error: Exception | None = None
            for attempt in range(1, 4):
                try:
                    store = factory(workspace, resolved_uri)
                    _probe_memory_store_health(store)
                    if attempt > 1:
                        logger.info("Memory store {} recovered on attempt {}", safe_uri, attempt)
                    return store
                except ImportError:
                    raise
                except Exception as exc:
                    last_error = exc
                    if attempt < 3:
                        logger.warning(
                            "Memory store {} init failed on attempt {}/3: {}",
                            safe_uri,
                            attempt,
                            exc,
                        )
                        time.sleep(0.1 * (2 ** (attempt - 1)))
            raise RuntimeError(
                "External memory store initialization failed for "
                f"{_sanitize_store_uri(resolved_uri)}: {last_error}"
            ) from last_error
        raise ValueError(f"Unsupported memory store URI scheme: {scheme}")
    raise ValueError("memory_store_uri is not configured")


class MemoryConsolidator:
    """Owns consolidation policy, locking, and session offset updates."""

    _MAX_CONSOLIDATION_ROUNDS = 5

    def __init__(
        self,
        workspace: Path,
        provider: LLMProvider,
        model: str,
        sessions: SessionManager,
        context_window_tokens: int,
        build_messages: Callable[..., list[dict[str, Any]]],
        get_tool_definitions: Callable[[], list[dict[str, Any]]],
        memory_store_uri: str | None = None,
        allow_local_memory_fallback: bool = True,
    ):
        self.store = create_memory_store(
            workspace,
            memory_store_uri,
            allow_local_fallback=allow_local_memory_fallback,
        )
        self.provider = provider
        self.model = model
        self.sessions = sessions
        self.context_window_tokens = context_window_tokens
        self._build_messages = build_messages
        self._get_tool_definitions = get_tool_definitions
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    def get_lock(self, session_key: str) -> asyncio.Lock:
        """Return the shared consolidation lock for one session."""
        return self._locks.setdefault(session_key, asyncio.Lock())

    @staticmethod
    def _normalize_explicit_name(raw: str) -> str | None:
        name = str(raw or "").strip().strip("“”\"'`")
        name = re.sub(r"\s+", " ", name)
        if not name or len(name) > 12:
            return None
        lowered = name.lower()
        if lowered in {"一个", "一下", "一下子", "自己"}:
            return None
        return name

    @classmethod
    def _extract_explicit_memory_facts_from_text(cls, text: str) -> list[str]:
        if not isinstance(text, str) or not text.strip():
            return []
        facts: list[str] = []
        for pattern in _EXPLICIT_NAME_PATTERNS:
            for match in pattern.finditer(text):
                name = cls._normalize_explicit_name(match.group("name"))
                if name:
                    facts.append(f"用户称呼：{name}")
        return facts

    def _persist_explicit_memory_from_session(self, session: Session) -> bool:
        processed = session.metadata.get("_explicit_memory_processed_messages", 0)
        try:
            start = max(0, min(int(processed), len(session.messages)))
        except Exception:
            start = 0

        facts: list[str] = []
        for message in session.messages[start:]:
            if message.get("role") != "user":
                continue
            facts.extend(self._extract_explicit_memory_facts_from_text(str(message.get("content") or "")))

        session.metadata["_explicit_memory_processed_messages"] = len(session.messages)
        persisted = self.store.persist_explicit_facts(facts)
        if facts or start != len(session.messages):
            self.sessions.save(session)
        return persisted

    def persist_explicit_memory(self, session: Session) -> bool:
        """Persist user-declared facts immediately after a turn is saved."""
        return self._persist_explicit_memory_from_session(session)

    async def consolidate_messages(self, messages: list[dict[str, object]]) -> bool:
        """Archive a selected message chunk into persistent memory."""
        return await self.store.consolidate(messages, self.provider, self.model)

    def pick_consolidation_boundary(
        self,
        session: Session,
        tokens_to_remove: int,
    ) -> tuple[int, int] | None:
        """Pick a user-turn boundary that removes enough old prompt tokens."""
        start = session.last_consolidated
        if start >= len(session.messages) or tokens_to_remove <= 0:
            return None

        removed_tokens = 0
        last_boundary: tuple[int, int] | None = None
        for idx in range(start, len(session.messages)):
            message = session.messages[idx]
            if idx > start and message.get("role") == "user":
                last_boundary = (idx, removed_tokens)
                if removed_tokens >= tokens_to_remove:
                    return last_boundary
            removed_tokens += estimate_message_tokens(message)

        return last_boundary

    def estimate_session_prompt_tokens(self, session: Session) -> tuple[int, str]:
        """Estimate current prompt size for the normal session history view."""
        history = session.get_history(max_messages=0)
        channel, chat_id = (session.key.split(":", 1) if ":" in session.key else (None, None))
        probe_messages = self._build_messages(
            history=history,
            current_message="[token-probe]",
            channel=channel,
            chat_id=chat_id,
        )
        return estimate_prompt_tokens_chain(
            self.provider,
            self.model,
            probe_messages,
            self._get_tool_definitions(),
        )

    async def archive_messages(self, messages: list[dict[str, object]]) -> bool:
        """Archive messages with guaranteed persistence (retries until raw-dump fallback)."""
        if not messages:
            return True
        for _ in range(self.store._MAX_FAILURES_BEFORE_RAW_ARCHIVE):
            if await self.consolidate_messages(messages):
                return True
        return True

    async def maybe_consolidate_by_tokens(self, session: Session) -> None:
        """Loop: archive old messages until prompt fits within half the context window."""
        if not session.messages or self.context_window_tokens <= 0:
            return

        lock = self.get_lock(session.key)
        async with lock:
            self._persist_explicit_memory_from_session(session)
            target = self.context_window_tokens // 2
            estimated, source = self.estimate_session_prompt_tokens(session)
            if estimated <= 0:
                return
            if estimated < self.context_window_tokens:
                logger.debug(
                    "Token consolidation idle {}: {}/{} via {}",
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    source,
                )
                return

            for round_num in range(self._MAX_CONSOLIDATION_ROUNDS):
                if estimated <= target:
                    return

                boundary = self.pick_consolidation_boundary(session, max(1, estimated - target))
                if boundary is None:
                    logger.debug(
                        "Token consolidation: no safe boundary for {} (round {})",
                        session.key,
                        round_num,
                    )
                    return

                end_idx = boundary[0]
                chunk = session.messages[session.last_consolidated:end_idx]
                if not chunk:
                    return

                logger.info(
                    "Token consolidation round {} for {}: {}/{} via {}, chunk={} msgs",
                    round_num,
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    source,
                    len(chunk),
                )
                if not await self.consolidate_messages(chunk):
                    return
                session.last_consolidated = end_idx
                self.sessions.save(session)

                estimated, source = self.estimate_session_prompt_tokens(session)
                if estimated <= 0:
                    return
