from __future__ import annotations

import hashlib
import json
from datetime import date

from canvas_task_sync.gemini import normalized_text
from canvas_task_sync.google_tasks import GoogleTasksClient, date_from_google_due
from canvas_task_sync.identity import resolve_logical_ids
from canvas_task_sync.managed_notes import (
    compose_task_notes,
    has_managed_delimiter,
    parse_notes,
)
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
    TaskType,
    UncertainItem,
)
from canvas_task_sync.state import StateStore


def desired_payload_hash(desired: DesiredTask) -> str:
    payload = json.dumps(
        {
            "title": desired.title,
            "due": desired.due_date.isoformat() if desired.due_date else None,
            "due_uncertain": desired.due_uncertain,
            "details": desired.details,
            "assignment_url": desired.assignment_url,
            "task_type": desired.task_type.value,
            "destination_task_list": desired.destination_task_list,
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
        source_url=desired.source_url,
        assignment_url=desired.assignment_url,
        anchor=desired.source_anchor,
        ordinal=desired.ordinal,
        fingerprint=desired.fingerprint,
        source_text=desired.source_text,
        title=desired.title,
        details=desired.details,
        classification=desired.classification,
        task_type=desired.task_type,
        action_kind=desired.action_kind,
        due_date=desired.due_date.isoformat() if desired.due_date else None,
        due_basis=desired.due_basis,
        due_uncertain=desired.due_uncertain,
        due_uncertain_reason=desired.due_uncertain_reason,
        source_date=desired.source_date.isoformat() if desired.source_date else None,
        historical=desired.historical,
        google_task_id=remote_task_id,
        tasklist_id=tasklist_id,
        tasklist_title=desired.destination_task_list,
        payload_hash=desired_payload_hash(desired),
    )


def _course_prefix(prefix: str, title: str) -> bool:
    header = f"[{prefix}]".casefold()
    folded = title.casefold()
    return folded == header or folded.startswith(f"{header} ")


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
        assessment_task_list: str = "Tests",
        tasklist_ids: dict[str, str] | None = None,
        collision_remote_tasks: list[RemoteTask] | None = None,
        course_prefix: str | None = None,
    ) -> SyncPlan:
        tasklist_ids = tasklist_ids or {task_list: ""}
        id_to_title = {identifier: title for title, identifier in tasklist_ids.items()}
        default_tasklist_id = tasklist_ids.get(task_list)

        active_remote = [task for task in remote_tasks if not task.deleted]
        for task in active_remote:
            if task.tasklist_id is None:
                task.tasklist_id = default_tasklist_id
            if task.tasklist_title is None and task.tasklist_id in id_to_title:
                task.tasklist_title = id_to_title[task.tasklist_id]
        remote_by_id = {(task.tasklist_id, task.id): task for task in active_remote}

        marker_matches: dict[str, list[RemoteTask]] = {}
        marker_metadata: dict[tuple[str | None, str], dict[str, object]] = {}
        marker_records: dict[tuple[str | None, str], StateRecord] = {}
        recovered_records: list[StateRecord] = []
        known_state_ids = {record.logical_id for record in state_records}
        malformed_markers: list[RemoteTask] = []
        for remote in active_remote:
            parsed = parse_notes(remote.notes)
            metadata = parsed.metadata
            if parsed.managed_block is None:
                if has_managed_delimiter(remote.notes) and (
                    course_prefix is None or _course_prefix(course_prefix, remote.title)
                ):
                    malformed_markers.append(remote)
                continue
            if not metadata or not isinstance(metadata.get("logical_id"), str):
                if course_prefix is None or _course_prefix(course_prefix, remote.title):
                    malformed_markers.append(remote)
                continue
            logical_id = str(metadata["logical_id"])
            marker_matches.setdefault(logical_id, []).append(remote)
            marker_metadata[(remote.tasklist_id, remote.id)] = metadata
            if metadata.get("course_id") != course_id:
                continue
            try:
                recovered = StateRecord(
                    logical_id=logical_id,
                    course_id=course_id,
                    source_key=str(metadata["source_key"]),
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
                    tasklist_id=remote.tasklist_id,
                )
                marker_records[(remote.tasklist_id, remote.id)] = recovered
                if logical_id not in known_state_ids and recovered.source_key == source_key:
                    recovered_records.append(recovered)
            except (KeyError, TypeError, ValueError):
                malformed_markers.append(remote)

        effective_records = [*state_records, *recovered_records]
        state_by_logical = {record.logical_id: record for record in effective_records}
        logical_ids = resolve_logical_ids(drafts, effective_records)
        desired_tasks: list[DesiredTask] = []
        for index, draft in enumerate(drafts):
            logical_id = logical_ids[index]
            mapped = state_by_logical.get(logical_id)
            default_destination = (
                assessment_task_list
                if draft.task_type in {TaskType.QUIZ, TaskType.TEST}
                else task_list
            )
            destination = (
                id_to_title.get(mapped.tasklist_id, default_destination)
                if mapped
                else default_destination
            )
            desired_tasks.append(
                DesiredTask(
                    **draft.model_dump(),
                    logical_id=logical_id,
                    destination_task_list=destination,
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
        for remote in malformed_markers:
            actions.append(
                SyncAction(
                    kind=SyncActionKind.UNCERTAIN,
                    title=remote.title,
                    reason="Legacy managed notes are malformed; notes were not removed.",
                    remote_task_id=remote.id,
                    task_list=remote.tasklist_title,
                )
            )

        desired_ids = {desired.logical_id for desired in desired_tasks}
        used_remote_keys: set[tuple[str | None, str]] = set()
        collision_remote_tasks = collision_remote_tasks or active_remote

        for desired in desired_tasks:
            state_record = state_by_logical.get(desired.logical_id)
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
                        task_list=desired.destination_task_list,
                    )
                )
                continue

            remote = marker_remotes[0] if marker_remotes else None
            if remote is None and state_record and state_record.google_task_id:
                remote = remote_by_id.get((state_record.tasklist_id, state_record.google_task_id))

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
                        task_list=desired.destination_task_list,
                    )
                )
                if remote:
                    used_remote_keys.add((remote.tasklist_id, remote.id))
                continue

            if remote is None:
                if state_record and state_record.google_task_id:
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
                            remote_task_id=state_record.google_task_id,
                            desired=desired,
                            task_list=desired.destination_task_list,
                        )
                    )
                    continue

                collisions = [
                    task
                    for task in collision_remote_tasks
                    if normalized_text(task.title) == normalized_text(desired.title)
                    and date_from_google_due(task.due) == desired.due_date
                ]
                if collisions:
                    collision = collisions[0]
                    actions.append(
                        SyncAction(
                            kind=SyncActionKind.IGNORED,
                            title=desired.title,
                            logical_id=desired.logical_id,
                            due_date=desired.due_date,
                            reason=(
                                "A same-class task has the same title and due date; no duplicate "
                                "will be made."
                            ),
                            evidence=desired.source_text,
                            source_anchor=desired.source_anchor,
                            remote_task_id=collision.id,
                            desired=desired,
                            task_list=collision.tasklist_title or desired.destination_task_list,
                        )
                    )
                    continue

                actions.append(
                    SyncAction(
                        kind=SyncActionKind.CREATE,
                        title=desired.title,
                        logical_id=desired.logical_id,
                        due_date=desired.due_date,
                        reason=(
                            "No local mapping or same-class Google Task exists for this source "
                            "identity."
                        ),
                        evidence=desired.source_text,
                        source_anchor=desired.source_anchor,
                        desired=desired,
                        task_list=desired.destination_task_list,
                    )
                )
                continue

            used_remote_keys.add((remote.tasklist_id, remote.id))
            expected_notes = compose_task_notes(remote.notes, desired)
            changed: list[str] = []
            if remote.title != desired.title:
                changed.append("title")
            if date_from_google_due(remote.due) != desired.due_date:
                changed.append("due date")
            if remote.notes != expected_notes:
                changed.append("notes")

            actual_destination = remote.tasklist_title or desired.destination_task_list
            desired.destination_task_list = actual_destination
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
                        task_list=actual_destination,
                        replacement_notes=expected_notes,
                    )
                )
            else:
                actions.append(
                    SyncAction(
                        kind=SyncActionKind.UNCHANGED,
                        title=desired.title,
                        logical_id=desired.logical_id,
                        due_date=desired.due_date,
                        reason=(
                            "Remote title and due date already match; metadata is stored locally."
                        ),
                        evidence=desired.source_text,
                        source_anchor=desired.source_anchor,
                        remote_task_id=remote.id,
                        desired=desired,
                        task_list=actual_destination,
                    )
                )

        for record in state_records:
            if record.logical_id in desired_ids:
                continue
            remote = (
                remote_by_id.get((record.tasklist_id, record.google_task_id))
                if record.google_task_id
                else None
            )
            actions.append(
                SyncAction(
                    kind=SyncActionKind.SOURCE_MISSING,
                    title=record.title,
                    logical_id=record.logical_id,
                    due_date=date.fromisoformat(record.due_date) if record.due_date else None,
                    reason=(
                        "This mapped task is no longer present in the current extraction; it "
                        "will not be deleted automatically."
                    ),
                    evidence=record.source_text,
                    source_anchor=record.anchor,
                    remote_task_id=remote.id if remote else record.google_task_id,
                    task_list=id_to_title.get(record.tasklist_id),
                )
            )

        for remote in active_remote:
            key = (remote.tasklist_id, remote.id)
            if key in used_remote_keys:
                continue
            metadata = marker_metadata.get(key)
            parsed = parse_notes(remote.notes)
            if (
                not metadata
                or parsed.managed_block is None
                or metadata.get("course_id") != course_id
            ):
                continue
            logical_id = str(metadata.get("logical_id", ""))
            actions.append(
                SyncAction(
                    kind=SyncActionKind.NOTES_CLEANUP,
                    title=remote.title,
                    logical_id=logical_id or None,
                    due_date=date_from_google_due(remote.due),
                    reason=(
                        "Move legacy sync metadata into local state and remove it from task notes."
                    ),
                    remote_task_id=remote.id,
                    task_list=remote.tasklist_title,
                    replacement_notes=parsed.user_notes,
                    recovered_state=(
                        state_by_logical.get(logical_id) or marker_records.get(key)
                    ),
                )
            )

        return SyncPlan(
            course_id=course_id,
            task_list=task_list,
            task_lists=list(dict.fromkeys([task_list, assessment_task_list])),
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
        if action.kind == SyncActionKind.CREATE and desired is not None:
            remote = tasks_client.create_task(
                tasklist_id,
                title=desired.title,
                notes=compose_task_notes("", desired),
                due_date=desired.due_date,
            )
            action.remote_task_id = remote.id
            state.upsert_record(
                _state_record(desired, tasklist_id=tasklist_id, remote_task_id=remote.id)
            )
            verified = tasks_client.verify_due(tasklist_id, remote.id, desired.due_date)
            action.due_verified = True
            remote_by_id[remote.id] = verified
        elif action.kind == SyncActionKind.UPDATE and desired is not None:
            if not action.remote_task_id:
                raise RuntimeError(f"Update for {desired.title} has no remote task ID.")
            existing = remote_by_id.get(action.remote_task_id)
            notes = action.replacement_notes
            if notes is None:
                notes = compose_task_notes(existing.notes if existing else "", desired)
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
            verified = tasks_client.verify_due(tasklist_id, remote.id, desired.due_date)
            action.due_verified = True
            remote_by_id[remote.id] = verified
        elif action.kind == SyncActionKind.NOTES_CLEANUP:
            if not action.remote_task_id or action.replacement_notes is None:
                raise RuntimeError(f"Notes cleanup for {action.title} is incomplete.")
            remote = tasks_client.update_notes(
                tasklist_id,
                action.remote_task_id,
                action.replacement_notes,
            )
            if action.recovered_state is not None:
                action.recovered_state.tasklist_id = tasklist_id
                action.recovered_state.google_task_id = remote.id
                state.upsert_record(action.recovered_state)
            remote_by_id[remote.id] = remote
        elif (
            action.kind == SyncActionKind.UNCHANGED
            and desired is not None
            and action.remote_task_id
        ):
            state.upsert_record(
                _state_record(
                    desired,
                    tasklist_id=tasklist_id,
                    remote_task_id=action.remote_task_id,
                )
            )
