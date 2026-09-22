"""Async httpx client for the YarnGPT API.

Reference: https://yarngpt-web.azurewebsites.net/api-docs
Quickstart: https://yarngpt-web.azurewebsites.net/get-started

Conventions honoured here:
- Auth is ``Authorization: Bearer <key>`` on every route except
  ``GET /tts/stream/{ticket}`` (the ticket itself is the credential).
- ``POST /tts`` and ``POST /asr`` require an ``Idempotency-Key`` header;
  replaying the same key + body returns the original job for free, while the
  same key with a different body is ``409 IDEMPOTENCY_KEY_REUSED``.
- Errors share one envelope: ``{"error": {"code", "message", "user_message",
  "service", "trace_id", "details"}}`` — branch on ``code``, show
  ``user_message`` to people, log ``trace_id`` (== ``X-Request-Id``).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import httpx


class YarnGPTError(Exception):
    """Typed YarnGPT failure (API error envelope or local client error)."""

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        status: int | None = None,
        user_message: str = "",
        service: str = "",
        trace_id: str = "",
        details: Any = None,
    ) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message
        self.status = status
        self.user_message = user_message or message or code
        self.service = service
        self.trace_id = trace_id
        self.details = details

    def __str__(self) -> str:  # pragma: no cover - trivial
        parts = [f"[{self.code}]", self.message or self.user_message]
        if self.status is not None:
            parts.append(f"(http {self.status})")
        if self.trace_id:
            parts.append(f"trace {self.trace_id}")
        return " ".join(parts)


class YarnGPTClient:
    """Minimal async client for YarnGPT TTS / STT / voices / usage routes."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.yarngpt.ai/api/v1",
        timeout: float = 30.0,
    ) -> None:
        if not api_key or not api_key.strip():
            raise YarnGPTError(
                "MISSING_API_KEY",
                "YARNGPT_API_KEY is not set",
                user_message="Set YARNGPT_API_KEY in .env to call the YarnGPT API.",
            )
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key.strip()}"},
        )
        self._default_voice_name: str | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> YarnGPTClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    @staticmethod
    def new_idempotency_key() -> str:
        """Fresh idempotency key — generate one per TTS request / ASR upload."""
        return uuid.uuid4().hex

    async def _resolve_voice(self, voice: str | None) -> str | None:
        """Return ``voice``, or the catalogue default.

        The docs describe ``voice`` as optional, but this deployment rejects
        an omitted voice with ``422 INVALID_INPUT`` (``body.voice`` required),
        so fall back to the catalogue entry with ``default: true`` instead of
        sending no voice at all. The id is read live, never hardcoded.
        """
        if voice:
            return voice
        if self._default_voice_name is None:
            try:
                data = await self.list_voices()
            except YarnGPTError:
                return None
            items = data.get("voices", data) if isinstance(data, dict) else data
            name = ""
            if isinstance(items, list):
                default = next(
                    (
                        i
                        for i in items
                        if isinstance(i, dict) and i.get("default") and i.get("name")
                    ),
                    None,
                )
                if default is not None:
                    name = default["name"]
                elif items:
                    first = items[0]
                    name = first.get("name", "") if isinstance(first, dict) else str(first)
            self._default_voice_name = name
        return self._default_voice_name or None

    # -- internals ------------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = await self._client.request(method, path, **kwargs)
        trace_id = resp.headers.get("x-request-id", "")
        if 200 <= resp.status_code < 300:
            if not resp.content:
                return {}
            ctype = resp.headers.get("content-type", "")
            if "application/json" in ctype:
                return resp.json()
            return resp.text
        raise self._parse_error(resp, trace_id)

    @staticmethod
    def _parse_error(resp: httpx.Response, trace_id: str) -> YarnGPTError:
        try:
            err = resp.json().get("error", {})
        except ValueError:
            err = {}
        if isinstance(err, dict) and err.get("code"):
            return YarnGPTError(
                err.get("code", f"HTTP_{resp.status_code}"),
                err.get("message", ""),
                status=resp.status_code,
                user_message=err.get("user_message", ""),
                service=err.get("service", ""),
                trace_id=err.get("trace_id", trace_id),
                details=err.get("details"),
            )
        # Non-envelope refusal (e.g. gateway plain-text preflight).
        body = (resp.text or "").strip()[:300]
        return YarnGPTError(
            f"HTTP_{resp.status_code}",
            body or resp.reason_phrase,
            status=resp.status_code,
            user_message=body or "The request was refused before reaching the API.",
            trace_id=trace_id,
        )

    # -- voices / languages / usage -------------------------------------

    async def list_voices(self) -> Any:
        """GET /voices — the only source of truth for voice ids."""
        return await self._request("GET", "/voices")

    async def get_voice(self, name: str) -> Any:
        """GET /voices/{name} — 404 VOICE_NOT_FOUND for an unknown name."""
        return await self._request("GET", f"/voices/{name}")

    async def list_asr_languages(self) -> Any:
        """GET /asr/languages — coverage info, not a language selector."""
        return await self._request("GET", "/asr/languages")

    async def daily_stats(self, date: str | None = None) -> Any:
        """GET /analytics/daily-stats — per-UTC-day call counts."""
        params = {"date": date} if date else None
        return await self._request("GET", "/analytics/daily-stats", params=params)

    # -- text-to-speech (async job) --------------------------------------

    async def tts_queue(
        self,
        text: str,
        *,
        voice: str | None = None,
        output_format: str = "mp3",
        target_language: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """POST /tts — queue synthesis, returns ``{job_id, status, ...}``."""
        body: dict[str, Any] = {
            "text": text,
            "voice": await self._resolve_voice(voice),
            "output_format": output_format,
        }
        if body["voice"] is None:
            del body["voice"]
        if target_language:
            body["target_language"] = target_language
        headers = {"Idempotency-Key": idempotency_key or self.new_idempotency_key()}
        data = await self._request("POST", "/tts", json=body, headers=headers)
        return dict(data)

    async def tts_status(self, job_id: str) -> dict[str, Any]:
        """GET /status/{job_id} — poll until ``completed``; read ``audio_url``."""
        data = await self._request("GET", f"/status/{job_id}")
        return dict(data)

    async def tts_poll(
        self,
        job_id: str,
        *,
        timeout_s: float = 120.0,
        interval_s: float = 1.0,
    ) -> dict[str, Any]:
        """Poll ``tts_status`` until ``completed``/``failed`` or timeout."""
        deadline = time.monotonic() + timeout_s
        last: dict[str, Any] = {}
        while True:
            last = await self.tts_status(job_id)
            if last.get("status") in ("completed", "failed"):
                return last
            if time.monotonic() >= deadline:
                raise YarnGPTError(
                    "POLL_TIMEOUT",
                    f"TTS job {job_id} not settled after {timeout_s}s",
                    user_message="Synthesis is taking longer than expected — try polling again.",
                    details={"job_id": job_id, "last_status": last.get("status")},
                )
            await asyncio.sleep(interval_s)

    # -- text-to-speech (streaming ticket) --------------------------------

    async def tts_prepare(
        self,
        text: str,
        *,
        voice: str | None = None,
        output_format: str = "mp3",
        target_language: str | None = None,
    ) -> dict[str, Any]:
        """POST /tts/prepare — mint a ticket; no Idempotency-Key on this route."""
        body: dict[str, Any] = {
            "text": text,
            "voice": await self._resolve_voice(voice),
            "output_format": output_format,
        }
        if body["voice"] is None:
            del body["voice"]
        if target_language:
            body["target_language"] = target_language
        data = await self._request("POST", "/tts/prepare", json=body)
        return dict(data)

    def stream_url(self, stream_path: str) -> str:
        """Join a ``stream_url`` path from tts_prepare onto the base URL."""
        return f"{self.base_url}/{stream_path.lstrip('/')}"

    # -- conversation (single-turn realtime) -------------------------------

    async def conversation(
        self,
        text: str,
        *,
        voice: str | None = None,
        output_format: str = "mp3",
        target_language: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[bytes, str]:
        """POST /streaming/conversation — raw audio bytes + content type.

        No job, no polling. Check the byte count before treating a 200 as
        audio — a total provider outage can answer 200 with a zero-byte body.
        """
        body: dict[str, Any] = {
            "text": text,
            "voice": await self._resolve_voice(voice),
            "output_format": output_format,
        }
        if body["voice"] is None:
            del body["voice"]
        if target_language:
            body["target_language"] = target_language
        headers = (
            {"Idempotency-Key": idempotency_key} if idempotency_key else None
        )
        resp = await self._client.post(
            "/streaming/conversation", json=body, headers=headers
        )
        if 200 <= resp.status_code < 300:
            if not resp.content:
                raise YarnGPTError(
                    "EMPTY_AUDIO",
                    "Conversation route returned 200 with an empty body",
                    status=resp.status_code,
                    user_message="Synthesis returned no audio — retry once.",
                    trace_id=resp.headers.get("x-request-id", ""),
                )
            return resp.content, resp.headers.get("content-type", "")
        raise self._parse_error(resp, resp.headers.get("x-request-id", ""))

    # -- speech-to-text (async job) -----------------------------------------

    async def asr_upload(
        self,
        file_bytes: bytes,
        filename: str,
        content_type: str = "application/octet-stream",
        *,
        target_language: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """POST /asr (multipart ``file``) — returns job id + price quote."""
        files = {"file": (filename, file_bytes, content_type)}
        data = {"target_language": target_language} if target_language else {}
        headers = {"Idempotency-Key": idempotency_key or self.new_idempotency_key()}
        result = await self._request(
            "POST", "/asr", files=files, data=data, headers=headers
        )
        return dict(result)

    async def asr_status(self, job_id: str) -> dict[str, Any]:
        """GET /asr/{job_id} — branch on ``status``, not on ``transcript``.

        A completed job can legitimately carry an empty transcript (silence).
        """
        data = await self._request("GET", f"/asr/{job_id}")
        return dict(data)

    async def asr_poll(
        self,
        job_id: str,
        *,
        timeout_s: float = 300.0,
        interval_s: float = 2.0,
    ) -> dict[str, Any]:
        """Poll ``asr_status`` until ``completed``/``failed`` or timeout."""
        deadline = time.monotonic() + timeout_s
        last: dict[str, Any] = {}
        while True:
            last = await self.asr_status(job_id)
            if last.get("status") in ("completed", "failed"):
                return last
            if time.monotonic() >= deadline:
                raise YarnGPTError(
                    "POLL_TIMEOUT",
                    f"ASR job {job_id} not settled after {timeout_s}s",
                    user_message="Transcription is taking longer than expected — keep polling.",
                    details={"job_id": job_id, "last_status": last.get("status")},
                )
            await asyncio.sleep(interval_s)

    async def asr_audio_link(self, job_id: str) -> dict[str, Any]:
        """GET /asr/{job_id}/audio — fresh signed link; fetch on demand."""
        data = await self._request("GET", f"/asr/{job_id}/audio")
        return dict(data)
