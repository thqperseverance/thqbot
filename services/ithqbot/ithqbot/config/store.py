"""Configuration storage implementations."""

import json
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from loguru import logger
from ithqbot.utils.crypto import ConfigEncryptor


class ConfigStore(ABC):
    """Abstract base class for configuration storage."""

    def __init__(self, encryptor: ConfigEncryptor | None = None):
        self.encryptor = encryptor

    def _encrypt(self, data: str) -> str:
        if self.encryptor:
            return self.encryptor.encrypt(data)
        return data

    def _decrypt(self, data: str) -> str:
        if self.encryptor:
            try:
                return self.encryptor.decrypt(data)
            except Exception as e:
                logger.error(f"Failed to decrypt config: {e}")
                raise
        return data

    @abstractmethod
    def load(self, bot_id: str) -> dict[str, Any] | None:
        """Load configuration for a specific bot."""
        pass

    @abstractmethod
    def save(self, bot_id: str, data: dict[str, Any]) -> None:
        """Save configuration for a specific bot."""
        pass


class FileConfigStore(ConfigStore):
    """Filesystem-based configuration storage."""

    def __init__(self, base_path: Path, encryptor: ConfigEncryptor | None = None):
        super().__init__(encryptor)
        self.base_path = base_path

    def _get_path(self, bot_id: str) -> Path:
        if bot_id == "default" or not bot_id:
            return self.base_path / "config.json"
        return self.base_path / f"config-{bot_id}.json"

    def load(self, bot_id: str) -> dict[str, Any] | None:
        path = self._get_path(bot_id)
        if not path.exists():
            # Fallback to default if bot-specific doesn't exist
            if bot_id != "default":
                path = self.base_path / "config.json"
            if not path.exists():
                return None

        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
                if not content.strip():
                    return None
                if content.strip().startswith("{"):
                    # Plain JSON
                    return json.loads(content)
                # Try decrypt
                decrypted = self._decrypt(content)
                return json.loads(decrypted)
        except Exception as e:
            logger.error(f"Failed to load config from {path}: {e}")
            return None

    def save(self, bot_id: str, data: dict[str, Any]) -> None:
        path = self._get_path(bot_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            content = json.dumps(data, indent=2, ensure_ascii=False)
            final_content = self._encrypt(content)
            with open(path, "w", encoding="utf-8") as f:
                f.write(final_content)
        except Exception as e:
            logger.error(f"Failed to save config to {path}: {e}")
            raise


class RedisConfigStore(ConfigStore):
    """Redis-based configuration storage."""

    def __init__(self, uri: str, key_prefix: str = "ithqbot:config:", encryptor: ConfigEncryptor | None = None):
        super().__init__(encryptor)
        import redis
        self.client = redis.from_url(uri, decode_responses=True)
        self.key_prefix = key_prefix

    def _get_key(self, bot_id: str) -> str:
        return f"{self.key_prefix}{bot_id}"

    def load(self, bot_id: str) -> dict[str, Any] | None:
        key = self._get_key(bot_id)
        data = self.client.get(key)
        if not data:
            return None
        try:
            if data.strip().startswith("{"):
                return json.loads(data)
            decrypted = self._decrypt(data)
            return json.loads(decrypted)
        except Exception as e:
            logger.error(f"Failed to parse config from Redis key {key}: {e}")
            return None

    def save(self, bot_id: str, data: dict[str, Any]) -> None:
        key = self._get_key(bot_id)
        try:
            content = json.dumps(data, ensure_ascii=False)
            final_content = self._encrypt(content)
            self.client.set(key, final_content)
        except Exception as e:
            logger.error(f"Failed to save config to Redis key {key}: {e}")
            raise


class SqlConfigStore(ConfigStore):
    """SQL-based configuration storage (PostgreSQL)."""

    def __init__(self, uri: str, table_name: str = "bot_configs", encryptor: ConfigEncryptor | None = None):
        super().__init__(encryptor)
        self.uri = uri
        self.table_name = table_name
        self._initialized = False

    def _ensure_table(self, conn):
        if self._initialized:
            return
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.table_name} (
                    bot_id TEXT PRIMARY KEY,
                    config JSONB NOT NULL,
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                )
            """)
        conn.commit()
        self._initialized = True

    def load(self, bot_id: str) -> dict[str, Any] | None:
        import psycopg
        try:
            with psycopg.connect(self.uri) as conn:
                self._ensure_table(conn)
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT config FROM {self.table_name} WHERE bot_id = %s",
                        (bot_id,)
                    )
                    row = cur.fetchone()
                    if not row:
                        return None
                    data = row[0]
                    if isinstance(data, dict):
                        return data
                    if isinstance(data, str) and data.strip().startswith("{"):
                        return json.loads(data)
                    decrypted = self._decrypt(data)
                    return json.loads(decrypted)
        except Exception as e:
            logger.error(f"Failed to load config from SQL (bot_id={bot_id}): {e}")
            return None

    def save(self, bot_id: str, data: dict[str, Any]) -> None:
        import psycopg
        try:
            with psycopg.connect(self.uri) as conn:
                self._ensure_table(conn)
                content = json.dumps(data)
                final_content = self._encrypt(content)
                with conn.cursor() as cur:
                    cur.execute(f"""
                        INSERT INTO {self.table_name} (bot_id, config, updated_at)
                        VALUES (%s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (bot_id) DO UPDATE
                        SET config = EXCLUDED.config, updated_at = EXCLUDED.updated_at
                    """, (bot_id, final_content))
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to save config to SQL (bot_id={bot_id}): {e}")
            raise
