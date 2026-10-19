"""Persistent transcript history backed by SQLite."""

from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Mapping, Sequence
from uuid import uuid4

from .settings import default_config_dir


STORE_VERSION = 3
DEFAULT_MAX_SESSIONS = 50
_SQLITE_HEADER = b"SQLite format 3\x00"
_SUMMARY_PREVIEW_LENGTH = 160


def default_transcript_history_path() -> Path:
    """Return the default user-specific transcript history database path."""

    return default_config_dir() / "transcript_history.sqlite3"


def normalize_paragraphs(text: str, paragraphs: Sequence[str] | None = None) -> list[str]:
    """Return non-empty transcript paragraphs in display order."""

    if paragraphs is not None:
        candidates = (str(paragraph).strip() for paragraph in paragraphs)
    else:
        stripped = str(text).strip()
        if not stripped:
            return []
        candidates = (part.strip() for part in re.split(r"\r?\n\s*\r?\n", stripped))
    return [paragraph for paragraph in candidates if paragraph]


def normalize_segments(
    segments: Sequence[Mapping[str, Any]] | None,
    *,
    paragraphs: Sequence[str],
    duration: float | None,
) -> list[dict[str, Any]]:
    """Return ASR segments with all timestamp values normalized to seconds.

    FunASR emits millisecond timestamps. Explicit ``time_unit`` metadata wins;
    otherwise its ``timestamp``/``spk`` fields and duration overruns are used to
    recognize that shape. Ambiguous integrations should set ``time_unit``.
    """

    raw_segments = [deepcopy(dict(segment)) for segment in (segments or ()) if isinstance(segment, Mapping)]
    if raw_segments:
        default_scale = _infer_timestamp_scale(raw_segments, duration)
        return [_normalize_segment_timestamps(segment, default_scale) for segment in raw_segments]
    if not paragraphs:
        return []

    total_duration = max(0.0, float(duration or 0.0))
    weights = [max(1, len(re.sub(r"\s+", "", paragraph))) for paragraph in paragraphs]
    total_weight = sum(weights)
    estimated: list[dict[str, Any]] = []
    elapsed_weight = 0
    for index, (paragraph, weight) in enumerate(zip(paragraphs, weights)):
        start = total_duration * elapsed_weight / total_weight
        elapsed_weight += weight
        end = total_duration if index == len(paragraphs) - 1 else total_duration * elapsed_weight / total_weight
        estimated.append(
            {
                "text": paragraph,
                "start": start,
                "end": end,
                "timing": "estimated",
                "time_unit": "seconds",
            }
        )
    return estimated


def build_session_record(
    *,
    source: Any,
    text: str,
    duration: float | None = None,
    paragraphs: Sequence[str] | None = None,
    segments: Sequence[Mapping[str, Any]] | None = None,
    session_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build a JSON-compatible transcript session record."""

    normalized_paragraphs = normalize_paragraphs(text, paragraphs)
    normalized_duration = None if duration is None else max(0.0, float(duration))
    return {
        "id": str(session_id or uuid4()),
        "created_at": created_at or _utc_now(),
        "source": deepcopy(source),
        "text": str(text),
        "duration": normalized_duration,
        "paragraphs": normalized_paragraphs,
        "segments": normalize_segments(
            segments,
            paragraphs=normalized_paragraphs,
            duration=normalized_duration,
        ),
    }


class TranscriptHistoryStore:
    """Thread- and process-safe SQLite store for recent transcript sessions.

    ``list_sessions`` remains the compatibility API and returns full records.
    New callers should use ``list_session_summaries`` and fetch a selected
    record through ``get_session`` to avoid transferring every transcript.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
    ) -> None:
        if max_sessions <= 0:
            raise ValueError("max_sessions must be greater than zero")
        self.path = Path(path) if path is not None else default_transcript_history_path()
        self.max_sessions = int(max_sessions)
        self._lock = threading.RLock()
        with self._lock:
            self._prepare_store_unlocked()

    def add_session(
        self,
        *,
        source: Any,
        text: str,
        duration: float | None = None,
        paragraphs: Sequence[str] | None = None,
        segments: Sequence[Mapping[str, Any]] | None = None,
        session_id: str | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        """Create, persist, and return a new transcript session."""

        record = build_session_record(
            source=source,
            text=text,
            duration=duration,
            paragraphs=paragraphs,
            segments=segments,
            session_id=session_id,
            created_at=created_at,
        )
        return self.save_session(record)

    def save_session(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Persist a pre-built session, replacing a matching id if present."""

        normalized = _validate_session_record(record)
        with self._lock:
            self._run_with_corruption_recovery_unlocked(
                lambda connection: self._save_session_unlocked(connection, normalized)
            )
        return deepcopy(normalized)

    def list_session_summaries(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Return lightweight recent-session metadata, newest first."""

        normalized_limit = self._normalize_limit(limit)
        with self._lock:
            summaries = self._run_with_corruption_recovery_unlocked(
                lambda connection: self._read_summaries_unlocked(connection, normalized_limit)
            )
        return deepcopy(summaries)

    def list_sessions(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Return full recent sessions, newest first (compatibility API)."""

        normalized_limit = self._normalize_limit(limit)
        with self._lock:
            sessions = self._run_with_corruption_recovery_unlocked(
                lambda connection: self._read_sessions_unlocked(connection, normalized_limit)
            )
        return deepcopy(sessions)

    def load(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Compatibility alias for :meth:`list_sessions`."""

        return self.list_sessions(limit=limit)

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        """Return full session detail by id, if it is still retained."""

        with self._lock:
            session = self._run_with_corruption_recovery_unlocked(
                lambda connection: self._read_session_unlocked(connection, str(session_id))
            )
        return deepcopy(session)

    def update_session_text(self, session_id: str, text: str) -> dict[str, Any] | None:
        """Persist an edited transcript while retaining its original timing evidence."""

        existing = self.get_session(session_id)
        if existing is None:
            return None
        source = existing.get("source")
        if isinstance(source, Mapping):
            source = {**source, "edited": True, "alignment_state": "stale"}
        else:
            source = {"kind": source, "edited": True, "alignment_state": "stale"}
        edited = {
            **existing,
            "source": source,
            "text": str(text),
            "paragraphs": normalize_paragraphs(text),
            "segments": [
                {**segment, "alignment_state": "stale"}
                for segment in existing.get("segments", [])
                if isinstance(segment, Mapping)
            ],
        }
        return self.save_session(edited)

    def save_workspace_draft(self, *, text: str, session_id: str = "", revision: int = 0) -> dict[str, Any]:
        """Atomically persist the current workbench document."""

        draft = {
            "text": str(text),
            "session_id": str(session_id),
            "revision": max(0, int(revision)),
            "updated_at": _utc_now(),
        }
        with self._lock:
            self._run_with_corruption_recovery_unlocked(
                lambda connection: self._save_workspace_draft_unlocked(connection, draft)
            )
        return deepcopy(draft)

    def load_workspace_draft(self) -> dict[str, Any] | None:
        """Return the last workbench document, if one was saved."""

        with self._lock:
            draft = self._run_with_corruption_recovery_unlocked(self._read_workspace_draft_unlocked)
        return deepcopy(draft)

    def clear_workspace_draft(self) -> None:
        with self._lock:
            self._run_with_corruption_recovery_unlocked(
                self._clear_workspace_draft_unlocked
            )

    def clear(self) -> None:
        """Remove all retained sessions while keeping a valid database."""

        with self._lock:
            self._run_with_corruption_recovery_unlocked(self._clear_unlocked)

    def _normalize_limit(self, limit: int | None) -> int:
        if limit is not None and limit < 0:
            raise ValueError("limit must not be negative")
        return min(self.max_sessions, self.max_sessions if limit is None else int(limit))

    def _prepare_store_unlocked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        legacy_sessions: list[dict[str, Any]] | None = None
        if self.path.exists() and not _is_sqlite_database(self.path):
            legacy_sessions = self._read_legacy_json_unlocked()
            backup_kind = "legacy-json" if legacy_sessions is not None else "corrupt"
            self._archive_store_files_unlocked(backup_kind)

        try:
            with closing(self._connect_unlocked()) as connection:
                self._initialize_schema_unlocked(connection)
                if legacy_sessions is not None:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        for record in reversed(legacy_sessions[: self.max_sessions]):
                            self._save_session_rows_unlocked(connection, record, prune=False)
                        self._prune_unlocked(connection)
                    except Exception:
                        connection.rollback()
                        raise
                    else:
                        connection.commit()
        except sqlite3.DatabaseError as error:
            if not _is_corruption_error(error):
                raise
            self._archive_store_files_unlocked("corrupt")
            with closing(self._connect_unlocked()) as connection:
                self._initialize_schema_unlocked(connection)

    def _read_legacy_json_unlocked(self) -> list[dict[str, Any]] | None:
        try:
            with self.path.open("r", encoding="utf-8") as history_file:
                payload = json.load(history_file)
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None

        raw_sessions = payload.get("sessions", []) if isinstance(payload, Mapping) else payload
        if not isinstance(raw_sessions, list):
            return None
        sessions: list[dict[str, Any]] = []
        for record in raw_sessions:
            try:
                sessions.append(_validate_session_record(record))
            except (TypeError, ValueError):
                continue
        return sessions

    def _connect_unlocked(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize_schema_unlocked(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            PRAGMA journal_mode = WAL;
            PRAGMA synchronous = FULL;
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                source_json TEXT NOT NULL,
                text TEXT NOT NULL,
                duration REAL,
                paragraphs_json TEXT NOT NULL,
                saved_order INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS segments (
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                text TEXT NOT NULL,
                start_seconds REAL,
                end_seconds REAL,
                speaker_json TEXT,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (session_id, position)
            );
            CREATE INDEX IF NOT EXISTS sessions_recent_idx
                ON sessions(saved_order DESC, id);
            CREATE INDEX IF NOT EXISTS segments_session_idx
                ON segments(session_id, position);
            CREATE TABLE IF NOT EXISTS workspace_draft (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                text TEXT NOT NULL,
                session_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        connection.execute(f"PRAGMA user_version = {STORE_VERSION}")

    @staticmethod
    def _save_workspace_draft_unlocked(connection: sqlite3.Connection, draft: Mapping[str, Any]) -> None:
        connection.execute(
            """
            INSERT INTO workspace_draft (singleton, text, session_id, revision, updated_at)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(singleton) DO UPDATE SET
                text = excluded.text,
                session_id = excluded.session_id,
                revision = excluded.revision,
                updated_at = excluded.updated_at
            """,
            (draft["text"], draft["session_id"], draft["revision"], draft["updated_at"]),
        )
        connection.commit()

    @staticmethod
    def _clear_workspace_draft_unlocked(connection: sqlite3.Connection) -> None:
        connection.execute("DELETE FROM workspace_draft")
        connection.commit()

    @staticmethod
    def _read_workspace_draft_unlocked(connection: sqlite3.Connection) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT text, session_id, revision, updated_at FROM workspace_draft WHERE singleton = 1"
        ).fetchone()
        if row is None:
            return None
        return {
            "text": str(row["text"]),
            "session_id": str(row["session_id"]),
            "revision": max(0, int(row["revision"])),
            "updated_at": str(row["updated_at"]),
        }

    def _run_with_corruption_recovery_unlocked(self, operation: Any) -> Any:
        try:
            with closing(self._connect_unlocked()) as connection:
                return operation(connection)
        except sqlite3.DatabaseError as error:
            if not _is_corruption_error(error):
                raise
            self._archive_store_files_unlocked("corrupt")
            with closing(self._connect_unlocked()) as connection:
                self._initialize_schema_unlocked(connection)
                return operation(connection)

    def _save_session_unlocked(
        self,
        connection: sqlite3.Connection,
        record: Mapping[str, Any],
    ) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            self._save_session_rows_unlocked(connection, record, prune=True)
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()

    def _save_session_rows_unlocked(
        self,
        connection: sqlite3.Connection,
        record: Mapping[str, Any],
        *,
        prune: bool,
    ) -> None:
        next_order = int(
            connection.execute("SELECT COALESCE(MAX(saved_order), 0) + 1 FROM sessions").fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO sessions (
                id, created_at, source_json, text, duration, paragraphs_json, saved_order
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                created_at = excluded.created_at,
                source_json = excluded.source_json,
                text = excluded.text,
                duration = excluded.duration,
                paragraphs_json = excluded.paragraphs_json,
                saved_order = excluded.saved_order
            """,
            (
                record["id"],
                record["created_at"],
                _json_dumps(record["source"]),
                record["text"],
                record["duration"],
                _json_dumps(record["paragraphs"]),
                next_order,
            ),
        )
        connection.execute("DELETE FROM segments WHERE session_id = ?", (record["id"],))
        for position, segment in enumerate(record["segments"]):
            speaker = segment.get("speaker", segment.get("spk"))
            connection.execute(
                """
                INSERT INTO segments (
                    session_id, position, text, start_seconds, end_seconds,
                    speaker_json, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["id"],
                    position,
                    str(segment.get("text", "")),
                    _optional_float(segment.get("start")),
                    _optional_float(segment.get("end")),
                    None if speaker is None else _json_dumps(speaker),
                    _json_dumps(segment),
                ),
            )
        if prune:
            self._prune_unlocked(connection)

    def _prune_unlocked(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            DELETE FROM sessions
            WHERE id IN (
                SELECT id FROM sessions
                ORDER BY saved_order DESC, id
                LIMIT -1 OFFSET ?
            )
            """,
            (self.max_sessions,),
        )

    def _read_summaries_unlocked(
        self,
        connection: sqlite3.Connection,
        limit: int,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT s.id, s.created_at, s.source_json, s.duration, s.text,
                   s.paragraphs_json, COUNT(g.position) AS segment_count
            FROM sessions AS s
            LEFT JOIN segments AS g ON g.session_id = s.id
            GROUP BY s.id
            ORDER BY s.saved_order DESC, s.id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        summaries: list[dict[str, Any]] = []
        for row in rows:
            paragraphs = _json_loads(row["paragraphs_json"])
            summaries.append(
                {
                    "id": row["id"],
                    "created_at": row["created_at"],
                    "source": _json_loads(row["source_json"]),
                    "duration": row["duration"],
                    "text_preview": _text_preview(row["text"]),
                    "paragraph_count": len(paragraphs) if isinstance(paragraphs, list) else 0,
                    "segment_count": int(row["segment_count"]),
                }
            )
        return summaries

    def _read_sessions_unlocked(
        self,
        connection: sqlite3.Connection,
        limit: int,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT id, created_at, source_json, text, duration, paragraphs_json
            FROM sessions
            ORDER BY saved_order DESC, id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return self._records_from_rows_unlocked(connection, rows)

    def _read_session_unlocked(
        self,
        connection: sqlite3.Connection,
        session_id: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT id, created_at, source_json, text, duration, paragraphs_json
            FROM sessions WHERE id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return self._records_from_rows_unlocked(connection, [row])[0]

    def _records_from_rows_unlocked(
        self,
        connection: sqlite3.Connection,
        rows: Sequence[sqlite3.Row],
    ) -> list[dict[str, Any]]:
        if not rows:
            return []
        ids = [str(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in ids)
        segment_rows = connection.execute(
            f"""
            SELECT session_id, payload_json FROM segments
            WHERE session_id IN ({placeholders})
            ORDER BY session_id, position
            """,
            ids,
        ).fetchall()
        segments_by_id: dict[str, list[dict[str, Any]]] = {session_id: [] for session_id in ids}
        for segment_row in segment_rows:
            payload = _json_loads(segment_row["payload_json"])
            if isinstance(payload, Mapping):
                segments_by_id[str(segment_row["session_id"])].append(dict(payload))
        return [
            {
                "id": str(row["id"]),
                "created_at": str(row["created_at"]),
                "source": _json_loads(row["source_json"]),
                "text": str(row["text"]),
                "duration": row["duration"],
                "paragraphs": list(_json_loads(row["paragraphs_json"])),
                "segments": segments_by_id[str(row["id"])],
            }
            for row in rows
        ]

    @staticmethod
    def _clear_unlocked(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("DELETE FROM sessions")
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()

    def _archive_store_files_unlocked(self, kind: str) -> Path | None:
        if not self.path.exists():
            return None
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = self.path.with_name(f"{self.path.name}.{kind}-{timestamp}.bak")
        os.replace(self.path, backup)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{self.path}{suffix}")
            if sidecar.exists():
                os.replace(sidecar, Path(f"{backup}{suffix}"))
        return backup


def _validate_session_record(record: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise TypeError("session record must be a mapping")
    required = {"id", "created_at", "source", "text", "duration", "paragraphs", "segments"}
    missing = required.difference(record)
    if missing:
        raise ValueError(f"session record is missing fields: {', '.join(sorted(missing))}")
    if not isinstance(record["paragraphs"], list) or not isinstance(record["segments"], list):
        raise ValueError("session paragraphs and segments must be lists")
    normalized = {key: deepcopy(record[key]) for key in required}
    normalized["id"] = str(normalized["id"])
    normalized["created_at"] = str(normalized["created_at"])
    normalized["text"] = str(normalized["text"])
    if normalized["duration"] is not None:
        normalized["duration"] = max(0.0, float(normalized["duration"]))
    normalized["paragraphs"] = [str(paragraph) for paragraph in normalized["paragraphs"]]
    normalized["segments"] = normalize_segments(
        normalized["segments"],
        paragraphs=normalized["paragraphs"],
        duration=normalized["duration"],
    )
    _json_dumps(normalized["source"])
    return normalized


def _infer_timestamp_scale(segments: Sequence[Mapping[str, Any]], duration: float | None) -> float:
    explicit_units = {
        str(segment.get("time_unit", "")).strip().lower()
        for segment in segments
        if segment.get("time_unit") is not None
    }
    if explicit_units.intersection({"ms", "millisecond", "milliseconds"}):
        return 0.001
    if explicit_units.intersection({"s", "sec", "second", "seconds"}):
        return 1.0
    if any(isinstance(segment.get("timestamp"), (list, tuple)) or "spk" in segment for segment in segments):
        return 0.001

    bounds = [
        value
        for segment in segments
        for value in (segment.get("start"), segment.get("end"))
        if _is_finite_number(value)
    ]
    if duration is not None and bounds:
        largest = max(float(value) for value in bounds)
        if largest > max(float(duration) * 1.5, float(duration) + 5.0):
            return 0.001
    return 1.0


def _normalize_segment_timestamps(segment: Mapping[str, Any], default_scale: float) -> dict[str, Any]:
    normalized = deepcopy(dict(segment))
    unit = str(normalized.get("time_unit", "")).strip().lower()
    scale = 0.001 if unit in {"ms", "millisecond", "milliseconds"} else default_scale
    if unit in {"s", "sec", "second", "seconds"}:
        scale = 1.0

    has_timing = False
    for key in ("start", "end"):
        if _is_finite_number(normalized.get(key)):
            normalized[key] = max(0.0, float(normalized[key]) * scale)
            has_timing = True
        elif key in normalized:
            normalized.pop(key)

    timestamp = normalized.get("timestamp")
    if isinstance(timestamp, (list, tuple)):
        normalized_timestamp: list[list[float]] = []
        for pair in timestamp:
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            if not _is_finite_number(pair[0]) or not _is_finite_number(pair[1]):
                continue
            normalized_timestamp.append(
                [max(0.0, float(pair[0]) * scale), max(0.0, float(pair[1]) * scale)]
            )
        normalized["timestamp"] = normalized_timestamp
        has_timing = has_timing or bool(normalized_timestamp)

    if has_timing:
        normalized["time_unit"] = "seconds"
    elif "time_unit" in normalized:
        normalized.pop("time_unit")
    return normalized


def _is_sqlite_database(path: Path) -> bool:
    try:
        with path.open("rb") as database_file:
            return database_file.read(len(_SQLITE_HEADER)) == _SQLITE_HEADER
    except OSError:
        return False


def _is_corruption_error(error: sqlite3.DatabaseError) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    if code in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
        return True
    message = str(error).lower()
    return "database disk image is malformed" in message or "file is not a database" in message


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _optional_float(value: Any) -> float | None:
    return float(value) if _is_finite_number(value) else None


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _json_loads(value: str) -> Any:
    return json.loads(value)


def _text_preview(text: str) -> str:
    compact = re.sub(r"\s+", " ", str(text)).strip()
    return compact[:_SUMMARY_PREVIEW_LENGTH]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
