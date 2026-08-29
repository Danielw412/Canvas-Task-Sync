from __future__ import annotations

import re
import uuid
from collections import defaultdict

from canvas_task_sync.gemini import token_similarity
from canvas_task_sync.models import DraftTask, StateRecord, TaskType

IDENTITY_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "canvas-task-sync.openai")
TABLE_ANCHOR_PATTERN = re.compile(r"^table:(?P<element>.+):r\d+:c(?P<column>\d+)$")
MATCH_THRESHOLD = 0.75
MATCH_MARGIN = 0.15


def initial_logical_id(draft: DraftTask) -> str:
    identity = "|".join(
        [
            draft.course_id,
            draft.source_key,
            draft.source_anchor,
            str(draft.ordinal),
        ]
    )
    return str(uuid.uuid5(IDENTITY_NAMESPACE, identity))


def _unreserved_initial_logical_id(draft: DraftTask, reserved: set[str]) -> str:
    candidate = initial_logical_id(draft)
    collision_index = 0
    while candidate in reserved:
        collision_index += 1
        identity = "|".join(
            [
                draft.course_id,
                draft.source_key,
                draft.source_anchor,
                str(draft.ordinal),
                f"inserted:{collision_index}",
            ]
        )
        candidate = str(uuid.uuid5(IDENTITY_NAMESPACE, identity))
    return candidate


def _family(anchor: str) -> str | None:
    match = TABLE_ANCHOR_PATTERN.match(anchor)
    if not match:
        return None
    return f"table:{match.group('element')}:c{match.group('column')}"


def _score(draft: DraftTask, record: StateRecord) -> float:
    evidence = token_similarity(draft.source_text, record.source_text)
    title = token_similarity(draft.title, record.title)
    return (0.75 * evidence) + (0.25 * title)


def _unique_match(
    draft: DraftTask,
    candidates: list[StateRecord],
) -> StateRecord | None:
    ranked = sorted(
        ((_score(draft, record), record.logical_id, record) for record in candidates),
        key=lambda item: (-item[0], item[1]),
    )
    if not ranked or ranked[0][0] < MATCH_THRESHOLD:
        return None
    runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
    if ranked[0][0] - runner_up < MATCH_MARGIN:
        return None
    return ranked[0][2]


def resolve_logical_ids(
    drafts: list[DraftTask],
    records: list[StateRecord],
) -> dict[int, str]:
    """Map draft list indexes to durable IDs without using Gemini wording as identity."""

    available = {record.logical_id: record for record in records}
    resolved: dict[int, str] = {}
    drafts_by_anchor: dict[str, list[int]] = defaultdict(list)
    records_by_anchor: dict[str, list[StateRecord]] = defaultdict(list)
    for index, draft in enumerate(drafts):
        drafts_by_anchor[draft.source_anchor].append(index)
    for record in records:
        records_by_anchor[record.anchor].append(record)

    # Exact fingerprints preserve IDs when deterministic in-cell ordering changes.
    for anchor, indexes in drafts_by_anchor.items():
        candidates = records_by_anchor.get(anchor, [])
        for index in indexes:
            exact = [
                record
                for record in candidates
                if record.logical_id in available
                and record.fingerprint == drafts[index].fingerprint
            ]
            if len(exact) == 1:
                record = exact[0]
                resolved[index] = record.logical_id
                available.pop(record.logical_id, None)

    # Unique normalized-token matches handle reordered or lightly edited in-cell actions.
    for anchor, indexes in drafts_by_anchor.items():
        for index in indexes:
            if index in resolved:
                continue
            candidates = [
                record
                for record in records_by_anchor.get(anchor, [])
                if record.logical_id in available
            ]
            match = _unique_match(drafts[index], candidates)
            if match:
                resolved[index] = match.logical_id
                available.pop(match.logical_id, None)

    # Position is authoritative for a wholesale wording edit only when one unambiguous item
    # remains in the stable block. Multiple unmatched actions are never paired by position.
    for anchor, indexes in drafts_by_anchor.items():
        unresolved = [index for index in indexes if index not in resolved]
        candidates = [
            record
            for record in records_by_anchor.get(anchor, [])
            if record.logical_id in available
        ]
        if len(unresolved) == 1 and len(candidates) == 1:
            resolved[unresolved[0]] = candidates[0].logical_id
            available.pop(candidates[0].logical_id, None)

    # When one legacy assessment is split into dated sections, preserve its remote task
    # as the first section and allocate new IDs only for the remaining sections.
    assessment_words = re.compile(r"\b(?:quiz|test|exam)\b", re.IGNORECASE)
    for anchor, indexes in drafts_by_anchor.items():
        unresolved = [index for index in indexes if index not in resolved]
        candidates = [
            record
            for record in records_by_anchor.get(anchor, [])
            if record.logical_id in available
        ]
        if (
            len(unresolved) < 2
            or len(candidates) != 1
            or not assessment_words.search(candidates[0].title)
            or not all(
                drafts[index].task_type in {TaskType.QUIZ, TaskType.TEST}
                for index in unresolved
            )
        ):
            continue
        first_section = min(
            unresolved,
            key=lambda index: (
                drafts[index].due_date is None,
                drafts[index].due_date or drafts[index].ordinal,
                drafts[index].ordinal,
            ),
        )
        resolved[first_section] = candidates[0].logical_id
        available.pop(candidates[0].logical_id, None)

    # A unique high-confidence match in the same table column survives inserted table rows.
    records_by_family: dict[str, list[StateRecord]] = defaultdict(list)
    for record in available.values():
        family = _family(record.anchor)
        if family:
            records_by_family[family].append(record)
    for index, draft in enumerate(drafts):
        if index in resolved:
            continue
        family = _family(draft.source_anchor)
        if not family:
            continue
        candidates = [
            record
            for record in records_by_family.get(family, [])
            if record.logical_id in available
        ]
        match = _unique_match(draft, candidates)
        if match:
            resolved[index] = match.logical_id
            available.pop(match.logical_id, None)

    # A newly inserted action can occupy an ordinal previously used by a matched
    # record later in the same block. Reserve every stored/resolved ID so the new
    # action cannot accidentally alias the existing remote task.
    reserved = {record.logical_id for record in records} | set(resolved.values())
    for index, draft in enumerate(drafts):
        if index in resolved:
            continue
        logical_id = _unreserved_initial_logical_id(draft, reserved)
        resolved[index] = logical_id
        reserved.add(logical_id)
    return resolved
