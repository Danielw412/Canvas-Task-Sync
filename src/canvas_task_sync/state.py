from __future__ import annotations

import sqlite3
from pathlib import Path

from canvas_task_sync.models import ExtractionMode, ExtractionOutcome, StateRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS task_mappings (
    logical_id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL,
    source_key TEXT NOT NULL,
    anchor TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    source_text TEXT NOT NULL,
    title TEXT NOT NULL,
    due_date TEXT,
    google_task_id TEXT,
    tasklist_id TEXT,
    payload_hash TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS task_mappings_source
    ON task_mappings(course_id, source_key);
CREATE TABLE IF NOT EXISTS extraction_cache (
    course_id TEXT NOT NULL,
    source_key TEXT NOT NULL,
    page_hash TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    model_name TEXT NOT NULL,
    configured_mode TEXT NOT NULL,
    outcome_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (
        course_id, source_key, page_hash, extractor_version, model_name, configured_mode
    )
);
"""


class StateStore:
    def __init__(self, path: Path, *, writable: bool) -> None:
        self.path = path
        self.writable = writable
        if writable:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(path)
            self.connection.executescript(SCHEMA)
            self.connection.commit()
        elif path.exists():
            self.connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        else:
            self.connection = sqlite3.connect(":memory:")
            self.connection.executescript(SCHEMA)
        self.connection.row_factory = sqlite3.Row

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def records(self, course_id: str, source_key: str) -> list[StateRecord]:
        try:
            rows = self.connection.execute(
                """
                SELECT logical_id, course_id, source_key, anchor, ordinal, fingerprint,
                       source_text, title, due_date, google_task_id, tasklist_id, payload_hash
                  FROM task_mappings
                 WHERE course_id = ? AND source_key = ?
                 ORDER BY anchor, ordinal, logical_id
                """,
                (course_id, source_key),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [StateRecord.model_validate(dict(row)) for row in rows]

    def upsert_record(self, record: StateRecord) -> None:
        if not self.writable:
            raise RuntimeError("Dry-run state is read-only.")
        self.connection.execute(
            """
            INSERT INTO task_mappings (
                logical_id, course_id, source_key, anchor, ordinal, fingerprint,
                source_text, title, due_date, google_task_id, tasklist_id, payload_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(logical_id) DO UPDATE SET
                course_id = excluded.course_id,
                source_key = excluded.source_key,
                anchor = excluded.anchor,
                ordinal = excluded.ordinal,
                fingerprint = excluded.fingerprint,
                source_text = excluded.source_text,
                title = excluded.title,
                due_date = excluded.due_date,
                google_task_id = excluded.google_task_id,
                tasklist_id = excluded.tasklist_id,
                payload_hash = excluded.payload_hash,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                record.logical_id,
                record.course_id,
                record.source_key,
                record.anchor,
                record.ordinal,
                record.fingerprint,
                record.source_text,
                record.title,
                record.due_date,
                record.google_task_id,
                record.tasklist_id,
                record.payload_hash,
            ),
        )
        self.connection.commit()

    def cached_extraction(
        self,
        *,
        course_id: str,
        source_key: str,
        page_hash: str,
        extractor_version: str,
        model_name: str,
        configured_mode: ExtractionMode,
    ) -> ExtractionOutcome | None:
        try:
            row = self.connection.execute(
                """
                SELECT outcome_json FROM extraction_cache
                 WHERE course_id = ? AND source_key = ? AND page_hash = ?
                   AND extractor_version = ? AND model_name = ? AND configured_mode = ?
                """,
                (
                    course_id,
                    source_key,
                    page_hash,
                    extractor_version,
                    model_name,
                    configured_mode.value,
                ),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        if row is None:
            return None
        try:
            return ExtractionOutcome.model_validate_json(row["outcome_json"])
        except ValueError:
            return None

    def cache_extraction(
        self,
        *,
        course_id: str,
        source_key: str,
        page_hash: str,
        extractor_version: str,
        model_name: str,
        configured_mode: ExtractionMode,
        outcome: ExtractionOutcome,
    ) -> None:
        if not self.writable:
            return
        self.connection.execute(
            """
            INSERT INTO extraction_cache (
                course_id, source_key, page_hash, extractor_version,
                model_name, configured_mode, outcome_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(
                course_id, source_key, page_hash, extractor_version, model_name, configured_mode
            ) DO UPDATE SET outcome_json = excluded.outcome_json, updated_at = CURRENT_TIMESTAMP
            """,
            (
                course_id,
                source_key,
                page_hash,
                extractor_version,
                model_name,
                configured_mode.value,
                outcome.model_dump_json(),
            ),
        )
        self.connection.commit()
