from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from canvas_task_sync.configuration import load_settings
from canvas_task_sync.models import GeminiTaskCandidate, SourceCapture
from canvas_task_sync.sources.google_slides import (
    build_anchor_transcript,
    canonical_page_hash,
    parse_page_blocks,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def spanish_page() -> dict[str, object]:
    return json.loads((FIXTURES / "spanish_target_page.json").read_text(encoding="utf-8"))


@pytest.fixture
def spanish_capture(spanish_page: dict[str, object]) -> SourceCapture:
    blocks = parse_page_blocks(spanish_page)
    image_bytes = base64.b64decode(
        (FIXTURES / "spanish_target_page.png.b64").read_text(encoding="ascii")
    )
    return SourceCapture(
        source_key="google_slides:fixture:g8596fffd0c_4_6",
        source_url="https://docs.google.com/presentation/d/fixture/edit",
        presentation_id="fixture",
        page_id="g8596fffd0c_4_6",
        page_hash=canonical_page_hash(spanish_page),
        transcript=build_anchor_transcript(blocks),
        blocks=blocks,
        image_bytes=image_bytes,
        image_mime_type="image/png",
    )


@pytest.fixture
def spanish_candidates() -> list[GeminiTaskCandidate]:
    raw = json.loads((FIXTURES / "spanish_gemini_tasks.json").read_text(encoding="utf-8"))
    return [GeminiTaskCandidate.model_validate(item) for item in raw]


@pytest.fixture
def spanish_course():
    root = Path(__file__).parents[1]
    return load_settings(root / "config" / "courses.yaml").course("spanish")

