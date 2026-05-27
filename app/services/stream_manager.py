from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from pathlib import Path
from typing import AsyncIterator

from app.config import Settings
from app.models.schemas import Segment, SegmentEvent, TaskInfo, TaskResult, TaskStatus
from app.services import splitter
from app.services.asr_client import ASRClient, ASRError
from app.services.ffmpeg_service import FFmpegError, normalize_to_wav, probe_duration
from app.services.merger import merge_segments

log = logging.getLogger(__name__)


class _Task:
    __slots__ = ("info", "result", "queue", "subscribers", "_done")

    def __init__(self, task_id: str) -> None:
        self.info = TaskInfo(task_id=task_id, status=TaskStatus.pending)
        self.result = TaskResult(task_id=task_id, status=TaskStatus.pending)
        self.queue: asyncio.Queue[SegmentEvent | None] = asyncio.Queue()
        self._done = asyncio.Event()

    @property
    def done(self) -> asyncio.Event:
        return self._done


class TaskManager:
    """Owns task lifecycle, segment-level event streaming, and result storage."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._tasks: dict[str, _Task] = {}
        self._lock = asyncio.Lock()

    # ---- public API ----

    async def submit(self, source_path: Path, original_name: str) -> str:
        task_id = uuid.uuid4().hex
        task = _Task(task_id)
        async with self._lock:
            self._tasks[task_id] = task

        asyncio.create_task(self._run(task_id, source_path, original_name))
        return task_id

    def get_info(self, task_id: str) -> TaskInfo | None:
        t = self._tasks.get(task_id)
        return t.info if t else None

    def get_result(self, task_id: str) -> TaskResult | None:
        t = self._tasks.get(task_id)
        return t.result if t else None

    async def stream(self, task_id: str) -> AsyncIterator[SegmentEvent]:
        task = self._tasks.get(task_id)
        if task is None:
            return
        while True:
            evt = await task.queue.get()
            if evt is None:
                return
            yield evt

    # ---- internal pipeline ----

    async def _run(self, task_id: str, source_path: Path, original_name: str) -> None:
        s = self._settings
        task = self._tasks[task_id]
        work_dir = s.temp_dir / task_id
        work_dir.mkdir(parents=True, exist_ok=True)

        try:
            task.info.status = TaskStatus.preprocessing
            normalized = work_dir / "input.wav"
            await normalize_to_wav(source_path, normalized)
            duration = await probe_duration(normalized)
            task.result.duration = duration

            task.info.status = TaskStatus.splitting
            segments = await splitter.split(
                normalized,
                work_dir / "segments",
                strategy=s.split_strategy,
                chunk=s.split_chunk_seconds,
                overlap=s.split_overlap_seconds,
                silence_noise_db=s.silence_noise_db,
                silence_min_duration=s.silence_min_duration,
            )
            task.info.total_segments = len(segments)
            task.result.segments = segments

            task.info.status = TaskStatus.transcribing
            await self._transcribe_all(task, segments)

            task.info.status = TaskStatus.merging
            task.result.text = merge_segments(segments)
            task.result.language = s.asr_language
            task.result.status = TaskStatus.done
            task.info.status = TaskStatus.done
            task.info.progress = 1.0

            # Persist result
            out_path = s.output_dir / f"{task_id}.json"
            out_path.write_text(task.result.model_dump_json(indent=2), encoding="utf-8")

        except (FFmpegError, ASRError, Exception) as e:  # noqa: BLE001
            log.exception("task %s failed", task_id)
            task.info.status = TaskStatus.failed
            task.info.error = str(e)
            task.result.status = TaskStatus.failed
            task.result.error = str(e)
        finally:
            await task.queue.put(None)
            task.done.set()
            try:
                # remove intermediate audio; keep result JSON in outputs/
                shutil.rmtree(work_dir, ignore_errors=True)
                if source_path.exists():
                    source_path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                log.warning("cleanup failed for %s", task_id, exc_info=True)

    async def _transcribe_all(self, task: _Task, segments: list[Segment]) -> None:
        s = self._settings
        sem = asyncio.Semaphore(max(1, s.asr_concurrency))

        async with ASRClient(
            base_url=s.asr_base_url,
            api_key=s.asr_api_key,
            model=s.asr_model,
            language=s.asr_language,
            timeout=s.asr_timeout,
            max_retries=s.asr_max_retries,
            retry_backoff=s.asr_retry_backoff,
        ) as client:

            async def worker(seg: Segment) -> None:
                async with sem:
                    try:
                        res = await client.transcribe(seg.file_path)
                        seg.text = res.text
                        seg.is_final = True
                    except ASRError as e:
                        seg.error = str(e)
                        seg.is_final = True
                        log.warning("segment %d failed: %s", seg.segment_id, e)
                    finally:
                        task.info.finished_segments += 1
                        if task.info.total_segments:
                            task.info.progress = task.info.finished_segments / task.info.total_segments
                        await task.queue.put(SegmentEvent(
                            task_id=task.info.task_id,
                            segment_id=seg.segment_id,
                            start=seg.start,
                            end=seg.end,
                            text=seg.text,
                            is_final=seg.is_final,
                            error=seg.error,
                        ))

            await asyncio.gather(*(worker(seg) for seg in segments))
