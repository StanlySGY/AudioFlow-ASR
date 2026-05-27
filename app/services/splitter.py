from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.models.schemas import Segment
from app.services import ffmpeg_service as ff
from app.services.ffmpeg_service import SilenceRange

log = logging.getLogger(__name__)


def _fixed_ranges(duration: float, chunk: float, overlap: float = 0.0) -> list[tuple[float, float]]:
    if duration <= 0 or chunk <= 0:
        return []
    step = max(chunk - overlap, 0.1)
    ranges: list[tuple[float, float]] = []
    t = 0.0
    while t < duration:
        end = min(t + chunk, duration)
        ranges.append((t, end))
        if end >= duration:
            break
        t += step
    return ranges


def _silence_aware_ranges(
    duration: float, chunk: float, silences: list[SilenceRange]
) -> list[tuple[float, float]]:
    """Cut at the silence midpoint nearest to the target chunk boundary,
    falling back to fixed cut if no silence near the boundary."""
    if duration <= 0:
        return []
    ranges: list[tuple[float, float]] = []
    cursor = 0.0
    while cursor < duration - 0.05:
        target = cursor + chunk
        if target >= duration:
            ranges.append((cursor, duration))
            break
        # Acceptable cut window: target ± 25% chunk
        window = chunk * 0.25
        lo, hi = target - window, target + window
        cut = None
        best_dist = float("inf")
        for s in silences:
            if s.mid <= cursor + 0.5:
                continue
            if lo <= s.mid <= hi:
                d = abs(s.mid - target)
                if d < best_dist:
                    best_dist = d
                    cut = s.mid
        cut = cut if cut is not None else target
        cut = min(cut, duration)
        ranges.append((cursor, cut))
        cursor = cut
    return ranges


async def plan_segments(
    src: Path, duration: float, *, strategy: str,
    chunk: float, overlap: float, silences: list[SilenceRange],
) -> list[tuple[float, float]]:
    if strategy == "fixed":
        return _fixed_ranges(duration, chunk, overlap=0.0)
    if strategy == "overlap":
        return _fixed_ranges(duration, chunk, overlap=overlap)
    if strategy == "silence":
        return _silence_aware_ranges(duration, chunk, silences)
    raise ValueError(f"unknown split strategy: {strategy}")


async def split(
    src: Path, out_dir: Path, *, strategy: str,
    chunk: float, overlap: float,
    silence_noise_db: float, silence_min_duration: float,
) -> list[Segment]:
    duration = await ff.probe_duration(src)
    silences: list[SilenceRange] = []
    if strategy == "silence":
        silences = await ff.detect_silence(src, silence_noise_db, silence_min_duration)

    ranges = await plan_segments(
        src, duration, strategy=strategy, chunk=chunk, overlap=overlap, silences=silences,
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    tasks = []
    segments: list[Segment] = []
    for idx, (start, end) in enumerate(ranges, start=1):
        seg_path = out_dir / f"segment_{idx:04d}.wav"
        segments.append(Segment(segment_id=idx, start=start, end=end, file_path=seg_path))
        tasks.append(ff.slice_segment(src, seg_path, start, end))

    await asyncio.gather(*tasks)
    return segments
