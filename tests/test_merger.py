from pathlib import Path

from app.models.schemas import Segment
from app.services.merger import _dedupe_join, merge_segments


def _seg(i: int, text: str, error: str | None = None) -> Segment:
    return Segment(segment_id=i, start=0.0, end=1.0, file_path=Path(f"/tmp/{i}.wav"), text=text, is_final=True, error=error)


def test_dedupe_join_removes_boundary_overlap():
    a = "今天上海天气很好，我们准备"
    b = "我们准备去吃火锅"
    out = _dedupe_join(a, b)
    assert out == "今天上海天气很好，我们准备去吃火锅"


def test_dedupe_join_keeps_separate_when_no_overlap():
    a = "你好"
    b = "再见"
    assert _dedupe_join(a, b) == "你好再见"


def test_dedupe_join_handles_empty():
    assert _dedupe_join("", "abc") == "abc"
    assert _dedupe_join("abc", "") == "abc"


def test_dedupe_join_does_not_dedupe_short_match():
    # 2-char match below min_overlap=4 should not be treated as overlap.
    a = "abc的"
    b = "的xyz"
    out = _dedupe_join(a, b)
    assert out == "abc的的xyz"


def test_merge_segments_sorts_and_skips_errors():
    segs = [
        _seg(2, "我们准备去吃火锅"),
        _seg(1, "今天上海天气很好，我们准备"),
        _seg(3, "", error="timeout"),
        _seg(4, " 锅很辣"),
    ]
    out = merge_segments(segs)
    assert "今天上海天气很好，我们准备去吃火锅" in out
    assert "锅很辣" in out
