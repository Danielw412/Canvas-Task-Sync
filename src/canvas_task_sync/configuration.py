from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from canvas_task_sync.models import ActionKind, DueRelation, ExtractionMode

WEEKDAY_NAMES = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}

GOOGLE_WORKSPACE_PATHS = {
    "google_slides": re.compile(r"/presentation/(?:u/\d+/)?d/"),
    "google_docs": re.compile(r"/document/(?:u/\d+/)?d/"),
    "google_sheets": re.compile(r"/spreadsheets/(?:u/\d+/)?d/"),
}


def _google_workspace_source_type(value: str) -> str | None:
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname != "docs.google.com":
        return None
    return next(
        (
            source_type
            for source_type, pattern in GOOGLE_WORKSPACE_PATHS.items()
            if pattern.search(parsed.path)
        ),
        None,
    )


class ExtractionSettings(BaseModel):
    mode: ExtractionMode = ExtractionMode.HYBRID
    thumbnail_size: str = "large"
    assignments_default_due: DueRelation = DueRelation.NEXT_CLASS
    same_day_action_kinds: set[ActionKind] = Field(
        default_factory=lambda: {ActionKind.BRING, ActionKind.PRESENT, ActionKind.SUBMIT}
    )

    @field_validator("thumbnail_size")
    @classmethod
    def validate_thumbnail_size(cls, value: str) -> str:
        normalized = value.lower()
        if normalized not in {"small", "medium", "large"}:
            raise ValueError("thumbnail_size must be small, medium, or large")
        return normalized


class GoogleSlidesSourceSettings(BaseModel):
    type: Literal["google_slides"] = "google_slides"
    url: str
    page_id: str
    extraction: ExtractionSettings = Field(default_factory=ExtractionSettings)

    @field_validator("url")
    @classmethod
    def validate_google_slides_url(cls, value: str) -> str:
        if _google_workspace_source_type(value) != "google_slides":
            raise ValueError("url must be a Google Slides presentation URL")
        return value

    @field_validator("page_id")
    @classmethod
    def validate_page_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("page_id cannot be empty")
        return value.strip()


class BrowserSheetSelection(BaseModel):
    sheet_id: str | None = None
    sheet_name: str | None = None
    range_a1: str | None = None

    @model_validator(mode="after")
    def validate_identifier(self) -> BrowserSheetSelection:
        if not (self.sheet_id or self.sheet_name):
            raise ValueError("A sheet selection needs a sheet_id or sheet_name")
        if self.range_a1 is not None:
            self.range_a1 = self.range_a1.strip() or None
        return self


class BrowserSelectionSettings(BaseModel):
    slide_ids: list[str] = Field(default_factory=list)
    section_ids: list[str] = Field(default_factory=list)
    sheets: list[BrowserSheetSelection] = Field(default_factory=list)

    @field_validator("slide_ids", "section_ids")
    @classmethod
    def normalize_identifiers(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))


BrowserSourceFormat = Literal[
    "auto",
    "google_slides",
    "google_docs",
    "google_sheets",
]


class BrowserSourceSettings(BaseModel):
    type: Literal["browser"] = "browser"
    url: str
    source_format: BrowserSourceFormat = "auto"
    freshness_seconds: int = Field(default=900, ge=30, le=3600)
    selection: BrowserSelectionSettings = Field(default_factory=BrowserSelectionSettings)
    extraction: ExtractionSettings = Field(default_factory=ExtractionSettings)

    @field_validator("url")
    @classmethod
    def validate_google_workspace_url(cls, value: str) -> str:
        if _google_workspace_source_type(value) is None:
            raise ValueError("url must be a Google Slides, Docs, or Sheets URL")
        return value

    @model_validator(mode="after")
    def validate_format_matches_url(self) -> BrowserSourceSettings:
        detected = _google_workspace_source_type(self.url)
        if detected is None:  # The field validator reports the user-facing URL error first.
            return self
        if self.source_format != "auto" and self.source_format != detected:
            raise ValueError(
                f"source_format {self.source_format!r} does not match the configured URL"
            )
        return self


SourceSettings = Annotated[
    GoogleSlidesSourceSettings | BrowserSourceSettings,
    Field(discriminator="type"),
]


class CourseSettings(BaseModel):
    enabled: bool = True
    name: str
    prefix: str
    task_list: str
    timezone: str = "America/New_York"
    meeting_days: list[str] = Field(default_factory=lambda: ["mon", "tue", "wed", "thu", "fri"])
    source: SourceSettings

    @field_validator("meeting_days")
    @classmethod
    def validate_meeting_days(cls, values: list[str]) -> list[str]:
        normalized = [value.lower() for value in values]
        unknown = sorted(set(normalized) - set(WEEKDAY_NAMES))
        if unknown:
            raise ValueError(f"Unknown meeting day(s): {', '.join(unknown)}")
        if not normalized:
            raise ValueError("meeting_days cannot be empty")
        return normalized

    @field_validator("name", "prefix", "task_list")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value cannot be empty")
        return value.strip()

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("timezone must be a valid IANA timezone name") from error
        return value

    @property
    def meeting_weekdays(self) -> list[int]:
        return sorted({WEEKDAY_NAMES[name] for name in self.meeting_days})


class ProjectSettings(BaseModel):
    version: int = 1
    state_path: Path = Path(".canvas-task-sync/state.sqlite3")
    gemini_model: str = "gemini-3.7-flash"
    gemini_fallback_models: list[str] = Field(
        default_factory=lambda: ["gemini-3.6-flash", "gemini-3.5-flash"]
    )
    courses: dict[str, CourseSettings]
    root_dir: Path = Path(".")

    @field_validator("gemini_model")
    @classmethod
    def validate_gemini_model(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("gemini_model cannot be empty")
        return value.strip()

    @field_validator("gemini_fallback_models")
    @classmethod
    def validate_gemini_fallback_models(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @property
    def gemini_model_chain(self) -> list[str]:
        return list(dict.fromkeys([self.gemini_model, *self.gemini_fallback_models]))

    @property
    def gemini_cache_key(self) -> str:
        return " -> ".join(self.gemini_model_chain)

    def course(self, course_id: str) -> CourseSettings:
        try:
            return self.courses[course_id]
        except KeyError as error:
            choices = ", ".join(sorted(self.courses)) or "none"
            raise ValueError(
                f"Unknown course '{course_id}'. Configured courses: {choices}"
            ) from error

    @property
    def resolved_state_path(self) -> Path:
        if self.state_path.is_absolute():
            return self.state_path
        return self.root_dir / self.state_path


def load_settings(path: Path) -> ProjectSettings:
    config_path = path.resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    settings = ProjectSettings.model_validate(raw)
    settings.root_dir = config_path.parent.parent
    return settings
