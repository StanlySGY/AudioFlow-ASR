from __future__ import annotations

import io
import logging
import tempfile
import time
import uuid
import wave
from pathlib import Path

import aiofiles
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from sse_starlette.sse import EventSourceResponse

from app.config import get_settings
from app.models.schemas import TaskInfo, TaskResult, TaskStatus
from app.security import require_token
from app.services.asr import ASRError, create_provider, list_providers
from app.services.stream_manager import TaskManager
from app.services.subtitles import to_srt, to_vtt

log = logging.getLogger(__name__)
router = APIRouter(prefix="/asr", tags=["asr"], dependencies=[Depends(require_token)])
meta_router = APIRouter(tags=["meta"])


def get_manager(request: Request) -> TaskManager:
    return request.app.state.manager


ALLOWED_EXTS = {
    ".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg", ".pcm",
    ".mp4", ".mov", ".mkv",
}


@router.post("/task")
async def create_task(
    file: UploadFile = File(...),
    model: str | None = Form(default=None),
    language: str | None = Form(default=None),
    split_strategy: str | None = Form(default=None),
    chunk_seconds: float | None = Form(default=None),
    overlap_seconds: float | None = Form(default=None),
    hotwords: str | None = Form(default=None),
    prompt_hints: str | None = Form(default=None),
    timestamps: bool | None = Form(default=None),
    manager: TaskManager = Depends(get_manager),
) -> dict[str, str]:
    settings = get_settings()
    if not file.filename:
        raise HTTPException(400, "missing filename")
    suffix = Path(file.filename).suffix.lower()
    if suffix and suffix not in ALLOWED_EXTS:
        raise HTTPException(400, f"unsupported file type: {suffix}")

    upload_id = uuid.uuid4().hex
    dst = settings.temp_dir / f"upload_{upload_id}{suffix or '.bin'}"
    limit = settings.max_upload_bytes
    written = 0
    try:
        async with aiofiles.open(dst, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > limit:
                    raise HTTPException(413, f"upload exceeds {limit} bytes")
                await out.write(chunk)
    except HTTPException:
        dst.unlink(missing_ok=True)
        raise
    except Exception:
        dst.unlink(missing_ok=True)
        raise

    overrides: dict = {}
    if model is not None: overrides["asr_model"] = model
    if language is not None: overrides["asr_language"] = language or None
    if split_strategy is not None:
        if split_strategy not in {"fixed", "silence", "overlap"}:
            dst.unlink(missing_ok=True)
            raise HTTPException(400, "split_strategy must be fixed|silence|overlap")
        overrides["split_strategy"] = split_strategy
    if chunk_seconds is not None: overrides["split_chunk_seconds"] = chunk_seconds
    if overlap_seconds is not None: overrides["split_overlap_seconds"] = overlap_seconds
    if hotwords is not None: overrides["asr_hotwords"] = hotwords
    if prompt_hints is not None: overrides["asr_prompt_hints"] = prompt_hints
    if timestamps is not None: overrides["asr_timestamps"] = timestamps

    task_id = await manager.submit(dst, file.filename, overrides=overrides or None)
    return {"task_id": task_id}


@router.get("/task/{task_id}", response_model=TaskInfo)
async def get_status(task_id: str, manager: TaskManager = Depends(get_manager)) -> TaskInfo:
    info = manager.get_info(task_id)
    if info is None:
        raise HTTPException(404, "task not found")
    return info


@router.get("/task/{task_id}/stream")
async def stream_task(task_id: str, manager: TaskManager = Depends(get_manager)) -> EventSourceResponse:
    if manager.get_info(task_id) is None:
        raise HTTPException(404, "task not found")

    async def event_gen():
        async for evt in manager.stream(task_id):
            yield {"event": "segment", "data": evt.model_dump_json()}
        info = manager.get_info(task_id)
        if info is not None:
            yield {"event": "done", "data": info.model_dump_json()}

    return EventSourceResponse(event_gen())


@router.get("/task/{task_id}/result", response_model=TaskResult)
async def get_result(task_id: str, manager: TaskManager = Depends(get_manager)) -> TaskResult | Response:
    result = manager.get_result(task_id)
    if result is None:
        raise HTTPException(404, "task not found")
    if result.status not in {TaskStatus.done, TaskStatus.failed}:
        return JSONResponse(status_code=202, content=result.model_dump(mode="json"))
    return result


@router.get("/task/{task_id}/subtitle", response_class=PlainTextResponse)
async def get_subtitle(
    task_id: str,
    format: str = "srt",
    manager: TaskManager = Depends(get_manager),
) -> Response:
    fmt = format.lower()
    if fmt not in {"srt", "vtt"}:
        raise HTTPException(400, "format must be 'srt' or 'vtt'")
    result = manager.get_result(task_id)
    if result is None:
        raise HTTPException(404, "task not found")
    if result.status != TaskStatus.done:
        raise HTTPException(409, f"task not ready (status={result.status.value})")

    body = to_srt(result.segments) if fmt == "srt" else to_vtt(result.segments)
    media = "application/x-subrip" if fmt == "srt" else "text/vtt"
    return Response(
        content=body,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{task_id}.{fmt}"'},
    )


@meta_router.get("/auth/info")
async def auth_info() -> dict[str, bool]:
    return {"auth_required": bool(get_settings().access_tokens_list)}


@router.get("/config")
async def get_config() -> dict:
    """Effective server configuration. Secrets (API key, tokens) are not included."""
    s = get_settings()
    return {
        "provider": s.asr_provider,
        "available_providers": list_providers(),
        "base_url": s.asr_base_url,
        "model": s.asr_model,
        "language": s.asr_language,
        "timestamps": s.asr_timestamps,
        "hotwords": s.asr_hotwords_list,
        "prompt_hints": s.asr_prompt_hints,
        "split_strategy": s.split_strategy,
        "chunk_seconds": s.split_chunk_seconds,
        "overlap_seconds": s.split_overlap_seconds,
        "silence_noise_db": s.silence_noise_db,
        "silence_min_duration": s.silence_min_duration,
        "concurrency": s.asr_concurrency,
        "max_retries": s.asr_max_retries,
        "max_upload_bytes": s.max_upload_bytes,
        "api_key_set": bool(s.asr_api_key),
    }


def _silent_wav_bytes(duration: float = 1.0, sample_rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * int(sample_rate * duration))
    return buf.getvalue()


@router.post("/ping")
async def ping_upstream() -> dict:
    """Send a 1s silent WAV to the configured ASR backend and report status."""
    settings = get_settings()
    tmp = settings.temp_dir / f"ping_{uuid.uuid4().hex}.wav"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(_silent_wav_bytes())

    t0 = time.perf_counter()
    try:
        async with create_provider(settings) as provider:
            res = await provider.transcribe(tmp)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return {
            "ok": True,
            "elapsed_ms": round(elapsed_ms, 1),
            "provider": settings.asr_provider,
            "base_url": settings.asr_base_url,
            "model": settings.asr_model,
            "text_preview": res.text[:100],
            "got_words": bool(res.words),
        }
    except ASRError as e:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return {
            "ok": False,
            "elapsed_ms": round(elapsed_ms, 1),
            "provider": settings.asr_provider,
            "base_url": settings.asr_base_url,
            "model": settings.asr_model,
            "error": str(e),
        }
    finally:
        tmp.unlink(missing_ok=True)


@router.get("/task/{task_id}/segments/{segment_id}/raw")
async def get_segment_raw(
    task_id: str, segment_id: int,
    manager: TaskManager = Depends(get_manager),
) -> dict:
    """Return the raw upstream ASR payload for a single segment (debug aid)."""
    result = manager.get_result(task_id)
    if result is None:
        raise HTTPException(404, "task not found")
    for seg in result.segments:
        if seg.segment_id == segment_id:
            return {
                "segment_id": seg.segment_id,
                "elapsed_ms": seg.elapsed_ms,
                "words": [w.model_dump() for w in seg.words],
                "raw": seg.raw,
                "error": seg.error,
            }
    raise HTTPException(404, "segment not found")
