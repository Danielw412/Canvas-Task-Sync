from __future__ import annotations

from canvas_task_sync.models import ExtractionMode, ExtractionOutcome, StateRecord
from canvas_task_sync.state import StateStore


def test_state_and_extraction_cache_round_trip(tmp_path):
    path = tmp_path / "state.sqlite3"
    record = StateRecord(
        logical_id="logical",
        course_id="spanish",
        source_key="source",
        anchor="anchor",
        ordinal=0,
        fingerprint="fingerprint",
        source_text="evidence",
        title="[SPANISH] Task",
        google_task_id="remote",
    )
    outcome = ExtractionOutcome(used_mode=ExtractionMode.HYBRID)
    with StateStore(path, writable=True) as state:
        state.upsert_record(record)
        state.cache_extraction(
            course_id="spanish",
            source_key="source",
            page_hash="hash",
            extractor_version="v1",
            model_name="model",
            configured_mode=ExtractionMode.HYBRID,
            outcome=outcome,
        )

    with StateStore(path, writable=False) as state:
        assert state.records("spanish", "source") == [record]
        cached = state.cached_extraction(
            course_id="spanish",
            source_key="source",
            page_hash="hash",
            extractor_version="v1",
            model_name="model",
            configured_mode=ExtractionMode.HYBRID,
        )
        assert cached == outcome


def test_a_state_file_without_tables_reads_as_empty(tmp_path):
    path = tmp_path / "state.sqlite3"
    path.touch()  # an empty file is a valid, table-less SQLite database
    with StateStore(path, writable=False) as state:
        assert state.all_records() == []


def test_a_locked_state_database_is_an_error_not_an_empty_list(tmp_path):
    import sqlite3

    import pytest

    class Locked:
        def execute(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        def close(self):
            pass

    with StateStore(tmp_path / "missing.sqlite3", writable=False) as state:
        state.connection = Locked()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            state.all_records()

