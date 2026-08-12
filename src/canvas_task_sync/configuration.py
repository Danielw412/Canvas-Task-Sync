from __future__ import annotations

from pathlib import Path
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
    type: str = "google_slides"
    url: str
    page_id: str
    extraction: ExtractionSettings = Field(default_factory=ExtractionSettings)

    @model_validator(mode="after")
    def validate_source_type(self) -> GoogleSlidesSourceSettings:
        if self.type != "google_slides":
            raise ValueError(f"Unsupported source type: {self.type}")
        return self

    @field_validator("url")
    @classmethod
    def validate_google_slides_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "docs.google.com"
            or "/presentation/d/" not in parsed.path
        ):
            raise ValueError("url must be a Google Slides presentation URL")
        return value

    @field_validator("page_id")
    @classmethod
    def validate_page_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("page_id cannot be empty")
        return value.strip()


class CourseSettings(BaseModel):
    enabled: bool = True
    name: str
    prefix: str
    task_list: str
    timezone: str = "America/New_York"
    meeting_days: list[str] = Field(default_factory=lambda: ["mon", "tue", "wed", "thu", "fri"])
    source: GoogleSlidesSourceSettings

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
    gemini_model: str = "gemini-3.6-flash"
    courses: dict[str, CourseSettings]
    root_dir: Path = Path(".")

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
