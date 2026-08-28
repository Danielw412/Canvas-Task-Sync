from __future__ import annotations

import sqlite3
from pathlib import Path

from canvas_task_sync.models import ExtractionMode, ExtractionOutcome, StateRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS task_mappings (
    logical_id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL,
    source_key TEXT NOT NULL,
    source_url TEXT,
    assignment_url TEXT,
    anchor TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    source_text TEXT NOT NULL,
    title TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '',
    classification TEXT,
    task_type TEXT,
    action_kind TEXT,
    due_date TEXT,
    due_basis TEXT,
    due_uncertain INTEGER NOT NULL DEFAULT 0,
    due_uncertain_reason TEXT,
    source_date TEXT,
    historical INTEGER NOT NULL DEFAULT 0,
    google_task_id TEXT,
    tasklist_id TEXT,
    tasklist_title TEXT,
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

TASK_MAPPING_COLUMNS: dict[str, str] = {
    "source_url": "TEXT",
    "assignment_url": "TEXT",
    "details": "TEXT NOT NULL DEFAULT ''",
    "classification": "TEXT",
    "task_type": "TEXT",
    "action_kind": "TEXT",
    "due_basis": "TEXT",
    "due_uncertain": "INTEGER NOT NULL DEFAULT 0",
    "due_uncertain_reason": "TEXT",
    "source_date": "TEXT",
    "historical": "INTEGER NOT NULL DEFAULT 0",
    "tasklist_title": "TEXT",
}

STATE_RECORD_COLUMNS: tuple[tuple[str, str], ...] = (
    ("logical_id", "NULL"),
    ("course_id", "NULL"),
    ("source_key", "NULL"),
    ("source_url", "NULL"),
    ("assignment_url", "NULL"),
    ("anchor", "NULL"),
    ("ordinal", "0"),
    ("fingerprint", "''"),
    ("source_text", "''"),
    ("title", "''"),
    ("details", "''"),
    ("classification", "NULL"),
    ("task_type", "NULL"),
    ("action_kind", "NULL"),
    ("due_date", "NULL"),
    ("due_basis", "NULL"),
    ("due_uncertain", "0"),
    ("due_uncertain_reason", "NULL"),
    ("source_date", "NULL"),
    ("historical", "0"),
    ("google_task_id", "NULL"),
    ("tasklist_id", "NULL"),
    ("tasklist_title", "NULL"),
    ("payload_hash", "NULL"),
)


class StateStore:
    def __init__(self, path: Path, *, writable: bool) -> None:
        self.path = path
        self.writable = writable
        if writable:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(path)
            self.connection.executescript(SCHEMA)
            self._migrate_task_mappings()
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

    def _task_mapping_column_names(self) -> set[str]:
        try:
            rows = self.connection.execute("PRAGMA table_info(task_mappings)").fetchall()
        except sqlite3.OperationalError:
            return set()
        return {str(row[1]) for row in rows}

    def _migrate_task_mappings(self) -> None:
        existing = self._task_mapping_column_names()
        for name, declaration in TASK_MAPPING_COLUMNS.items():
            if name not in existing:
                self.connection.execute(
                    f"ALTER TABLE task_mappings ADD COLUMN {name} {declaration}"
                )

    def _record_select(self) -> str:
        existing = self._task_mapping_column_names()
        return ", ".join(
            name if name in existing else f"{fallback} AS {name}"
            for name, fallback in STATE_RECORD_COLUMNS
        )

    def _read_records(
        self,
        *,
        where: str = "",
        parameters: tuple[object, ...] = (),
        order_by: str = "course_id, due_date, title, logical_id",
    ) -> list[StateRecord]:
        try:
            rows = self.connection.execute(
                f"SELECT {self._record_select()} FROM task_mappings {where} ORDER BY {order_by}",
                parameters,
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [StateRecord.model_validate(dict(row)) for row in rows]

    def records(self, course_id: str, source_key: str) -> list[StateRecord]:
        return self._read_records(
            where="WHERE course_id = ? AND source_key = ?",
            parameters=(course_id, source_key),
            order_by="anchor, ordinal, logical_id",
        )

    def all_records(self) -> list[StateRecord]:
        return self._read_records()

    def get_record(self, logical_id: str) -> StateRecord | None:
        records = self._read_records(
            where="WHERE logical_id = ?",
            parameters=(logical_id,),
            order_by="logical_id",
        )
        return records[0] if records else None

    def upsert_record(self, record: StateRecord) -> None:
        if not self.writable:
            raise RuntimeError("Dry-run state is read-only.")
        self.connection.execute(
            """
            INSERT INTO task_mappings (
                logical_id, course_id, source_key, source_url, assignment_url,
                anchor, ordinal, fingerprint, source_text, title, details,
                classification, task_type, action_kind, due_date, due_basis,
                due_uncertain, due_uncertain_reason, source_date, historical,
                google_task_id, tasklist_id, tasklist_title, payload_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(logical_id) DO UPDATE SET
                course_id = excluded.course_id,
                source_key = excluded.source_key,
                source_url = excluded.source_url,
                assignment_url = excluded.assignment_url,
                anchor = excluded.anchor,
                ordinal = excluded.ordinal,
                fingerprint = excluded.fingerprint,
                source_text = excluded.source_text,
                title = excluded.title,
                details = excluded.details,
                classification = excluded.classification,
                task_type = excluded.task_type,
                action_kind = excluded.action_kind,
                due_date = excluded.due_date,
                due_basis = excluded.due_basis,
                due_uncertain = excluded.due_uncertain,
                due_uncertain_reason = excluded.due_uncertain_reason,
                source_date = excluded.source_date,
                historical = excluded.historical,
                google_task_id = excluded.google_task_id,
                tasklist_id = excluded.tasklist_id,
                tasklist_title = excluded.tasklist_title,
                payload_hash = excluded.payload_hash,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                record.logical_id,
                record.course_id,
                record.source_key,
                record.source_url,
                record.assignment_url,
                record.anchor,
                record.ordinal,
                record.fingerprint,
                record.source_text,
                record.title,
                record.details,
                record.classification.value if record.classification else None,
                record.task_type.value if record.task_type else None,
                record.action_kind.value if record.action_kind else None,
                record.due_date,
                record.due_basis,
                int(record.due_uncertain),
                record.due_uncertain_reason,
                record.source_date,
                int(record.historical),
                record.google_task_id,
                record.tasklist_id,
                record.tasklist_title,
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
