"""Time-aligned transcript ledger for online and reviewed ASR segments."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class SegmentOrigin(str, Enum):
    ONLINE = "online"
    REVIEW = "review"


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    id: str
    start_frame: int
    end_frame: int
    text: str
    origin: SegmentOrigin
    revision: int
    speaker: str | int | None = None
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class ReviewSegment:
    start_frame: int
    end_frame: int
    text: str
    speaker: str | int | None = None
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationUpdate:
    revision: int
    changed: bool
    removed_ids: tuple[str, ...] = ()
    added: tuple[TranscriptSegment, ...] = ()


class TranscriptLedger:
    """Maintain one ordered transcript whose review windows are authoritative."""

    def __init__(self) -> None:
        self._segments: list[TranscriptSegment] = []
        self._revision = 0
        self._next_online_id = 0

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def text(self) -> str:
        return "".join(segment.text for segment in self._ordered_segments()).strip()

    def snapshot(self) -> tuple[TranscriptSegment, ...]:
        return tuple(self._ordered_segments())

    def append_online(
        self,
        text: str,
        *,
        start_frame: int,
        end_frame: int,
        speaker: str | int | None = None,
        confidence: float | None = None,
    ) -> TranscriptSegment | None:
        cleaned = str(text).strip()
        if not cleaned:
            return None
        start, end = _validated_range(start_frame, end_frame)
        self._revision += 1
        self._next_online_id += 1
        segment = TranscriptSegment(
            id=f"online-{self._next_online_id}",
            start_frame=start,
            end_frame=end,
            text=cleaned,
            origin=SegmentOrigin.ONLINE,
            revision=self._revision,
            speaker=speaker,
            confidence=_optional_confidence(confidence),
        )
        self._segments.append(segment)
        return segment

    def reconcile_review(
        self,
        *,
        start_frame: int,
        end_frame: int,
        segments: Iterable[ReviewSegment] = (),
        fallback_text: str = "",
    ) -> ReconciliationUpdate:
        window_start, window_end = _validated_range(start_frame, end_frame)
        reviewed = _review_segments_within_window(
            segments,
            window_start=window_start,
            window_end=window_end,
        )
        cleaned_fallback = str(fallback_text).strip()
        if not reviewed and cleaned_fallback:
            reviewed = (
                ReviewSegment(
                    start_frame=window_start,
                    end_frame=window_end,
                    text=cleaned_fallback,
                ),
            )
        if not reviewed:
            return ReconciliationUpdate(revision=self._revision, changed=False)

        removed = tuple(
            segment
            for segment in self._segments
            if _ranges_overlap(
                segment.start_frame,
                segment.end_frame,
                window_start,
                window_end,
            )
        )
        removed_ids = {segment.id for segment in removed}
        self._segments = [segment for segment in self._segments if segment.id not in removed_ids]
        self._revision += 1
        added = tuple(
            TranscriptSegment(
                id=f"review-{self._revision}-{index}",
                start_frame=segment.start_frame,
                end_frame=segment.end_frame,
                text=segment.text,
                origin=SegmentOrigin.REVIEW,
                revision=self._revision,
                speaker=segment.speaker,
                confidence=_optional_confidence(segment.confidence),
            )
            for index, segment in enumerate(reviewed, start=1)
        )
        self._segments.extend(added)
        return ReconciliationUpdate(
            revision=self._revision,
            changed=True,
            removed_ids=tuple(segment.id for segment in removed),
            added=added,
        )

    def history_segments(self, *, sample_rate: int) -> tuple[dict[str, object], ...]:
        rate = int(sample_rate)
        if rate <= 0:
            raise ValueError("sample_rate must be greater than zero")
        output: list[dict[str, object]] = []
        for segment in self._ordered_segments():
            item: dict[str, object] = {
                "id": segment.id,
                "text": segment.text,
                "start": segment.start_frame / rate,
                "end": segment.end_frame / rate,
                "time_unit": "seconds",
                "origin": segment.origin.value,
                "revision": segment.revision,
            }
            if segment.speaker is not None:
                item["speaker"] = segment.speaker
            if segment.confidence is not None:
                item["confidence"] = segment.confidence
            output.append(item)
        return tuple(output)

    def _ordered_segments(self) -> list[TranscriptSegment]:
        return sorted(
            self._segments,
            key=lambda segment: (
                segment.start_frame,
                segment.end_frame,
                0 if segment.origin is SegmentOrigin.REVIEW else 1,
                segment.id,
            ),
        )


def _review_segments_within_window(
    segments: Iterable[ReviewSegment],
    *,
    window_start: int,
    window_end: int,
) -> tuple[ReviewSegment, ...]:
    output: list[ReviewSegment] = []
    for segment in segments:
        text = str(segment.text).strip()
        if not text:
            continue
        start = max(window_start, int(segment.start_frame))
        end = min(window_end, int(segment.end_frame))
        if start >= end:
            continue
        output.append(
            ReviewSegment(
                start_frame=start,
                end_frame=end,
                text=text,
                speaker=segment.speaker,
                confidence=_optional_confidence(segment.confidence),
            )
        )
    output.sort(key=lambda segment: (segment.start_frame, segment.end_frame))
    return tuple(output)


def _validated_range(start_frame: int, end_frame: int) -> tuple[int, int]:
    start = int(start_frame)
    end = int(end_frame)
    if start < 0 or end <= start:
        raise ValueError("segment frame range must satisfy 0 <= start_frame < end_frame")
    return start, end


def _ranges_overlap(left_start: int, left_end: int, right_start: int, right_end: int) -> bool:
    return left_start < right_end and right_start < left_end


def _optional_confidence(value: float | None) -> float | None:
    if value is None:
        return None
    return max(0.0, min(1.0, float(value)))
