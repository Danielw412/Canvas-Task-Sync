from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from canvas_task_sync.models import ExtractionMode, SyncPlan
from canvas_task_sync.redaction import redact_text, sanitize
from canvas_task_sync.web_models import (
    EventLevel,
    RunDetail,
    RunEvent,
    RunMode,
    RunStage,
    RunStatus,
    RunSummary,
    RunTrigger,
    Schedule,
    ScheduleCreate,
    ScheduleMode,
    ScheduleOccurrence,
    ScheduleUpdate,
)

SCHEMA_VERSION = 1
SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id TEXT NOT NULL,
    trigger TEXT NOT NULL,
    requested_mode TEXT NOT NULL,
    schedule_id INTEGER,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    extraction_mode TEXT,
    include_past INTEGER NOT NULL DEFAULT 0,
    test_rebase_week TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    plan_json TEXT,
    prepared_json TEXT,
    plan_hash TEXT,
    config_hash TEXT,
    page_hash TEXT,
    remote_hash TEXT,
    counts_json TEXT NOT NULL DEFAULT '{}',
    applied_counts_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT,
    error_summary TEXT
);
CREATE INDEX IF NOT EXISTS runs_created_at ON runs(created_at DESC);
CREATE INDEX IF NOT EXISTS runs_course ON runs(course_id, created_at DESC);
CREATE INDEX IF NOT EXISTS runs_status ON runs(status, created_at DESC);
CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    stage TEXT NOT NULL,
    event_type TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    duration_ms INTEGER,
    UNIQUE(run_id, sequence)
);
CREATE INDEX IF NOT EXISTS run_events_run ON run_events(run_id, sequence);
CREATE INDEX IF NOT EXISTS run_events_created ON run_events(created_at DESC);
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    course_id TEXT NOT NULL,
    weekdays_json TEXT NOT NULL,
    local_time TEXT NOT NULL,
    timezone TEXT NOT NULL,
    mode TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    next_run_at TEXT,
    last_run_at TEXT,
    last_result TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS schedules_due ON schedules(enabled, next_run_at);
CREATE TABLE IF NOT EXISTS schedule_occurrences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id INTEGER NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    scheduled_for TEXT NOT NULL,
    status TEXT NOT NULL,
    run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    details TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(schedule_id, scheduled_for)
);
CREATE INDEX IF NOT EXISTS schedule_occurrences_schedule
    ON schedule_occurrences(schedule_id, scheduled_for DESC);
CREATE TABLE IF NOT EXISTS control_settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


class ControlStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA journal_mode = WAL")
            self.connection.execute("PRAGMA busy_timeout = 5000")
            self.connection.executescript(SCHEMA)
            self.connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self.connection.execute(
                """
                INSERT OR IGNORE INTO control_settings(key, value_json, updated_at)
                VALUES ('history_retention_days', '90', ?)
                """,
                (_iso(utc_now()),),
            )
            self.connection.commit()

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def create_run(
        self,
        *,
        course_id: str,
        trigger: RunTrigger,
        requested_mode: RunMode,
        extraction_mode: ExtractionMode | None = None,
        include_past: bool = False,
        test_rebase_week: date | None = None,
        schedule_id: int | None = None,
    ) -> int:
        created_at = _iso(utc_now())
        with self._lock:
            cursor = self.connection.execute(
                """
                INSERT INTO runs(
                    course_id, trigger, requested_mode, schedule_id, status, stage,
                    created_at, extraction_mode, include_past, test_rebase_week
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    course_id,
                    trigger.value,
                    requested_mode.value,
                    schedule_id,
                    RunStatus.QUEUED.value,
                    RunStage.QUEUED.value,
                    created_at,
                    extraction_mode.value if extraction_mode else None,
                    int(include_past),
                    test_rebase_week.isoformat() if test_rebase_week else None,
                ),
            )
            self.connection.commit()
            return int(cursor.lastrowid)

    def update_run(self, run_id: int, **values: Any) -> None:
        allowed = {
            "status",
            "stage",
            "started_at",
            "finished_at",
            "extraction_mode",
            "cancel_requested",
            "plan_json",
            "prepared_json",
            "plan_hash",
            "config_hash",
            "page_hash",
            "remote_hash",
            "counts_json",
            "applied_counts_json",
            "error_code",
            "error_summary",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"Unsupported run fields: {', '.join(sorted(unknown))}")
        if not values:
            return
        normalized: dict[str, Any] = {}
        for key, value in values.items():
            if isinstance(value, (RunStatus, RunStage, ExtractionMode)):
                normalized[key] = value.value
            elif isinstance(value, datetime):
                normalized[key] = _iso(value)
            elif isinstance(value, bool):
                normalized[key] = int(value)
            elif key.endswith("_json") and not isinstance(value, str):
                normalized[key] = json.dumps(value, ensure_ascii=False, sort_keys=True)
            else:
                normalized[key] = value
        assignments = ", ".join(f"{key} = ?" for key in normalized)
        with self._lock:
            self.connection.execute(
                f"UPDATE runs SET {assignments} WHERE id = ?",  # noqa: S608 - whitelist above.
                (*normalized.values(), run_id),
            )
            self.connection.commit()

    def mark_run_started(self, run_id: int, *, stage: RunStage) -> None:
        self.update_run(
            run_id,
            status=RunStatus.RUNNING,
            stage=stage,
            started_at=utc_now(),
            finished_at=None,
            error_code=None,
            error_summary=None,
        )

    def request_cancel(self, run_id: int) -> bool:
        with self._lock:
            cursor = self.connection.execute(
                """
                UPDATE runs SET cancel_requested = 1
                 WHERE id = ? AND status IN (?, ?)
                """,
                (run_id, RunStatus.QUEUED.value, RunStatus.RUNNING.value),
            )
            self.connection.commit()
            return cursor.rowcount > 0

    def cancellation_requested(self, run_id: int) -> bool:
        with self._lock:
            row = self.connection.execute(
                "SELECT cancel_requested FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def set_prepared_plan(
        self,
        run_id: int,
        *,
        plan: SyncPlan,
        prepared_json: str,
        plan_hash: str,
        config_hash: str,
        page_hash: str,
        remote_hash: str,
        counts: dict[str, int],
    ) -> None:
        self.update_run(
            run_id,
            plan_json=plan.model_dump_json(),
            prepared_json=prepared_json,
            plan_hash=plan_hash,
            config_hash=config_hash,
            page_hash=page_hash,
            remote_hash=remote_hash,
            counts_json=counts,
            extraction_mode=plan.extraction_mode,
        )

    def prepared_json(self, run_id: int) -> str | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT prepared_json FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        return str(row["prepared_json"]) if row and row["prepared_json"] else None

    def add_event(
        self,
        run_id: int,
        *,
        stage: RunStage,
        event_type: str,
        message: str,
        level: EventLevel = EventLevel.INFO,
        metadata: dict[str, Any] | None = None,
        duration_ms: int | None = None,
    ) -> RunEvent:
        with self._lock:
            row = self.connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence "
                "FROM run_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            sequence = int(row["sequence"])
            created_at = utc_now()
            cursor = self.connection.execute(
                """
                INSERT INTO run_events(
                    run_id, sequence, created_at, stage, event_type, level,
                    message, metadata_json, duration_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    sequence,
                    _iso(created_at),
                    stage.value,
                    event_type,
                    level.value,
                    redact_text(message),
                    json.dumps(sanitize(metadata or {}), ensure_ascii=False, sort_keys=True),
                    duration_ms,
                ),
            )
            self.connection.execute(
                "UPDATE runs SET stage = ? WHERE id = ?", (stage.value, run_id)
            )
            self.connection.commit()
            event_id = int(cursor.lastrowid)
        return RunEvent(
            id=event_id,
            run_id=run_id,
            sequence=sequence,
            created_at=created_at,
            stage=stage,
            event_type=event_type,
            level=level,
            message=redact_text(message),
            metadata=sanitize(metadata or {}),
            duration_ms=duration_ms,
        )

    def events_after(self, run_id: int, sequence: int = 0, limit: int = 500) -> list[RunEvent]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT id, run_id, sequence, created_at, stage, event_type, level,
                       message, metadata_json, duration_ms
                  FROM run_events
                 WHERE run_id = ? AND sequence > ?
                 ORDER BY sequence
                 LIMIT ?
                """,
                (run_id, sequence, limit),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def recent_events(self, *, limit: int = 100) -> list[RunEvent]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT id, run_id, sequence, created_at, stage, event_type, level,
                       message, metadata_json, duration_ms
                  FROM run_events
                 ORDER BY id DESC
                 LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def get_run(self, run_id: int, *, include_events: bool = True) -> RunDetail | None:
        with self._lock:
            row = self.connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        events = self.events_after(run_id) if include_events else []
        return self._run_detail_from_row(row, events)

    def list_runs(
        self,
        *,
        limit: int = 50,
        course_id: str | None = None,
        statuses: Iterable[RunStatus] | None = None,
    ) -> list[RunSummary]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if course_id:
            clauses.append("course_id = ?")
            parameters.append(course_id)
        status_values = [status.value for status in statuses or []]
        if status_values:
            placeholders = ",".join("?" for _ in status_values)
            clauses.append(f"status IN ({placeholders})")
            parameters.extend(status_values)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        with self._lock:
            rows = self.connection.execute(
                f"SELECT * FROM runs {where} ORDER BY id DESC LIMIT ?",  # noqa: S608
                parameters,
            ).fetchall()
        return [self._run_summary_from_row(row) for row in rows]

    def latest_run(self, course_id: str | None = None) -> RunSummary | None:
        runs = self.list_runs(limit=1, course_id=course_id)
        return runs[0] if runs else None

    def create_schedule(
        self,
        schedule: ScheduleCreate,
        *,
        next_run_at: datetime | None,
    ) -> Schedule:
        now = utc_now()
        with self._lock:
            cursor = self.connection.execute(
                """
                INSERT INTO schedules(
                    name, course_id, weekdays_json, local_time, timezone, mode,
                    enabled, next_run_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    schedule.name,
                    schedule.course_id,
                    json.dumps(schedule.weekdays),
                    schedule.local_time.isoformat(timespec="minutes"),
                    schedule.timezone,
                    schedule.mode.value,
                    int(schedule.enabled),
                    _iso(next_run_at),
                    _iso(now),
                    _iso(now),
                ),
            )
            self.connection.commit()
            schedule_id = int(cursor.lastrowid)
        result = self.get_schedule(schedule_id)
        if result is None:
            raise RuntimeError("Newly created schedule could not be loaded.")
        return result

    def update_schedule(
        self,
        schedule_id: int,
        schedule: ScheduleUpdate,
        *,
        next_run_at: datetime | None,
    ) -> Schedule | None:
        with self._lock:
            self.connection.execute(
                """
                UPDATE schedules SET
                    name = ?, course_id = ?, weekdays_json = ?, local_time = ?,
                    timezone = ?, mode = ?, enabled = ?, next_run_at = ?, updated_at = ?
                 WHERE id = ?
                """,
                (
                    schedule.name,
                    schedule.course_id,
                    json.dumps(schedule.weekdays),
                    schedule.local_time.isoformat(timespec="minutes"),
                    schedule.timezone,
                    schedule.mode.value,
                    int(schedule.enabled),
                    _iso(next_run_at),
                    _iso(utc_now()),
                    schedule_id,
                ),
            )
            self.connection.commit()
        return self.get_schedule(schedule_id)

    def delete_schedule(self, schedule_id: int) -> bool:
        with self._lock:
            cursor = self.connection.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
            self.connection.commit()
            return cursor.rowcount > 0

    def set_schedule_enabled(
        self,
        schedule_id: int,
        *,
        enabled: bool,
        next_run_at: datetime | None,
    ) -> Schedule | None:
        with self._lock:
            cursor = self.connection.execute(
                """
                UPDATE schedules
                   SET enabled = ?, next_run_at = ?, updated_at = ?
                 WHERE id = ?
                """,
                (int(enabled), _iso(next_run_at), _iso(utc_now()), schedule_id),
            )
            self.connection.commit()
        return self.get_schedule(schedule_id) if cursor.rowcount else None

    def disable_schedules_for_course(self, course_id: str) -> int:
        with self._lock:
            cursor = self.connection.execute(
                """
                UPDATE schedules
                   SET enabled = 0, next_run_at = NULL, updated_at = ?
                 WHERE course_id = ? AND enabled = 1
                """,
                (_iso(utc_now()), course_id),
            )
            self.connection.commit()
            return cursor.rowcount

    def get_schedule(self, schedule_id: int) -> Schedule | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
        return self._schedule_from_row(row) if row else None

    def list_schedules(self, *, enabled_only: bool = False) -> list[Schedule]:
        where = "WHERE enabled = 1" if enabled_only else ""
        with self._lock:
            rows = self.connection.execute(
                f"SELECT * FROM schedules {where} ORDER BY name COLLATE NOCASE, id"  # noqa: S608
            ).fetchall()
        return [self._schedule_from_row(row) for row in rows]

    def due_schedules(self, now: datetime) -> list[Schedule]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT * FROM schedules
                 WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?
                 ORDER BY next_run_at, id
                """,
                (_iso(now),),
            ).fetchall()
        return [self._schedule_from_row(row) for row in rows]

    def advance_schedule(
        self,
        schedule_id: int,
        *,
        next_run_at: datetime | None,
        last_run_at: datetime | None,
        last_result: str,
    ) -> None:
        with self._lock:
            self.connection.execute(
                """
                UPDATE schedules
                   SET next_run_at = ?, last_run_at = ?, last_result = ?, updated_at = ?
                 WHERE id = ?
                """,
                (
                    _iso(next_run_at),
                    _iso(last_run_at),
                    last_result,
                    _iso(utc_now()),
                    schedule_id,
                ),
            )
            self.connection.commit()

    def record_schedule_occurrence(
        self,
        *,
        schedule_id: int,
        scheduled_for: datetime,
        status: str,
        run_id: int | None = None,
        details: str = "",
    ) -> ScheduleOccurrence:
        created_at = utc_now()
        with self._lock:
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO schedule_occurrences(
                    schedule_id, scheduled_for, status, run_id, details, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    schedule_id,
                    _iso(scheduled_for),
                    status,
                    run_id,
                    details,
                    _iso(created_at),
                ),
            )
            if cursor.rowcount:
                occurrence_id = int(cursor.lastrowid)
            else:
                row = self.connection.execute(
                    """
                    SELECT id FROM schedule_occurrences
                     WHERE schedule_id = ? AND scheduled_for = ?
                    """,
                    (schedule_id, _iso(scheduled_for)),
                ).fetchone()
                occurrence_id = int(row["id"])
            self.connection.commit()
        return ScheduleOccurrence(
            id=occurrence_id,
            schedule_id=schedule_id,
            scheduled_for=scheduled_for,
            status=status,
            run_id=run_id,
            details=details,
            created_at=created_at,
        )

    def finish_schedule_occurrence(
        self,
        *,
        run_id: int,
        status: str,
        details: str,
    ) -> bool:
        with self._lock:
            row = self.connection.execute(
                "SELECT schedule_id FROM schedule_occurrences WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return False
            self.connection.execute(
                """
                UPDATE schedule_occurrences
                   SET status = ?, details = ?
                 WHERE run_id = ?
                """,
                (status, details, run_id),
            )
            self.connection.execute(
                """
                UPDATE schedules
                   SET last_result = ?, updated_at = ?
                 WHERE id = ?
                """,
                (status, _iso(utc_now()), row["schedule_id"]),
            )
            self.connection.commit()
            return True

    def list_schedule_occurrences(self, *, limit: int = 50) -> list[ScheduleOccurrence]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM schedule_occurrences ORDER BY scheduled_for DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            ScheduleOccurrence(
                id=row["id"],
                schedule_id=row["schedule_id"],
                scheduled_for=row["scheduled_for"],
                status=row["status"],
                run_id=row["run_id"],
                details=row["details"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self.connection.execute(
                "SELECT value_json FROM control_settings WHERE key = ?", (key,)
            ).fetchone()
        return _loads(row["value_json"], default) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock:
            self.connection.execute(
                """
                INSERT INTO control_settings(key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (key, json.dumps(value), _iso(utc_now())),
            )
            self.connection.commit()

    def clear_history(self) -> None:
        with self._lock:
            self.connection.execute("DELETE FROM run_events")
            self.connection.execute("DELETE FROM schedule_occurrences")
            self.connection.execute("DELETE FROM runs")
            self.connection.commit()

    def prune_history(self, *, retention_days: int | None = None) -> int:
        days = retention_days or int(self.get_setting("history_retention_days", 90))
        cutoff = _iso(utc_now() - timedelta(days=days))
        with self._lock:
            cursor = self.connection.execute("DELETE FROM runs WHERE created_at < ?", (cutoff,))
            self.connection.execute(
                "DELETE FROM schedule_occurrences WHERE created_at < ?", (cutoff,)
            )
            self.connection.commit()
            return cursor.rowcount

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> RunEvent:
        return RunEvent(
            id=row["id"],
            run_id=row["run_id"],
            sequence=row["sequence"],
            created_at=row["created_at"],
            stage=RunStage(row["stage"]),
            event_type=row["event_type"],
            level=EventLevel(row["level"]),
            message=row["message"],
            metadata=_loads(row["metadata_json"], {}),
            duration_ms=row["duration_ms"],
        )

    @staticmethod
    def _run_summary_from_row(row: sqlite3.Row) -> RunSummary:
        return RunSummary(
            id=row["id"],
            course_id=row["course_id"],
            trigger=RunTrigger(row["trigger"]),
            requested_mode=RunMode(row["requested_mode"]),
            status=RunStatus(row["status"]),
            stage=RunStage(row["stage"]),
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            extraction_mode=(
                ExtractionMode(row["extraction_mode"]) if row["extraction_mode"] else None
            ),
            counts=_loads(row["counts_json"], {}),
            applied_counts=_loads(row["applied_counts_json"], {}),
            plan_hash=row["plan_hash"],
            error_code=row["error_code"],
            error_summary=row["error_summary"],
            schedule_id=row["schedule_id"],
        )

    @classmethod
    def _run_detail_from_row(cls, row: sqlite3.Row, events: list[RunEvent]) -> RunDetail:
        summary = cls._run_summary_from_row(row)
        plan: SyncPlan | None = None
        if row["plan_json"]:
            try:
                plan = SyncPlan.model_validate_json(row["plan_json"])
            except ValueError:
                plan = None
        return RunDetail(
            **summary.model_dump(),
            include_past=bool(row["include_past"]),
            test_rebase_week=row["test_rebase_week"],
            config_hash=row["config_hash"],
            page_hash=row["page_hash"],
            remote_hash=row["remote_hash"],
            cancel_requested=bool(row["cancel_requested"]),
            plan=plan,
            events=events,
        )

    @staticmethod
    def _schedule_from_row(row: sqlite3.Row) -> Schedule:
        return Schedule(
            id=row["id"],
            name=row["name"],
            course_id=row["course_id"],
            weekdays=_loads(row["weekdays_json"], []),
            local_time=row["local_time"],
            timezone=row["timezone"],
            mode=ScheduleMode(row["mode"]),
            enabled=bool(row["enabled"]),
            next_run_at=row["next_run_at"],
            last_run_at=row["last_run_at"],
            last_result=row["last_result"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
