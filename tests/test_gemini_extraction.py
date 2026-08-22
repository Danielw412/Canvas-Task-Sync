from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from canvas_task_sync.configuration import CourseSettings
from canvas_task_sync.gemini import GeminiExtractionError, GeminiExtractor, GoogleGenAIBackend
from canvas_task_sync.models import (
    ActionKind,
    AgendaBlock,
    Confidence,
    DueRelation,
    ExtractionMode,
    GeminiTaskCandidate,
    SourceCapture,
    TaskClassification,
)


class FakeBackend:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def generate(self, *, prompt, image_bytes, image_mime_type):
        self.calls.append(
            {
                "prompt": prompt,
                "image_bytes": image_bytes,
                "image_mime_type": image_mime_type,
            }
        )
        return self.responses.pop(0)


@pytest.mark.parametrize(
    ("mode", "has_image", "has_exact_text"),
    [
        (ExtractionMode.IMAGE, True, False),
        (ExtractionMode.TEXT, False, True),
        (ExtractionMode.HYBRID, True, True),
    ],
)
def test_modes_control_what_gemini_receives(
    mode,
    has_image,
    has_exact_text,
    spanish_capture,
    spanish_course,
    spanish_candidates,
):
    course = spanish_course.model_copy(deep=True)
    course.source.extraction.mode = mode
    backend = FakeBackend([spanish_candidates])

    outcome = GeminiExtractor(backend).extract(spanish_capture, course)

    assert outcome.used_mode == mode
    assert (backend.calls[0]["image_bytes"] is not None) is has_image
    assert ("Completar actividades de práctica - VHL" in backend.calls[0]["prompt"]) is (
        has_exact_text
    )
    assert len(outcome.tasks) == 6
    assert len(spanish_capture.image_bytes) > 10_000


def test_hybrid_uses_exact_page_text_for_unicode(
    spanish_capture, spanish_course, spanish_candidates
):
    backend = FakeBackend([spanish_candidates])
    outcome = GeminiExtractor(backend).extract(spanish_capture, spanish_course)
    evidence = {task.source_text for task in outcome.tasks}

    assert "Completar actividades de práctica - VHL" in evidence
    assert "Presentar Conversaciones hipotéticas" in evidence
    assert "Completar actividad de la clase y entregarla aquí" in evidence
    assert "¡Traer DINERO!" in evidence
    assert "De Niño" in backend.calls[0]["prompt"]


def test_unreconciled_source_disagreement_is_uncertain(
    spanish_capture, spanish_course, spanish_candidates
):
    conflicting = deepcopy(spanish_candidates[:1])
    conflicting[0].source_text = "Leer un capítulo que no aparece en esta celda"
    backend = FakeBackend([conflicting])

    outcome = GeminiExtractor(backend).extract(spanish_capture, spanish_course)

    assert not outcome.tasks
    assert len(outcome.uncertain) == 1
    assert "could not be reconciled" in outcome.uncertain[0].reason


def test_exact_canvas_subphrase_reconciles_without_absorbing_the_entire_row():
    block = AgendaBlock(
        anchor="canvas:physics:6",
        element_id="physics:table:0",
        kind="table_cell",
        row_index=2,
        row_label="T",
        text=(
            "Tuesday Classwork: Unit 1 Assignment 1 in Canvas Homework: "
            "Read chapter 3 in the text book, and sign up for AP Classroom."
        ),
    )
    capture = SourceCapture(
        source_key="canvas:11126:week:2026-08-17",
        source_url="https://canvas.example/courses/11126",
        source_type="canvas",
        page_hash="a" * 64,
        transcript=block.text,
        blocks=[block],
        selection={"week_start": "2026-08-17"},
    )
    candidate = GeminiTaskCandidate(
        source_anchor=block.anchor,
        source_text="Read chapter 3 in the text book",
        row_label="T",
        classification=TaskClassification.HOMEWORK,
        action_kind=ActionKind.READ,
        title="Read chapter 3",
        due_relation=DueRelation.NEXT_CLASS,
        confidence=Confidence.HIGH,
    )
    course = CourseSettings.model_validate(
        {
            "name": "AP Physics C",
            "prefix": "PHYSICS",
            "task_list": "School",
            "source": {"type": "none", "extraction": {"mode": "text"}},
            "canvas_course_id": "11126",
        }
    )

    outcome = GeminiExtractor(FakeBackend([[candidate]])).extract(capture, course)

    assert not outcome.uncertain
    assert len(outcome.tasks) == 1
    assert outcome.tasks[0].source_text == "Read chapter 3 in the text book"


def test_auto_retries_hybrid_when_image_ocr_loses_punctuation(
    spanish_capture, spanish_course, spanish_candidates
):
    course = spanish_course.model_copy(deep=True)
    course.source.extraction.mode = ExtractionMode.AUTO
    corrupted = deepcopy(spanish_candidates)
    corrupted[1].source_text = "Completar actividades de practica - VHL"
    backend = FakeBackend([corrupted, spanish_candidates])

    outcome = GeminiExtractor(backend).extract(spanish_capture, course)

    assert len(backend.calls) == 2
    assert backend.calls[0]["image_bytes"] is not None
    assert "Completar actividades de práctica - VHL" not in backend.calls[0]["prompt"]
    assert "Completar actividades de práctica - VHL" in backend.calls[1]["prompt"]
    assert outcome.used_mode == ExtractionMode.HYBRID
    assert any("OCR evidence mismatch" in reason for reason in outcome.fallback_reasons)
    assert any(task.source_text.endswith("práctica - VHL") for task in outcome.tasks)


def test_auto_retries_when_anchor_or_row_context_is_missing(
    spanish_capture, spanish_course, spanish_candidates
):
    course = spanish_course.model_copy(deep=True)
    course.source.extraction.mode = ExtractionMode.AUTO
    image_candidates = deepcopy(spanish_candidates)
    image_candidates[0].source_anchor = "unknown"
    image_candidates[0].row_label = None
    image_candidates[0].confidence = Confidence.MEDIUM
    backend = FakeBackend([image_candidates, spanish_candidates])

    outcome = GeminiExtractor(backend).extract(spanish_capture, course)

    assert outcome.used_mode == ExtractionMode.HYBRID
    assert len(backend.calls) == 2
    assert outcome.fallback_reasons


def test_model_quota_fallback_chain_keeps_high_reasoning():
    calls = []

    class Models:
        def generate_content(self, *, model, contents, config):
            calls.append((model, contents, config))
            if model != "gemini-3.5-flash":
                raise RuntimeError("429 RESOURCE_EXHAUSTED quota exceeded")
            return SimpleNamespace(parsed=[], text="[]")

    backend = GoogleGenAIBackend(
        "gemini-3.7-flash",
        client=SimpleNamespace(models=Models()),
        fallback_models=["gemini-3.6-flash", "gemini-3.5-flash"],
        retry_waiter=lambda _seconds, _attempts: None,
    )
    assert backend.generate(prompt="agenda", image_bytes=None, image_mime_type=None) == []
    assert [call[0] for call in calls] == [
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
    ]
    assert all(
        str(call[2].thinking_config.thinking_level).casefold().endswith("high")
        for call in calls
    )
    assert backend.used_model == "gemini-3.5-flash"
    assert len(backend.fallback_reasons) == 3


def test_two_failed_models_wait_one_minute_then_retry_chain():
    calls: list[str] = []
    waits: list[tuple[float, list[str]]] = []

    class Models:
        def generate_content(self, *, model, **_kwargs):
            calls.append(model)
            if len(calls) <= 2:
                raise RuntimeError("503 UNAVAILABLE")
            return SimpleNamespace(parsed=[], text="[]")

    backend = GoogleGenAIBackend(
        "gemini-3.7-flash",
        client=SimpleNamespace(models=Models()),
        fallback_models=["gemini-3.6-flash"],
        retry_waiter=lambda seconds, attempts: waits.append((seconds, attempts)),
    )

    assert backend.generate(prompt="agenda", image_bytes=None, image_mime_type=None) == []
    assert calls == ["gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.7-flash"]
    assert waits == [(60.0, [
        "gemini-3.7-flash: service unavailable",
        "gemini-3.6-flash: service unavailable",
    ])]


def test_recent_assignment_context_is_included_in_gemini_prompt(
    spanish_capture, spanish_course, spanish_candidates
):
    backend = FakeBackend([spanish_candidates])
    GeminiExtractor(backend).extract(
        spanish_capture,
        spanish_course,
        existing_assignments=["[SPANISH] VHL practice | needsAction | due 2026-08-18"],
    )
    assert "[SPANISH] VHL practice | needsAction | due 2026-08-18" in backend.calls[0][
        "prompt"
    ]
    assert "task_type=quiz" in backend.calls[0]["prompt"]
    assert "Study guides, studying, preparation, corrections" in backend.calls[0]["prompt"]
    assert "latest consecutive occurrence" in backend.calls[0]["prompt"]
    assert "exact atomic phrase" in backend.calls[0]["prompt"]
    assert "one to three concise sentences" in backend.calls[0]["prompt"]


def test_course_ai_instructions_are_delimited_and_scoped_to_that_course(
    spanish_capture, spanish_course, spanish_candidates
):
    instructed = spanish_course.model_copy(deep=True)
    instructed.ai_instructions = "Do not create homework tasks for reading assignments."
    instructed_backend = FakeBackend([spanish_candidates])
    GeminiExtractor(instructed_backend).extract(spanish_capture, instructed)

    plain_backend = FakeBackend([spanish_candidates])
    GeminiExtractor(plain_backend).extract(spanish_capture, spanish_course)

    instructed_prompt = instructed_backend.calls[0]["prompt"]
    assert "<course-instructions>" in instructed_prompt
    assert "Do not create homework tasks for reading assignments." in instructed_prompt
    assert "Do not create homework tasks for reading assignments." not in plain_backend.calls[0][
        "prompt"
    ]


def test_exhausted_model_chain_reports_each_attempt_without_raw_provider_details():
    class Models:
        def generate_content(self, **_kwargs):
            raise RuntimeError("429 RESOURCE_EXHAUSTED secret-provider-detail")

    backend = GoogleGenAIBackend(
        "gemini-3.7-flash",
        client=SimpleNamespace(models=Models()),
        fallback_models=["gemini-3.6-flash", "gemini-3.5-flash"],
        retry_waiter=lambda _seconds, _attempts: None,
    )
    with pytest.raises(GeminiExtractionError) as caught:
        backend.generate(prompt="agenda", image_bytes=None, image_mime_type=None)
    message = str(caught.value)
    assert "gemini-3.7-flash: quota or rate limit" in message
    assert "gemini-3.6-flash: quota or rate limit" in message
    assert "gemini-3.5-flash: quota or rate limit" in message
    assert "secret-provider-detail" not in message
