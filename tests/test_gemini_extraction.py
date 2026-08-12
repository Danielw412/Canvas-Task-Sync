from __future__ import annotations

from copy import deepcopy

import pytest

from canvas_task_sync.gemini import GeminiExtractor
from canvas_task_sync.models import Confidence, ExtractionMode


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
