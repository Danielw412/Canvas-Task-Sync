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

