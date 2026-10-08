from __future__ import annotations

import re
from datetime import date, timedelta
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
# "Publish to web" links (/d/e/2PACX-...) carry no file ID the APIs or the extension accept.
PUBLISHED_WORKSPACE_PATH = re.compile(r"^/(?:presentation|document|spreadsheets)/d/e/")
PUBLISHED_URL_ERROR = (
    "url is a 'Publish to web' link (/d/e/...), which has no Google file ID. Use the editor URL, "
    "or embed the published deck in Canvas, where Canvas discovery reads it automatically"
)

GEMINI_MODEL_OPTIONS = (
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
)
GeminiModelName = Literal[
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
]
GeminiReasoningLevel = Literal["low", "medium", "high"]

# Which agent turns agenda evidence into task candidates. Gemini runs through the API key in
# .env; Claude and Codex run through their SDKs with this machine's own sign-ins, so they draw
# from the signed-in subscription's usage rather than from an API key.
AgentProvider = Literal["gemini", "claude", "codex"]
AgentEffort = Literal["low", "medium", "high", "xhigh", "max"]
AGENT_PROVIDER_LABELS: dict[str, str] = {"gemini": "Gemini", "claude": "Claude", "codex": "Codex"}
_AGENT_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")


class AgentModelOption(BaseModel):
    id: str
    label: str
    # Empty when the model takes no effort setting.
    efforts: tuple[str, ...]


AGENT_MODEL_OPTIONS: dict[str, tuple[AgentModelOption, ...]] = {
    "gemini": tuple(
        AgentModelOption(
            id=model,
            label=model.removeprefix("gemini-").replace("-", " "),
            efforts=("low", "medium", "high"),
        )
        for model in GEMINI_MODEL_OPTIONS
    ),
    "claude": (
        AgentModelOption(id="claude-sonnet-5-5", label="Sonnet 5.5", efforts=_AGENT_EFFORTS),
        # Haiku 4.5 is the newest Haiku Claude Code offers, and it has no effort setting.
        AgentModelOption(id="claude-haiku-4-5-20251001", label="Haiku 4.5", efforts=()),
        AgentModelOption(id="claude-opus-5-5", label="Opus 5.5", efforts=_AGENT_EFFORTS),
    ),
    "codex": (
        AgentModelOption(id="gpt-6-luna", label="GPT-6 Luna", efforts=_AGENT_EFFORTS),
        AgentModelOption(id="gpt-6.1-sol", label="GPT-6.1 Sol", efforts=_AGENT_EFFORTS),
    ),
}


def agent_model_option(provider: str, model: str) -> AgentModelOption | None:
    return next(
        (option for option in AGENT_MODEL_OPTIONS.get(provider, ()) if option.id == model),
        None,
    )


class ExtractionAgentSettings(BaseModel):
    """The one agent and model every course's extraction uses.

    For Gemini, ``model: null`` keeps each course's own model chain and reasoning from the
    Courses page; a Gemini model here replaces them for every course.
    """

    provider: AgentProvider = "gemini"
    model: str | None = None
    effort: AgentEffort = "medium"

    @model_validator(mode="after")
    def validate_model(self) -> ExtractionAgentSettings:
        options = AGENT_MODEL_OPTIONS[self.provider]
        if self.model is None and self.provider != "gemini":
            self.model = options[0].id
        if self.model is None:  # Per-course Gemini settings, including their reasoning.
            return self
        option = agent_model_option(self.provider, self.model)
        if option is None:
            choices = ", ".join(item.id for item in options)
            label = AGENT_PROVIDER_LABELS[self.provider]
            raise ValueError(f"{label} model must be one of: {choices}")
        if option.efforts and self.effort not in option.efforts:
            raise ValueError(
                f"{option.label} effort must be one of: {', '.join(option.efforts)}"
            )
        return self

    @property
    def provider_label(self) -> str:
        return AGENT_PROVIDER_LABELS[self.provider]

    def describe(self) -> str:
        """A short status line, such as "Claude · Sonnet 5.5 · medium effort"."""
        if self.model is None:
            return f"{self.provider_label} · per-course models"
        option = agent_model_option(self.provider, self.model)
        parts = [self.provider_label, option.label if option else self.model]
        if option is not None and option.efforts:
            parts.append(f"{self.effort} {'reasoning' if self.provider == 'gemini' else 'effort'}")
        return " · ".join(parts)


class ResolvedExtractionAgent(BaseModel):
    """What one course's extraction actually runs, after global and course settings merge."""

    provider: AgentProvider
    # The primary model first; only Gemini has fallbacks.
    models: list[str]
    # None for a model without an effort setting.
    effort: str | None

    @property
    def model(self) -> str:
        return self.models[0]

    @property
    def provider_label(self) -> str:
        return AGENT_PROVIDER_LABELS[self.provider]

    @property
    def cache_key(self) -> str:
        if self.provider == "gemini":
            # Unchanged from before agents existed, so existing Gemini cache entries still hit.
            return f"{' -> '.join(self.models)}|reasoning:{self.effort}"
        return f"{self.provider}:{self.model}|effort:{self.effort or 'none'}"


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


def _reject_published_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.hostname == "docs.google.com" and PUBLISHED_WORKSPACE_PATH.match(parsed.path):
        raise ValueError(PUBLISHED_URL_ERROR)


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

    @field_validator("assignments_default_due")
    @classmethod
    def validate_assignments_default_due(cls, value: DueRelation) -> DueRelation:
        if value == DueRelation.DAYS_AFTER:
            raise ValueError(
                "assignments_default_due cannot be days_after; state a days-after rule in the "
                "course's AI instructions instead"
            )
        return value


class GoogleSlidesSourceSettings(BaseModel):
    type: Literal["google_slides"] = "google_slides"
    url: str
    page_id: str
    extraction: ExtractionSettings = Field(default_factory=ExtractionSettings)

    @field_validator("url")
    @classmethod
    def validate_google_slides_url(cls, value: str) -> str:
        _reject_published_url(value)
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
        _reject_published_url(value)
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


class NoFallbackSourceSettings(BaseModel):
    type: Literal["none"] = "none"
    extraction: ExtractionSettings = Field(default_factory=ExtractionSettings)


SourceSettings = Annotated[
    GoogleSlidesSourceSettings | BrowserSourceSettings | NoFallbackSourceSettings,
    Field(discriminator="type"),
]


class CanvasAgendaOverride(BaseModel):
    """A user's explicit correction of one Canvas table's week, with a bounded lifetime."""

    page_slug: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_-]+$")
    table_number: int = Field(default=1, ge=1, le=100)
    expected_heading_date: date
    target_week_start: date
    required_text: str = Field(min_length=1, max_length=200)

    @field_validator("page_slug", "required_text", mode="before")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return " ".join(value.split()) if isinstance(value, str) else value

    @field_validator("target_week_start")
    @classmethod
    def validate_target_week(cls, value: date) -> date:
        if value.weekday() != 0:
            raise ValueError("The override target week must begin on a Monday.")
        return value

    @property
    def expires_on(self) -> date:
        return self.target_week_start + timedelta(days=6)


class CourseSettings(BaseModel):
    enabled: bool = True
    name: str
    prefix: str
    task_list: str
    assessment_task_list: str = "Tests"
    ai_instructions: str = ""
    gemini_model: GeminiModelName | None = None
    gemini_fallback_models: list[GeminiModelName] | None = None
    gemini_reasoning: GeminiReasoningLevel = "medium"
    timezone: str = "America/New_York"
    meeting_days: list[str] = Field(default_factory=lambda: ["mon", "tue", "wed", "thu", "fri"])
    canvas_course_id: str | None = None
    canvas_base_url: str | None = None
    canvas_agenda_override: CanvasAgendaOverride | None = None
    source: SourceSettings

    @model_validator(mode="after")
    def validate_agenda_sources(self) -> CourseSettings:
        if self.canvas_agenda_override is not None and not self.canvas_course_id:
            raise ValueError("canvas_course_id is required for a temporary Canvas agenda override")
        if self.source.type == "none" and not self.canvas_course_id:
            raise ValueError("canvas_course_id is required when no fallback source is configured")
        has_primary = self.gemini_model is not None
        has_fallbacks = self.gemini_fallback_models is not None
        if has_primary != has_fallbacks:
            raise ValueError(
                "gemini_model and gemini_fallback_models must be configured together"
            )
        if self.gemini_model is not None and self.gemini_fallback_models is not None:
            chain = [self.gemini_model, *self.gemini_fallback_models]
            if len(chain) != len(set(chain)):
                raise ValueError("The course Gemini model chain cannot contain duplicates")
        return self

    @field_validator("canvas_course_id")
    @classmethod
    def validate_canvas_course_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        if not re.fullmatch(r"\d+", normalized):
            raise ValueError("canvas_course_id must contain only digits")
        return normalized

    @field_validator("canvas_base_url")
    @classmethod
    def validate_canvas_base_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.strip().rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("canvas_base_url must be an http(s) Canvas origin")
        return normalized

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

    @field_validator("name", "prefix", "task_list", "assessment_task_list")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value cannot be empty")
        return value.strip()

    @field_validator("ai_instructions")
    @classmethod
    def normalize_ai_instructions(cls, value: str) -> str:
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
        default_factory=lambda: [
            "gemini-3.6-flash",
            "gemini-3.5-flash",
            "gemini-3.5-flash-lite",
        ]
    )
    extraction_agent: ExtractionAgentSettings = Field(default_factory=ExtractionAgentSettings)
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

    def gemini_model_chain_for(self, course: CourseSettings) -> list[str]:
        if course.gemini_model is None or course.gemini_fallback_models is None:
            return self.gemini_model_chain
        return [course.gemini_model, *course.gemini_fallback_models]

    def gemini_cache_key_for(self, course: CourseSettings) -> str:
        return " -> ".join(self.gemini_model_chain_for(course))

    def extraction_agent_for(self, course: CourseSettings) -> ResolvedExtractionAgent:
        agent = self.extraction_agent
        if agent.provider == "gemini":
            if agent.model is None:
                return ResolvedExtractionAgent(
                    provider="gemini",
                    models=self.gemini_model_chain_for(course),
                    effort=course.gemini_reasoning,
                )
            fallbacks = [model for model in self.gemini_model_chain if model != agent.model]
            return ResolvedExtractionAgent(
                provider="gemini", models=[agent.model, *fallbacks], effort=agent.effort
            )
        assert agent.model is not None  # The validator fills in the provider's default.
        option = agent_model_option(agent.provider, agent.model)
        return ResolvedExtractionAgent(
            provider=agent.provider,
            models=[agent.model],
            effort=agent.effort if option is not None and option.efforts else None,
        )

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
