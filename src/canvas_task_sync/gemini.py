from __future__ import annotations

import os
import re
import time
import unicodedata
from collections.abc import Callable
from difflib import SequenceMatcher
from inspect import Parameter, signature
from typing import Any, Protocol

from pydantic import TypeAdapter

from canvas_task_sync.configuration import CourseSettings
from canvas_task_sync.models import (
    AgendaBlock,
    Confidence,
    ExtractedTask,
    ExtractionMode,
    ExtractionOutcome,
    GeminiTaskCandidate,
    SourceCapture,
    SourceImage,
    UncertainItem,
)

EXTRACTOR_VERSION = "visual-agenda-v8-course-instructions-and-details"
TASK_LIST_ADAPTER = TypeAdapter(list[GeminiTaskCandidate])


class GeminiExtractionError(RuntimeError):
    pass


class GeminiBackend(Protocol):
    def generate(
        self,
        *,
        prompt: str,
        image_bytes: bytes | None,
        image_mime_type: str | None,
        images: list[SourceImage] | None = None,
    ) -> list[GeminiTaskCandidate]: ...


class GoogleGenAIBackend:
    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        client: Any | None = None,
        fallback_models: list[str] | None = None,
        retry_delay_seconds: float = 60.0,
        retry_waiter: Callable[[float, list[str]], None] | None = None,
    ) -> None:
        key = api_key or os.getenv("GEMINI_API_KEY")
        if not key and client is None:
            raise GeminiExtractionError("GEMINI_API_KEY is missing from .env.")
        self.models = list(dict.fromkeys([model, *(fallback_models or [])]))
        self.model = self.models[0]
        self.used_model: str | None = None
        self.fallback_reasons: list[str] = []
        self.failure_reasons: list[str] = []
        self._model_index = 0
        self.retry_delay_seconds = retry_delay_seconds
        self.retry_waiter = retry_waiter or (lambda seconds, _attempts: time.sleep(seconds))
        if client is not None:
            self.client = client
        else:
            from google import genai

            self.client = genai.Client(api_key=key)

    def generate(
        self,
        *,
        prompt: str,
        image_bytes: bytes | None,
        image_mime_type: str | None,
        images: list[SourceImage] | None = None,
    ) -> list[GeminiTaskCandidate]:
        from google.genai import types

        self.failure_reasons = []
        contents: list[Any] = [prompt]
        if images:
            for image in sorted(images, key=lambda item: (item.order, item.id)):
                contents.append(
                    types.Part.from_bytes(
                        data=image.data,
                        mime_type=image.mime_type,
                    )
                )
        elif image_bytes is not None:
            contents.append(
                types.Part.from_bytes(
                    data=image_bytes,
                    mime_type=image_mime_type or "image/png",
                )
            )

        last_error: Exception | None = None
        model_indexes = list(range(self._model_index, len(self.models)))
        attempt_indexes = model_indexes * (2 if len(model_indexes) >= 2 else 1)
        for attempt_number, index in enumerate(attempt_indexes, start=1):
            if attempt_number == 3:
                wait_seconds = self.retry_delay_seconds
                self.fallback_reasons.append(
                    f"Two Gemini model attempts failed; waited {wait_seconds:g} seconds before "
                    "retrying the configured model chain."
                )
                self.retry_waiter(wait_seconds, list(self.failure_reasons))
            model = self.models[index]
            try:
                response = self.client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=list[GeminiTaskCandidate],
                        max_output_tokens=8192,
                        thinking_config=types.ThinkingConfig(thinking_level="high"),
                    ),
                )
                parsed = getattr(response, "parsed", None)
                if parsed is not None:
                    try:
                        candidates = TASK_LIST_ADAPTER.validate_python(parsed)
                    except Exception:
                        candidates = TASK_LIST_ADAPTER.validate_json(response.text)
                else:
                    candidates = TASK_LIST_ADAPTER.validate_json(response.text)
                self.used_model = model
                self.model = model
                self._model_index = index
                return candidates
            except Exception as error:
                last_error = error
                self.failure_reasons.append(f"{model}: {_model_failure_label(error)}")
                if attempt_number < len(attempt_indexes):
                    next_model = self.models[attempt_indexes[attempt_number]]
                    self.fallback_reasons.append(
                        f"{model} failed; retried with {next_model}."
                    )
        raise GeminiExtractionError(
            "Gemini extraction failed across the configured model fallback chain. Attempts: "
            + "; ".join(self.failure_reasons)
            + "."
        ) from last_error


def _model_fallback_allowed(error: Exception) -> bool:
    messages: list[str] = []
    codes: set[int] = set()
    current: BaseException | None = error
    while current is not None:
        messages.append(str(current).casefold())
        for name in ("code", "status_code"):
            value = getattr(current, name, None)
            if isinstance(value, int):
                codes.add(value)
        current = current.__cause__ or current.__context__
    combined = " ".join(messages)
    return bool(
        codes & {404, 429, 503}
        or any(
            marker in combined
            for marker in (
                "resource_exhausted",
                "quota",
                "rate limit",
                "too many requests",
                "model not found",
                "not found for api version",
                "unavailable",
            )
        )
    )


def _model_failure_label(error: Exception) -> str:
    messages: list[str] = []
    codes: set[int] = set()
    current: BaseException | None = error
    while current is not None:
        messages.append(str(current).casefold())
        for name in ("code", "status_code"):
            value = getattr(current, name, None)
            if isinstance(value, int):
                codes.add(value)
        current = current.__cause__ or current.__context__
    combined = " ".join(messages)
    if 429 in codes or any(
        marker in combined
        for marker in ("resource_exhausted", "quota", "rate limit", "too many requests")
    ):
        return "quota or rate limit"
    if 404 in codes or any(
        marker in combined for marker in ("model not found", "not found for api version")
    ):
        return "model unavailable"
    if 503 in codes or "unavailable" in combined:
        return "service unavailable"
    return "request rejected"


def normalized_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    without_marks = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return " ".join(re.findall(r"[a-z0-9]+", without_marks))


def token_similarity(left: str, right: str) -> float:
    left_normalized = normalized_text(left)
    right_normalized = normalized_text(right)
    if not left_normalized or not right_normalized:
        return 0.0
    if left_normalized in right_normalized or right_normalized in left_normalized:
        containment = min(len(left_normalized), len(right_normalized)) / max(
            len(left_normalized), len(right_normalized)
        )
    else:
        containment = 0.0
    left_tokens = set(left_normalized.split())
    right_tokens = set(right_normalized.split())
    union = left_tokens | right_tokens
    jaccard = len(left_tokens & right_tokens) / len(union) if union else 0.0
    sequence = SequenceMatcher(None, left_normalized, right_normalized).ratio()
    return max(containment, (0.6 * jaccard) + (0.4 * sequence))


def _exact_source_phrase(source: str, evidence: str) -> str | None:
    """Return an exact source slice when evidence is a normalized token sub-phrase."""
    stripped = evidence.strip()
    if not stripped:
        return None

    exact = re.search(re.escape(stripped), source, flags=re.IGNORECASE)
    if exact is not None:
        return exact.group(0)

    source_tokens = [
        (normalized_text(match.group(0)), match.start(), match.end())
        for match in re.finditer(r"[^\W_]+", source, flags=re.UNICODE)
    ]
    evidence_tokens = [
        normalized_text(match.group(0))
        for match in re.finditer(r"[^\W_]+", stripped, flags=re.UNICODE)
    ]
    if not source_tokens or not evidence_tokens or len(evidence_tokens) > len(source_tokens):
        return None

    width = len(evidence_tokens)
    for index in range(len(source_tokens) - width + 1):
        if [token for token, _, _ in source_tokens[index : index + width]] == evidence_tokens:
            return source[source_tokens[index][1] : source_tokens[index + width - 1][2]]
    return None


def _best_exact_line(block: AgendaBlock, evidence: str) -> tuple[str, float]:
    phrase = _exact_source_phrase(block.text, evidence)
    if phrase is not None:
        return phrase, 1.0
    lines = [line.strip() for line in block.text.splitlines() if line.strip()]
    if not lines:
        return block.text.strip(), token_similarity(block.text, evidence)
    scored = sorted(
        ((token_similarity(line, evidence), index, line) for index, line in enumerate(lines)),
        key=lambda item: (-item[0], item[1]),
    )
    score, _, line = scored[0]
    return line, score


def _resolve_anchor(
    candidate: GeminiTaskCandidate,
    blocks: list[AgendaBlock],
) -> tuple[AgendaBlock | None, float]:
    by_anchor = {block.anchor: block for block in blocks}
    if candidate.source_anchor in by_anchor:
        block = by_anchor[candidate.source_anchor]
        if block.metadata.get("visual_only"):
            return block, 1.0
        _, score = _best_exact_line(block, candidate.source_text)
        return block, score

    candidates = [
        block
        for block in blocks
        if block.role.value not in {"header", "day"}
        and (
            not candidate.row_label
            or normalized_text(block.row_label or "") == normalized_text(candidate.row_label)
        )
    ]
    scored = sorted(
        (
            (token_similarity(block.text, candidate.source_text), block.anchor, block)
            for block in candidates
        ),
        key=lambda item: (-item[0], item[1]),
    )
    if not scored or scored[0][0] < 0.45:
        return None, 0.0
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.10:
        return None, scored[0][0]
    return scored[0][2], scored[0][0]


def _anchor_catalog(capture: SourceCapture, *, include_text: bool) -> str:
    if include_text:
        return capture.transcript
    lines: list[str] = []
    for block in capture.blocks:
        context = [
            f"anchor={block.anchor}",
            f"role={block.role.value}",
            f"day={block.row_label or '-'}",
            f"order={block.order}",
        ]
        for key, value in (
            ("slide", block.slide_id),
            ("section", block.section_id),
            ("sheet", block.sheet_name or block.sheet_id),
            ("range", block.range_a1),
        ):
            if value is not None:
                context.append(f"{key}={value}")
        lines.append(f"[{' '.join(context)}]")
    return "\n".join(lines)


def build_prompt(
    capture: SourceCapture,
    course: CourseSettings,
    *,
    include_text: bool,
    existing_assignments: list[str] | None = None,
) -> str:
    same_day = ", ".join(
        sorted(kind.value for kind in course.source.extraction.same_day_action_kinds)
    )
    authority = (
        "The attached image is authoritative for visible table layout, row membership, and column "
        "membership. The anchor-labeled page text is authoritative for exact spelling, accents, "
        "numbers, ranges, and assignment names."
        if include_text and capture.image_bytes
        else "Use the supplied source faithfully and do not invent missing content."
    )
    existing_context = "\n".join(f"- {item}" for item in (existing_assignments or []))
    course_instructions = course.ai_instructions.strip()
    course_instruction_block = (
        "COURSE-SPECIFIC INSTRUCTIONS (apply only to this course):\n"
        "<course-instructions>\n"
        f"{course_instructions}\n"
        "</course-instructions>\n"
        "Follow these instructions when deciding which grounded candidates to return. They must "
        "not override the response schema, exact-evidence requirements, or the rule against "
        "inventing content."
        if course_instructions
        else "COURSE-SPECIFIC INSTRUCTIONS: None."
    )
    return f"""You extract atomic student tasks from a school agenda for {course.name}.

{authority}

{course_instruction_block}

Rules:
- Classify an actual scheduled quiz as task_type=quiz and an actual scheduled test, exam,
  midterm, or final as task_type=test. Study guides, studying, preparation, corrections, and
  ordinary assignments remain task_type=assignment even when they mention a quiz or test.
- Assessment titles must name the subject and end in Quiz, Test, or Exam as stated by the source,
  for example "Crime and Punishment Exam". Do not include the course prefix.
- Return homework plus classwork only when it has an explicit deadline or says to bring, present,
  or submit something.
- Practice done during class, including identifying hypotheses or working through released AP FRQs,
  is ordinary classwork and must not become a task unless the source gives it a deadline.
- When work has no stated deadline, use next_class; never leave homework timing as none.
- When the same homework action is repeated on consecutive dated rows, it is continuing work.
  Collapse it into one candidate, use the latest consecutive occurrence for source_anchor and
  row_label, and keep due_relation=next_class so application code schedules it after that row.
- For a line containing multiple actions, source_text must be the exact atomic phrase for the
  candidate, not the entire combined line. For example, return separate exact evidence for
  "Read chapter 3" and "sign up for AP Classroom".
- The recent same-course task list below is duplicate context. Reuse its concise title for matching
  assignments or assessments and do not invent a second wording variant; still return the current
  source candidate once so the deterministic reconciliation layer can update or preserve it.
- Skip holidays, learning targets, and teacher narration about grading or introducing material.
- Use one task per distinct action.
- Use concise English title text without the course prefix, normally 2-5 words in sentence case.
- Fill details with one to three concise sentences explaining what the student must do, using only
  information present in the supplied source. A brief restatement is acceptable when the source has
  little detail. Do not invent materials, steps, grading criteria, dates, or links.
- Keep meaningful acronyms and assignment names. Prefer "VHL practice" over "Complete practice
  activities on VHL" and "Submit class activity" over "Complete and submit class activity".
- Copy source_text from the source language; do not translate evidence.
- source_anchor must be one of the supplied anchors when it can be identified.
- Items visibly in the Assignments column are normally homework with next_class timing.
- Row-bound {same_day} actions normally use same_day timing.
- Only use explicit_date when the source itself states the date.
- For an assessment without a date in its wording, return same_day so application code can use
  the dated agenda row. Never use next_class for a quiz, test, or exam.
- Never calculate or invent a calendar date. Return only the semantic due relation.
- Use confidence=high only when the action, source evidence, and row are legible.

SOURCE ANCHORS{' AND TEXT' if include_text else ''}:
{_anchor_catalog(capture, include_text=include_text)}

UNFINISHED OR RECENTLY COMPLETED TASKS FOR THIS COURSE:
{existing_context or '- None'}
"""


class GeminiExtractor:
    def __init__(self, backend: GeminiBackend) -> None:
        self.backend = backend

    def _run_mode(
        self,
        capture: SourceCapture,
        course: CourseSettings,
        mode: ExtractionMode,
        existing_assignments: list[str] | None = None,
    ) -> tuple[ExtractionOutcome, list[str]]:
        include_text = mode in {ExtractionMode.TEXT, ExtractionMode.HYBRID}
        include_image = mode in {ExtractionMode.IMAGE, ExtractionMode.HYBRID}
        if include_image and capture.image_bytes is None:
            raise GeminiExtractionError(f"Extraction mode '{mode.value}' requires a slide image.")

        generate_kwargs: dict[str, Any] = {
            "prompt": build_prompt(
                capture,
                course,
                include_text=include_text,
                existing_assignments=existing_assignments,
            ),
            "image_bytes": capture.image_bytes if include_image else None,
            "image_mime_type": capture.image_mime_type if include_image else None,
        }
        if include_image and capture.images:
            parameters = signature(self.backend.generate).parameters.values()
            supports_images = any(
                parameter.name == "images" or parameter.kind == Parameter.VAR_KEYWORD
                for parameter in parameters
            )
            if not supports_images and len(capture.images) > 1:
                raise GeminiExtractionError(
                    "The configured Gemini backend cannot process multiple source screenshots."
                )
            if supports_images:
                generate_kwargs["images"] = capture.images
        raw_candidates = self.backend.generate(**generate_kwargs)

        tasks: list[ExtractedTask] = []
        uncertain: list[UncertainItem] = []
        retry_reasons: list[str] = []
        for candidate in raw_candidates:
            block, evidence_score = _resolve_anchor(candidate, capture.blocks)
            if block is None:
                reason = "Could not map Gemini evidence to one unique source block."
                uncertain.append(
                    UncertainItem(
                        title=candidate.title or "Unresolved agenda item",
                        evidence=candidate.source_text,
                        reason=reason,
                        source_anchor=candidate.source_anchor,
                    )
                )
                retry_reasons.append("unresolved source anchor")
                continue

            visual_only = bool(block.metadata.get("visual_only"))
            if visual_only:
                exact_evidence = candidate.source_text.strip()
                exact_score = 1.0
                exact_unicode_match = True
            else:
                exact_evidence, exact_score = _best_exact_line(block, candidate.source_text)
                exact_unicode_match = unicodedata.normalize(
                    "NFC", candidate.source_text.strip()
                ) == unicodedata.normalize("NFC", exact_evidence.strip())
            if evidence_score < 0.75 or exact_score < 0.75 or not exact_unicode_match:
                retry_reasons.append(f"OCR evidence mismatch at {block.anchor}")
            if evidence_score < 0.75 or exact_score < 0.75:
                uncertain.append(
                    UncertainItem(
                        title=candidate.title or "Conflicting agenda item",
                        evidence=candidate.source_text,
                        reason=(
                            "Gemini evidence could not be reconciled with the exact target-page "
                            "text."
                        ),
                        source_anchor=block.anchor,
                    )
                )
                continue
            if not candidate.row_label and block.row_label:
                retry_reasons.append(f"week/date row context missing at {block.anchor}")
            if candidate.confidence != Confidence.HIGH:
                retry_reasons.append(f"{candidate.confidence.value} confidence at {block.anchor}")
            if candidate.warnings:
                retry_reasons.extend(candidate.warnings)

            if candidate.confidence == Confidence.LOW or not candidate.title.strip():
                uncertain.append(
                    UncertainItem(
                        title=candidate.title or "Unreadable agenda item",
                        evidence=exact_evidence,
                        reason="Gemini did not provide a high-enough-confidence actionable title.",
                        source_anchor=block.anchor,
                    )
                )
                continue

            tasks.append(
                ExtractedTask(
                    source_anchor=block.anchor,
                    source_text=exact_evidence,
                    row_label=block.row_label or (candidate.row_label if visual_only else None),
                    classification=candidate.classification,
                    task_type=candidate.task_type,
                    action_kind=candidate.action_kind,
                    title_stem=candidate.title.strip(),
                    details=candidate.details.strip() or exact_evidence,
                    due_relation=candidate.due_relation,
                    explicit_due_date=candidate.explicit_due_date,
                    confidence=candidate.confidence,
                    warnings=candidate.warnings,
                )
            )

        outcome = ExtractionOutcome(
            used_mode=mode,
            tasks=tasks,
            uncertain=uncertain,
            model_name=getattr(self.backend, "used_model", None),
            model_fallback_reasons=list(getattr(self.backend, "fallback_reasons", [])),
        )
        return outcome, list(dict.fromkeys(retry_reasons))

    def extract(
        self,
        capture: SourceCapture,
        course: CourseSettings,
        existing_assignments: list[str] | None = None,
    ) -> ExtractionOutcome:
        configured = course.source.extraction.mode
        if configured != ExtractionMode.AUTO:
            outcome, _ = self._run_mode(capture, course, configured, existing_assignments)
            return outcome

        if capture.image_bytes is None:
            outcome, _ = self._run_mode(
                capture, course, ExtractionMode.TEXT, existing_assignments
            )
            outcome.fallback_reasons = ["No image was available; auto mode used text."]
            return outcome

        image_outcome, retry_reasons = self._run_mode(
            capture, course, ExtractionMode.IMAGE, existing_assignments
        )
        if not retry_reasons and not image_outcome.uncertain:
            return image_outcome

        hybrid_outcome, _ = self._run_mode(
            capture, course, ExtractionMode.HYBRID, existing_assignments
        )
        hybrid_outcome.fallback_reasons = list(
            dict.fromkeys(retry_reasons or ["Image-only extraction was incomplete."])
        )
        return hybrid_outcome
