from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger(__name__)


class ASRError(RuntimeError):
    pass


class _RetryableASRError(ASRError):
    """Internal marker for transient upstream failures (5xx/429)."""


@dataclass(frozen=True)
class ASRResult:
    text: str
    language: str | None = None
    duration: float | None = None
    raw: dict | None = None


class ASRClient:
    """OpenAI-compatible /audio/transcriptions client (Qwen DashScope by default)."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        language: str | None = None,
        timeout: float = 120.0,
        max_retries: int = 3,
        retry_backoff: float = 1.5,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._language = language
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "ASRClient":
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(self._timeout),
            headers={"Authorization": f"Bearer {self._api_key}"} if self._api_key else {},
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def transcribe(self, file_path: Path, *, prompt: str | None = None) -> ASRResult:
        if self._client is None:
            raise RuntimeError("ASRClient must be used as async context manager")

        data: dict[str, str] = {"model": self._model, "response_format": "json"}
        if self._language:
            data["language"] = self._language
        if prompt:
            data["prompt"] = prompt

        last_err: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                with file_path.open("rb") as f:
                    files = {"file": (file_path.name, f, "audio/wav")}
                    resp = await self._client.post(
                        "/audio/transcriptions", data=data, files=files,
                    )
                if resp.status_code >= 500 or resp.status_code == 429:
                    raise _RetryableASRError(f"upstream {resp.status_code}: {resp.text[:200]}")
                if resp.status_code >= 400:
                    # 4xx (other than 429) → don't retry; surface immediately
                    raise ASRError(f"client error {resp.status_code}: {resp.text[:200]}")
                payload = resp.json()
                return ASRResult(
                    text=(payload.get("text") or "").strip(),
                    language=payload.get("language"),
                    duration=payload.get("duration"),
                    raw=payload,
                )
            except (httpx.TimeoutException, httpx.TransportError, _RetryableASRError) as e:
                last_err = e
                if attempt >= self._max_retries:
                    break
                delay = self._retry_backoff ** attempt
                log.warning("asr retry %d/%d after %.1fs: %s", attempt + 1, self._max_retries, delay, e)
                await asyncio.sleep(delay)

        raise ASRError(f"transcribe failed after {self._max_retries + 1} attempts: {last_err}")
