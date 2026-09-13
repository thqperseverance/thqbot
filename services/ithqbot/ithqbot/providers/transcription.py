"""Voice transcription provider using Groq."""

import os
from pathlib import Path

import httpx
from loguru import logger

from ithqbot.utils.http_client import HTTPClientError, classify_http_client_error


class GroqTranscriptionProvider:
    """
    Voice transcription provider using Groq's Whisper API.

    Groq offers extremely fast transcription with a generous free tier.
    """

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("GROQ_API_KEY")
        self.api_url = "https://api.groq.com/openai/v1/audio/transcriptions"

    @staticmethod
    def classify_error(exc: Exception) -> HTTPClientError:
        return classify_http_client_error("Groq 语音转写", exc)

    async def transcribe(self, file_path: str | Path) -> str:
        """
        Transcribe an audio file using Groq.

        Args:
            file_path: Path to the audio file.

        Returns:
            Transcribed text.
        """
        if not self.api_key:
            logger.warning("Groq 语音转写未配置 API Key")
            return ""

        path = Path(file_path)
        if not path.exists():
            logger.error("语音文件不存在: {}", file_path)
            return ""

        try:
            async with httpx.AsyncClient() as client:
                with open(path, "rb") as f:
                    files = {
                        "file": (path.name, f),
                        "model": (None, "whisper-large-v3"),
                    }
                    headers = {
                        "Authorization": f"Bearer {self.api_key}",
                    }

                    response = await client.post(
                        self.api_url,
                        headers=headers,
                        files=files,
                        timeout=60.0
                    )

                    response.raise_for_status()
                    data = response.json()
                    return data.get("text", "")

        except Exception as e:
            error = self.classify_error(e)
            logger.error(
                "Groq 语音转写失败: error_type={} retryable={} http_status={} detail={}",
                error.error_type,
                error.retryable,
                error.http_status,
                error.detail,
            )
            return ""
