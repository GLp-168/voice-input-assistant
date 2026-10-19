from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3

import pytest

from voice_input.transcript_history import TranscriptHistoryStore, build_session_record


def test_build_session_record_estimates_paragraph_timestamps_without_speakers() -> None:
    record = build_session_record(
        source="microphone",
        text="aa\n\nbbbbbb",
        duration=8.0,
        session_id="session-1",
        created_at="2026-07-13T10:00:00Z",
    )

    assert set(record) == {"id", "created_at", "source", "text", "duration", "paragraphs", "segments"}
    assert record["paragraphs"] == ["aa", "bbbbbb"]
    assert record["segments"] == [
        {"text": "aa", "start": 0.0, "end": 2.0, "timing": "estimated", "time_unit": "seconds"},
        {"text": "bbbbbb", "start": 2.0, "end": 8.0, "timing": "estimated", "time_unit": "seconds"},
    ]
    assert all("speaker" not in segment and "spk" not in segment for segment in record["segments"])


def test_build_session_record_normalizes_funasr_milliseconds_and_preserves_speaker_zero() -> None:
    source_segments = [
        {"text": "first", "start": 123, "end": 456, "spk": 0},
        {
            "text": "second",
            "start": 456.5,
            "end": 900.25,
            "speaker": "A",
            "timestamp": [[456.5, 700], [700, 900.25]],
        },
    ]

    record = build_session_record(
        source={"type": "file", "name": "meeting.wav"},
        text="first second",
        duration=1.0,
        paragraphs=["first second"],
        segments=source_segments,
    )

    assert record["segments"][0] == {
        "text": "first",
        "start": pytest.approx(0.123),
        "end": pytest.approx(0.456),
        "spk": 0,
        "time_unit": "seconds",
    }
    assert record["segments"][1]["timestamp"] == [
        [pytest.approx(0.4565), pytest.approx(0.7)],
        [pytest.approx(0.7), pytest.approx(0.90025)],
    ]
    assert record["segments"][1]["speaker"] == "A"
    assert record["segments"] is not source_segments


def test_history_store_uses_structured_sqlite_tables_and_keeps_only_recent_sessions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "nested" / "history.sqlite3"
    store = TranscriptHistoryStore(path, max_sessions=2)
    for index in range(3):
        store.add_session(
            source="microphone",
            text=f"session {index}",
            duration=float(index),
            segments=[{"text": str(index), "start": 0.0, "end": float(index), "speaker": 0}],
            session_id=str(index),
            created_at=f"2026-07-13T10:00:0{index}Z",
        )

    assert [session["id"] for session in store.list_sessions()] == ["2", "1"]
    assert path.read_bytes().startswith(b"SQLite format 3\x00")
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 2
        speaker_json = connection.execute(
            "SELECT speaker_json FROM segments WHERE session_id = '2'"
        ).fetchone()[0]
    assert json.loads(speaker_json) == 0


def test_history_store_serializes_writers_across_store_instances(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    stores = [TranscriptHistoryStore(path, max_sessions=12) for _ in range(4)]

    def save(index: int) -> None:
        stores[index % len(stores)].add_session(
            source="microphone",
            text=f"session {index}",
            duration=1.0,
            session_id=str(index),
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(save, range(40)))

    sessions = stores[0].list_sessions()
    assert len(sessions) == 12
    assert len({session["id"] for session in sessions}) == 12
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 12


def test_history_store_returns_lightweight_summaries_and_lazy_detail(tmp_path: Path) -> None:
    store = TranscriptHistoryStore(tmp_path / "history.sqlite3")
    long_text = "first line\n\n" + "x" * 300
    saved = store.add_session(
        source={"kind": "microphone"},
        text=long_text,
        duration=12.0,
        segments=[{"text": "first line", "start": 0.0, "end": 2.0, "speaker": 0}],
        session_id="session-1",
    )

    summaries = store.list_session_summaries()
    assert summaries == [
        {
            "id": "session-1",
            "created_at": saved["created_at"],
            "source": {"kind": "microphone"},
            "duration": 12.0,
            "text_preview": ("first line " + "x" * 300)[:160],
            "paragraph_count": 2,
            "segment_count": 1,
        }
    ]
    assert "text" not in summaries[0]
    assert "segments" not in summaries[0]
    detail = store.get_session("session-1")
    assert detail is not None
    assert detail["text"] == long_text
    assert detail["segments"][0]["speaker"] == 0


def test_history_store_migrates_legacy_json_and_preserves_backup(tmp_path: Path) -> None:
    path = tmp_path / "transcript_history.json"
    legacy_record = {
        "id": "legacy-1",
        "created_at": "2026-07-13T10:00:00Z",
        "source": "microphone",
        "text": "legacy transcript",
        "duration": 1.0,
        "paragraphs": ["legacy transcript"],
        "segments": [{"text": "legacy transcript", "start": 0, "end": 850, "spk": 0}],
    }
    legacy_payload = {"version": 1, "sessions": [legacy_record]}
    original_bytes = json.dumps(legacy_payload, ensure_ascii=False).encode("utf-8")
    path.write_bytes(original_bytes)

    store = TranscriptHistoryStore(path)

    detail = store.get_session("legacy-1")
    assert detail is not None
    assert detail["segments"][0]["end"] == pytest.approx(0.85)
    assert detail["segments"][0]["spk"] == 0
    assert path.read_bytes().startswith(b"SQLite format 3\x00")
    backups = list(tmp_path.glob("transcript_history.json.legacy-json-*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original_bytes


def test_history_store_archives_malformed_legacy_file_before_saving(tmp_path: Path) -> None:
    path = tmp_path / "transcript_history.json"
    malformed = b'{"sessions": ['
    path.write_bytes(malformed)

    store = TranscriptHistoryStore(path)
    store.add_session(source="microphone", text="new transcript", session_id="new")

    assert store.get_session("new") is not None
    backups = list(tmp_path.glob("transcript_history.json.corrupt-*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == malformed
    assert path.read_bytes().startswith(b"SQLite format 3\x00")


def test_history_store_recovers_runtime_database_corruption_without_overwriting_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.sqlite3"
    store = TranscriptHistoryStore(path)
    store.add_session(source="microphone", text="old", session_id="old")
    malformed_database = b"SQLite format 3\x00" + b"broken database contents"
    path.write_bytes(malformed_database)

    assert store.list_sessions() == []
    store.add_session(source="microphone", text="new", session_id="new")

    assert store.get_session("new") is not None
    backups = list(tmp_path.glob("history.sqlite3.corrupt-*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == malformed_database


def test_history_store_replaces_session_transactionally_and_cascades_segments(tmp_path: Path) -> None:
    store = TranscriptHistoryStore(tmp_path / "history.sqlite3")
    store.add_session(
        source="microphone",
        text="old",
        segments=[{"text": "old", "start": 0.0, "end": 1.0}],
        session_id="same",
    )
    store.add_session(
        source="microphone",
        text="new",
        segments=[
            {"text": "new one", "start": 0.0, "end": 1.0},
            {"text": "new two", "start": 1.0, "end": 2.0},
        ],
        session_id="same",
    )

    sessions = store.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["text"] == "new"
    assert [segment["text"] for segment in sessions[0]["segments"]] == ["new one", "new two"]


def test_history_store_validates_limits_and_clear(tmp_path: Path) -> None:
    store = TranscriptHistoryStore(tmp_path / "history.sqlite3")
    store.add_session(source="microphone", text="one", session_id="one")

    assert store.list_sessions(limit=0) == []
    with pytest.raises(ValueError, match="must not be negative"):
        store.list_session_summaries(limit=-1)

    store.clear()
    assert store.list_sessions() == []
    assert store.get_session("one") is None


def test_workspace_draft_is_persisted_and_cleared(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    store = TranscriptHistoryStore(path)

    saved = store.save_workspace_draft(text="人工修订稿", session_id="session-1", revision=7)

    assert saved["revision"] == 7
    assert TranscriptHistoryStore(path).load_workspace_draft() == saved
    store.clear_workspace_draft()
    assert store.load_workspace_draft() is None


def test_update_session_text_marks_original_segments_stale(tmp_path: Path) -> None:
    store = TranscriptHistoryStore(tmp_path / "history.sqlite3")
    store.add_session(
        source={"kind": "microphone"},
        text="原稿",
        segments=[{"text": "原稿", "start": 0.0, "end": 1.0}],
        session_id="session-1",
    )

    updated = store.update_session_text("session-1", "人工修订稿")

    assert updated is not None
    assert updated["text"] == "人工修订稿"
    assert updated["source"]["alignment_state"] == "stale"
    assert updated["segments"][0]["alignment_state"] == "stale"
