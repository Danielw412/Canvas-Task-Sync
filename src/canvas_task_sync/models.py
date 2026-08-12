from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ExtractionMode(StrEnum):
    IMAGE = "image"
    TEXT = "text"
    HYBRID = "hybrid"
    AUTO = "auto"


class BlockRole(StrEnum):
    HEADER = "header"
    DAY = "day"
    LEARNING = "learning"
    ASSIGNMENTS = "assignments"
    UNKNOWN = "unknown"


class TaskClassification(StrEnum):
    HOMEWORK = "homework"
    CLASSWORK = "classwork"


class ActionKind(StrEnum):
    PRACTICE = "practice"
    COMPLETE = "complete"
    BRING = "bring"
    PRESENT = "present"
    SUBMIT = "submit"
    READ = "read"
    STUDY = "study"
    WRITE = "write"
    OTHER = "other"


class DueRelation(StrEnum):
    EXPLICIT_DATE = "explicit_date"
    SAME_DAY = "same_day"
    NEXT_CLASS = "next_class"
    NONE = "none"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SyncActionKind(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    UNCHANGED = "unchanged"
    UNCERTAIN = "uncertain"
    IGNORED = "ignored"
    SOURCE_MISSING = "source_missing"
    REMOTE_MISSING = "remote_missing"
    HISTORICAL_BLOCKED = "historical_blocked"


class AgendaBlock(BaseModel):
    anchor: str
    element_id: str
    kind: str
    role: BlockRole = BlockRole.UNKNOWN
    row_index: int | None = None
    column_index: int | None = None
    row_label: str | None = None
    text: str
    geometry: dict[str, Any] = Field(default_factory=dict)


class SourceCapture(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    source_key: str
    source_url: str
    presentation_id: str
    page_id: str
    page_hash: str
    transcript: str
    blocks: list[AgendaBlock]
    image_bytes: bytes | None = None
    image_mime_type: str | None = None
    image_sha256: str | None = None


class GeminiTaskCandidate(BaseModel):
    source_anchor: str | None = None
    source_text: str
    row_label: str | None = None
    classification: TaskClassification
    action_kind: ActionKind
    title: str
    details: str = ""
    due_relation: DueRelation = DueRelation.NONE
    explicit_due_date: str | None = None
    confidence: Confidence = Confidence.MEDIUM
    warnings: list[str] = Field(default_factory=list)


class ExtractedTask(BaseModel):
    source_anchor: str
    source_text: str
    row_label: str | None = None
    classification: TaskClassification
    action_kind: ActionKind
    title_stem: str
    details: str = ""
    due_relation: DueRelation = DueRelation.NONE
    explicit_due_date: str | None = None
    confidence: Confidence = Confidence.MEDIUM
    warnings: list[str] = Field(default_factory=list)


class UncertainItem(BaseModel):
    title: str
    evidence: str = ""
    reason: str
    source_anchor: str | None = None


class IgnoredItem(BaseModel):
    title: str
    evidence: str = ""
    reason: str
    source_anchor: str | None = None


class ExtractionOutcome(BaseModel):
    used_mode: ExtractionMode
    tasks: list[ExtractedTask] = Field(default_factory=list)
    uncertain: list[UncertainItem] = Field(default_factory=list)
    fallback_reasons: list[str] = Field(default_factory=list)


class DraftTask(BaseModel):
    course_id: str
    source_key: str
    source_url: str
    source_anchor: str
    source_text: str
    ordinal: int = 0
    title: str
    details: str
    classification: TaskClassification
    action_kind: ActionKind
    due_date: date | None = None
    due_basis: str
    source_date: date | None = None
    historical: bool = False
    fingerprint: str


class DesiredTask(DraftTask):
    logical_id: str
    managed_notes: str


class RemoteTask(BaseModel):
    id: str
    title: str = ""
    notes: str = ""
    due: str | None = None
    status: str = "needsAction"
    deleted: bool = False
    hidden: bool = False


class StateRecord(BaseModel):
    logical_id: str
    course_id: str
    source_key: str
    anchor: str
    ordinal: int
    fingerprint: str
    source_text: str
    title: str
    due_date: str | None = None
    google_task_id: str | None = None
    tasklist_id: str | None = None
    payload_hash: str | None = None


class SyncAction(BaseModel):
    kind: SyncActionKind
    title: str
    logical_id: str | None = None
    due_date: date | None = None
    reason: str
    evidence: str | None = None
    source_anchor: str | None = None
    remote_task_id: str | None = None
    desired: DesiredTask | None = None


class SyncPlan(BaseModel):
    course_id: str
    task_list: str
    dry_run: bool
    extraction_mode: ExtractionMode
    fallback_reasons: list[str] = Field(default_factory=list)
    actions: list[SyncAction] = Field(default_factory=list)

    def grouped(self) -> dict[SyncActionKind, list[SyncAction]]:
        return {
            kind: [action for action in self.actions if action.kind == kind]
            for kind in SyncActionKind
        }
