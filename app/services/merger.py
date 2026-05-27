from __future__ import annotations

from app.models.schemas import Segment


def _longest_common_substring(a: str, b: str, *, min_len: int = 4) -> tuple[int, int, int]:
    """Return (i, j, length) of longest common substring; length=0 if below threshold."""
    if not a or not b:
        return 0, 0, 0
    n, m = len(a), len(b)
    # Rolling DP to save memory.
    prev = [0] * (m + 1)
    best_len = 0
    best_i = 0
    best_j = 0
    for i in range(1, n + 1):
        curr = [0] * (m + 1)
        ai = a[i - 1]
        for j in range(1, m + 1):
            if ai == b[j - 1]:
                curr[j] = prev[j - 1] + 1
                if curr[j] > best_len:
                    best_len = curr[j]
                    best_i = i - curr[j]
                    best_j = j - curr[j]
        prev = curr
    if best_len < min_len:
        return 0, 0, 0
    return best_i, best_j, best_len


def _dedupe_join(prev: str, nxt: str, *, min_overlap: int = 4) -> str:
    """Join two consecutive transcripts removing the longest overlapping span
    that ends prev and starts nxt (or anywhere near the boundary)."""
    if not prev:
        return nxt
    if not nxt:
        return prev
    # Compare prev's tail with nxt's head, sized to the shorter side.
    tail_window = prev[-200:]
    head_window = nxt[:200]
    i, j, ln = _longest_common_substring(tail_window, head_window, min_len=min_overlap)
    if ln == 0:
        return prev + nxt
    # Require the overlap to actually touch the boundary, otherwise just concat.
    touches_prev_end = (i + ln) >= len(tail_window) - 1
    touches_nxt_start = j <= 1
    if touches_prev_end and touches_nxt_start:
        return prev + nxt[j + ln:]
    return prev + nxt


def merge_segments(segments: list[Segment]) -> str:
    """Merge segment texts in order, removing overlap-induced repetition."""
    ordered = sorted(segments, key=lambda s: s.segment_id)
    out = ""
    for seg in ordered:
        if seg.error or not seg.text:
            continue
        out = _dedupe_join(out, seg.text)
    return out.strip()
