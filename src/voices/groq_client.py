"""Groq speech-to-text via the official ``groq`` SDK.

Reference: https://console.groq.com/docs/speech-to-text
Endpoint: POST {base}/audio/transcriptions (OpenAI-compatible).

Unlike YarnGPT ASR there is no job queue — the transcript comes back in the
same response, so callers must not poll. `language` is an ISO-639-1 hint that
improves accuracy/latency; `prompt` (≤224 tokens) biases spelling and style.
The blocking SDK call runs in a worker thread so async views stay responsive.
"""

from __future__ import annotations

import asyncio
from typing import Any

from groq import APIError, Groq

from voices.config import GROQ_LANGUAGE_MAP, GROQ_PROMPTS


class GroqError(Exception):
    """Groq STT failure (message safe to show, details for logs)."""

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        status: int | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message
        self.status = status
        self.details = details

    def __str__(self) -> str:  # pragma: no cover - trivial
        parts = [f"[{self.code}]", self.message or self.code]
        if self.status is not None:
            parts.append(f"(http {self.status})")
        return " ".join(parts)


class GroqClient:
    """Thin async wrapper around the official Groq SDK's transcriptions."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.groq.com",
        model: str = "whisper-large-v3-turbo",
        timeout: float = 120.0,
    ) -> None:
        if not api_key or not api_key.strip():
            raise GroqError(
                "MISSING_GROQ_KEY",
                "GROQ_API_KEY is not set",
            )
        self.model = model
        # The SDK's endpoint paths already include /openai/v1, so the base
        # must be host-only — strip a trailing /openai/v1 if configured.
        base = base_url.rstrip("/")
        if base.lower().endswith("/openai/v1"):
            base = base[: -len("/openai/v1")] or base
        self.base_url = base
        self._client = Groq(api_key=api_key.strip(), base_url=base, timeout=timeout)

    async def aclose(self) -> None:
        return None

    async def __aenter__(self) -> GroqClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    @staticmethod
    def map_language(code: str | None) -> tuple[str, str]:
        """Map an app language code to (groq language, prompt)."""
        key = (code or "en").strip().lower()
        language = GROQ_LANGUAGE_MAP.get(key, "en")
        return language, GROQ_PROMPTS.get(language, "")

    async def transcribe(
        self,
        file_bytes: bytes,
        filename: str,
        content_type: str = "application/octet-stream",
        *,
        language: str | None = None,
        prompt: str | None = None,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        """Transcribe audio — returns ``{text, language}`` directly."""
        lang, default_prompt = self.map_language(language)
        use_prompt = prompt if prompt is not None else default_prompt
        kwargs: dict[str, Any] = {
            "file": (filename, file_bytes),
            "model": self.model,
            "language": lang,
            "temperature": temperature,
            "response_format": "json",
        }
        if use_prompt:
            kwargs["prompt"] = use_prompt[:1500]
        try:
            result = await asyncio.to_thread(
                self._client.audio.transcriptions.create, **kwargs
            )
        except APIError as err:
            raise GroqError(
                f"HTTP_{err.status_code}",
                err.message or "Groq transcription failed.",
                status=err.status_code,
                details={"body": str(err.body)[:300] if err.body else ""},
            ) from None
        text = getattr(result, "text", "") or ""
        return {"text": text, "language": lang}
