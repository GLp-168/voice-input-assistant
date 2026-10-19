from __future__ import annotations

import pytest

from voice_input.transcript_reconciliation import (
    ReviewSegment,
    SegmentOrigin,
    TranscriptLedger,
)


def test_online_segments_are_rendered_in_time_order() -> None:
    ledger = TranscriptLedger()
    ledger.append_online("后", start_frame=20, end_frame=30)
    ledger.append_online("前", start_frame=0, end_frame=10)

    assert ledger.text == "前后"
    assert [segment.start_frame for segment in ledger.snapshot()] == [0, 20]


def test_review_replaces_matching_time_window_instead_of_appending() -> None:
    ledger = TranscriptLedger()
    first = ledger.append_online("项目推近会", start_frame=0, end_frame=100)
    ledger.append_online("会议纪要", start_frame=100, end_frame=200)

    update = ledger.reconcile_review(
        start_frame=0,
        end_frame=100,
        fallback_text="项目推进会",
    )

    assert first is not None
    assert update.removed_ids == (first.id,)
    assert ledger.text == "项目推进会会议纪要"
    assert [segment.origin for segment in ledger.snapshot()] == [
        SegmentOrigin.REVIEW,
        SegmentOrigin.ONLINE,
    ]


def test_review_preserves_adjacent_segments() -> None:
    ledger = TranscriptLedger()
    ledger.append_online("甲", start_frame=0, end_frame=100)
    ledger.append_online("乙", start_frame=100, end_frame=200)
    ledger.append_online("丙", start_frame=200, end_frame=300)

    ledger.reconcile_review(start_frame=100, end_frame=200, fallback_text="乙复核")

    assert ledger.text == "甲乙复核丙"


def test_repeated_review_of_same_window_is_idempotent_for_text() -> None:
    ledger = TranscriptLedger()
    ledger.append_online("初稿", start_frame=0, end_frame=100)
    first = ledger.reconcile_review(start_frame=0, end_frame=100, fallback_text="一审")
    second = ledger.reconcile_review(start_frame=0, end_frame=100, fallback_text="二审")

    assert ledger.text == "二审"
    assert second.removed_ids == tuple(segment.id for segment in first.added)
    assert len(ledger.snapshot()) == 1


def test_empty_review_keeps_online_fallback_unchanged() -> None:
    ledger = TranscriptLedger()
    ledger.append_online("实时结果", start_frame=0, end_frame=100)
    revision = ledger.revision

    update = ledger.reconcile_review(start_frame=0, end_frame=100)

    assert update.changed is False
    assert ledger.revision == revision
    assert ledger.text == "实时结果"


def test_review_segments_are_clamped_and_sorted_within_window() -> None:
    ledger = TranscriptLedger()
    ledger.append_online("旧", start_frame=100, end_frame=300)

    ledger.reconcile_review(
        start_frame=100,
        end_frame=300,
        segments=(
            ReviewSegment(250, 350, "后"),
            ReviewSegment(50, 150, "前"),
        ),
    )

    segments = ledger.snapshot()
    assert [(item.start_frame, item.end_frame, item.text) for item in segments] == [
        (100, 150, "前"),
        (250, 300, "后"),
    ]
    assert ledger.text == "前后"


def test_overlapping_online_segment_is_removed_by_authoritative_review_window() -> None:
    ledger = TranscriptLedger()
    online = ledger.append_online("跨界实时文本", start_frame=50, end_frame=150)

    update = ledger.reconcile_review(start_frame=100, end_frame=200, fallback_text="复核")

    assert online is not None
    assert update.removed_ids == (online.id,)
    assert ledger.text == "复核"


def test_history_segments_convert_frames_and_preserve_speaker() -> None:
    ledger = TranscriptLedger()
    ledger.reconcile_review(
        start_frame=1600,
        end_frame=3200,
        segments=(ReviewSegment(1600, 3200, "内容", speaker="S1", confidence=1.5),),
    )

    assert ledger.history_segments(sample_rate=1600) == (
        {
            "id": "review-1-1",
            "text": "内容",
            "start": 1.0,
            "end": 2.0,
            "time_unit": "seconds",
            "origin": "review",
            "revision": 1,
            "speaker": "S1",
            "confidence": 1.0,
        },
    )


@pytest.mark.parametrize(("start", "end"), [(-1, 2), (2, 2), (3, 2)])
def test_invalid_frame_ranges_are_rejected(start: int, end: int) -> None:
    ledger = TranscriptLedger()

    with pytest.raises(ValueError, match="frame range"):
        ledger.append_online("文本", start_frame=start, end_frame=end)

    with pytest.raises(ValueError, match="frame range"):
        ledger.reconcile_review(start_frame=start, end_frame=end, fallback_text="文本")


def test_history_segments_requires_positive_sample_rate() -> None:
    ledger = TranscriptLedger()
    ledger.append_online("文本", start_frame=0, end_frame=1)

    with pytest.raises(ValueError, match="sample_rate"):
        ledger.history_segments(sample_rate=0)
