from __future__ import annotations

import hashlib
import json
from datetime import date

from canvas_task_sync.gemini import normalized_text
from canvas_task_sync.google_tasks import GoogleTasksClient, date_from_google_due
from canvas_task_sync.identity import resolve_logical_ids
from canvas_task_sync.managed_notes import build_managed_block, merge_notes, parse_notes
from canvas_task_sync.models import (
    DesiredTask,
    DraftTask,
    ExtractionMode,
    IgnoredItem,
    RemoteTask,
    StateRecord,
    SyncAction,
    SyncActionKind,
    SyncPlan,
    UncertainItem,
)
from canvas_task_sync.state import StateStore


def desired_payload_hash(desired: DesiredTask) -> str:
    payload = json.dumps(
        {
            "title": desired.title,
            "due": desired.due_date.isoformat() if desired.due_date else None,
            "managed_notes": desired.managed_notes,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _state_record(
    desired: DesiredTask,
    *,
    tasklist_id: str,
    remote_task_id: str,
) -> StateRecord:
    return StateRecord(
        logical_id=desired.logical_id,
        course_id=desired.course_id,
        source_key=desired.source_key,
        anchor=desired.source_anchor,
        ordinal=desired.ordinal,
        fingerprint=desired.fingerprint,
        source_text=desired.source_text,
        title=desired.title,
        due_date=desired.due_date.isoformat() if desired.due_date else None,
        google_task_id=remote_task_id,
        tasklist_id=tasklist_id,
        payload_hash=desired_payload_hash(desired),
    )


class SyncPlanner:
    def plan(
        self,
        *,
        course_id: str,
        source_key: str,
        task_list: str,
        extraction_mode: ExtractionMode,
        fallback_reasons: list[str],
        drafts: list[DraftTask],
        uncertain: list[UncertainItem],
        ignored: list[IgnoredItem],
        state_records: list[StateRecord],
        remote_tasks: list[RemoteTask],
        include_past: bool,
        dry_run: bool,
    ) -> SyncPlan:
        active_remote = [task for task in remote_tasks if not task.deleted]
        remote_by_id = {task.id: task for task in active_remote}
        marker_matches: dict[str, list[RemoteTask]] = {}
        marker_metadata: dict[str, dict[str, object]] = {}
        recovered_records: list[StateRecord] = []
        known_state_ids = {record.logical_id for record in state_records}
        for remote in active_remote:
            metadata = parse_notes(remote.notes).metadata
            if not metadata or not isinstance(metadata.get("logical_id"), str):
                continue
            logical_id = str(metadata["logical_id"])
            marker_matches.setdefault(logical_id, []).append(remote)
            marker_metadata[remote.id] = metadata
            if logical_id in known_state_ids:
                continue
            if metadata.get("course_id") != course_id or metadata.get("source_key") != source_key:
                continue
            try:
                recovered_records.append(
                    StateRecord(
                        logical_id=logical_id,
                        course_id=course_id,
                        source_key=source_key,
                        anchor=str(metadata["source_anchor"]),
                        ordinal=int(metadata["ordinal"]),
                        fingerprint=str(metadata["fingerprint"]),
                        source_text=str(metadata.get("source_text", "")),
                        title=str(metadata.get("title") or remote.title),
                        due_date=(
                            str(metadata["due_date"])
                            if metadata.get("due_date") is not None
                            else None
                        ),
                        google_task_id=remote.id,
                    )
                )
            except (KeyError, TypeError, ValueError):
                # An old/corrupt marker remains visible as a collision, never an implicit claim.
                continue

        effective_records = [*state_records, *recovered_records]
        logical_ids = resolve_logical_ids(drafts, effective_records)
        desired_tasks: list[DesiredTask] = []
        for index, draft in enumerate(drafts):
            logical_id = logical_ids[index]
            desired_tasks.append(
                DesiredTask(
                    **draft.model_dump(),
                    logical_id=logical_id,
                    managed_notes=build_managed_block(draft, logical_id),
                )
            )

        actions: list[SyncAction] = []
        for item in uncertain:
            actions.append(
                SyncAction(
                    kind=SyncActionKind.UNCERTAIN,
                    title=item.title,
                    reason=item.reason,
                    evidence=item.evidence,
                    source_anchor=item.source_anchor,
                )
            )
        for item in ignored:
            actions.append(
                SyncAction(
                    kind=SyncActionKind.IGNORED,
                    title=item.title,
                    reason=item.reason,
                    evidence=item.evidence,
                    source_anchor=item.source_anchor,
                )
            )

        state_by_logical = {record.logical_id: record for record in effective_records}
        desired_ids = {desired.logical_id for desired in desired_tasks}
        used_remote_ids: set[str] = set()

        for desired in desired_tasks:
            state = state_by_logical.get(desired.logical_id)
            marker_remotes = marker_matches.get(desired.logical_id, [])
            if len(marker_remotes) > 1:
                actions.append(
                    SyncAction(
                        kind=SyncActionKind.UNCERTAIN,
                        title=desired.title,
                        logical_id=desired.logical_id,
                        due_date=desired.due_date,
                        reason="More than one remote task contains this managed logical ID.",
                        evidence=desired.source_text,
                        source_anchor=desired.source_anchor,
                        desired=desired,
                    )
                )
                continue

            remote = marker_remotes[0] if marker_remotes else None
            if remote is None and state and state.google_task_id:
                remote = remote_by_id.get(state.google_task_id)

            if desired.historical and not include_past:
                actions.append(
                    SyncAction(
                        kind=SyncActionKind.HISTORICAL_BLOCKED,
                        title=desired.title,
                        logical_id=desired.logical_id,
                        due_date=desired.due_date,
                        reason="Due date is in the past; pass --include-past to allow it.",
                        evidence=desired.source_text,
                        source_anchor=desired.source_anchor,
                        remote_task_id=remote.id if remote else None,
                        desired=desired,
                    )
                )
                if remote:
                    used_remote_ids.add(remote.id)
                continue

            if remote is None:
                if state and state.google_task_id:
                    actions.append(
                        SyncAction(
                            kind=SyncActionKind.REMOTE_MISSING,
                            title=desired.title,
                            logical_id=desired.logical_id,
                            due_date=desired.due_date,
                            reason=(
                                "The mapped Google Task is missing or deleted; it will not be "
                                "recreated automatically."
                            ),
                            evidence=desired.source_text,
                            source_anchor=desired.source_anchor,
                            remote_task_id=state.google_task_id,
                            desired=desired,
                        )
                    )
                    continue

                collisions = [
                    task
                    for task in active_remote
                    if parse_notes(task.notes).metadata is None
                    and normalized_text(task.title) == normalized_text(desired.title)
                    and date_from_google_due(task.due) == desired.due_date
                ]
                if collisions:
                    actions.append(
                        SyncAction(
                            kind=SyncActionKind.UNCERTAIN,
                            title=desired.title,
                            logical_id=desired.logical_id,
                            due_date=desired.due_date,
                            reason=(
                                "An unmanaged task has the same title and due date; no automatic "
                                "claim or duplicate will be made."
                            ),
                            evidence=desired.source_text,
                            source_anchor=desired.source_anchor,
                            remote_task_id=collisions[0].id,
                            desired=desired,
                        )
                    )
                    continue

                actions.append(
                    SyncAction(
                        kind=SyncActionKind.CREATE,
                        title=desired.title,
                        logical_id=desired.logical_id,
                        due_date=desired.due_date,
                        reason="No managed Google Task exists for this source identity.",
                        evidence=desired.source_text,
                        source_anchor=desired.source_anchor,
                        desired=desired,
                    )
                )
                continue

            used_remote_ids.add(remote.id)
            expected_notes = merge_notes(remote.notes, desired.managed_notes)
            changed: list[str] = []
            if remote.title != desired.title:
                changed.append("title")
            if date_from_google_due(remote.due) != desired.due_date:
                changed.append("due date")
            if remote.notes != expected_notes:
                changed.append("managed notes")

            if changed:
                actions.append(
                    SyncAction(
                        kind=SyncActionKind.UPDATE,
                        title=desired.title,
                        logical_id=desired.logical_id,
                        due_date=desired.due_date,
                        reason=f"Changed {', '.join(changed)}.",
                        evidence=desired.source_text,
                        source_anchor=desired.source_anchor,
                        remote_task_id=remote.id,
                        desired=desired,
                    )
                )
            else:
                actions.append(
                    SyncAction(
                        kind=SyncActionKind.UNCHANGED,
                        title=desired.title,
                        logical_id=desired.logical_id,
                        due_date=desired.due_date,
                        reason="Remote title, due date, and managed notes already match.",
                        evidence=desired.source_text,
                        source_anchor=desired.source_anchor,
                        remote_task_id=remote.id,
                        desired=desired,
                    )
                )

        for record in state_records:
            if record.logical_id in desired_ids:
                continue
            marker_remote = marker_matches.get(record.logical_id, [])
            remote = marker_remote[0] if len(marker_remote) == 1 else None
            if remote is None and record.google_task_id:
                remote = remote_by_id.get(record.google_task_id)
            actions.append(
                SyncAction(
                    kind=SyncActionKind.SOURCE_MISSING,
                    title=record.title,
                    logical_id=record.logical_id,
                    due_date=(
                        date.fromisoformat(record.due_date)
                        if record.due_date
                        else None
                    ),
                    reason=(
                        "This mapped task is no longer present in the current extraction; it will "
                        "not be deleted automatically."
                    ),
                    evidence=record.source_text,
                    source_anchor=record.anchor,
                    remote_task_id=remote.id if remote else record.google_task_id,
                )
            )

        # Markers permit recovery even when SQLite is lost. Surface stale markers from this source.
        for remote in active_remote:
            if remote.id in used_remote_ids:
                continue
            metadata = marker_metadata.get(remote.id)
            if not metadata:
                continue
            if metadata.get("course_id") != course_id:
                continue
            if metadata.get("source_key") != source_key:
                continue
            logical_id = str(metadata.get("logical_id"))
            if logical_id in desired_ids or logical_id in state_by_logical:
                continue
            actions.append(
                SyncAction(
                    kind=SyncActionKind.SOURCE_MISSING,
                    title=remote.title,
                    logical_id=logical_id,
                    due_date=date_from_google_due(remote.due),
                    reason=(
                        "A recoverable managed marker exists, but its source item is no longer "
                        "present; the task will not be deleted automatically."
                    ),
                    source_anchor=(
                        str(metadata["source_anchor"])
                        if metadata.get("source_anchor") is not None
                        else None
                    ),
                    remote_task_id=remote.id,
                )
            )

        return SyncPlan(
            course_id=course_id,
            task_list=task_list,
            dry_run=dry_run,
            extraction_mode=extraction_mode,
            fallback_reasons=fallback_reasons,
            actions=actions,
        )


def apply_sync_plan(
    plan: SyncPlan,
    *,
    tasklist_id: str,
    tasks_client: GoogleTasksClient,
    state: StateStore,
) -> None:
    if plan.dry_run:
        raise RuntimeError("A dry-run plan cannot be applied.")
    remote_by_id = {task.id: task for task in tasks_client.list_tasks(tasklist_id)}
    for action in plan.actions:
        desired = action.desired
        if desired is None:
            continue
        if action.kind == SyncActionKind.CREATE:
            remote = tasks_client.create_task(
                tasklist_id,
                title=desired.title,
                notes=desired.managed_notes,
                due_date=desired.due_date,
            )
            action.remote_task_id = remote.id
            state.upsert_record(
                _state_record(desired, tasklist_id=tasklist_id, remote_task_id=remote.id)
            )
            remote_by_id[remote.id] = remote
        elif action.kind == SyncActionKind.UPDATE:
            if not action.remote_task_id:
                raise RuntimeError(f"Update for {desired.title} has no remote task ID.")
            # The managed replacement is idempotent and preserves every outside user note.
            existing = remote_by_id.get(action.remote_task_id)
            existing_notes = existing.notes if existing else ""
            notes = merge_notes(existing_notes, desired.managed_notes)
            remote = tasks_client.update_task(
                tasklist_id,
                action.remote_task_id,
                title=desired.title,
                notes=notes,
                due_date=desired.due_date,
            )
            state.upsert_record(
                _state_record(desired, tasklist_id=tasklist_id, remote_task_id=remote.id)
            )
            remote_by_id[remote.id] = remote
        elif action.kind == SyncActionKind.UNCHANGED and action.remote_task_id:
            state.upsert_record(
                _state_record(
                    desired,
                    tasklist_id=tasklist_id,
                    remote_task_id=action.remote_task_id,
                )
            )
