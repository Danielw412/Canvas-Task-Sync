from __future__ import annotations

from typing import Any

from google.oauth2.credentials import Credentials

from canvas_task_sync.configuration import (
    BrowserSourceSettings,
    CourseSettings,
    NoFallbackSourceSettings,
    SourceSettings,
)
from canvas_task_sync.models import AcquisitionStrategy
from canvas_task_sync.sources.base import IncrementalImageSourceAdapter, SourceAdapter
from canvas_task_sync.sources.browser_connector import BrowserConnectorSource
from canvas_task_sync.sources.canvas import (
    CanvasAgendaNotFound,
    CanvasAgendaOverrideError,
    CanvasAgendaSource,
    CanvasSourceError,
)
from canvas_task_sync.sources.google_slides import GoogleSlidesSource


class CourseAgendaSource:
    """Try Canvas first and instantiate the configured fallback only if it is needed.

    An ``agenda_resolver`` (the agent agenda verifier, given only for Claude and Codex) checks
    what Canvas discovery chose before it is used. It returns the verified capture, or raises a
    ``CanvasSourceError`` that falls back exactly as a missing agenda does. When discovery finds
    nothing, it is asked to look only if ``discovers`` says so: with a configured fallback the
    fallback is used as before, without an agent turn.
    """

    def __init__(
        self,
        primary_factory: Any | None,
        fallback_factory: Any | None,
        strategy: AcquisitionStrategy,
        agenda_resolver: Any | None = None,
    ) -> None:
        self.primary_factory = primary_factory
        self.primary: SourceAdapter | None = None
        self.fallback_factory = fallback_factory
        self.strategy = strategy
        self.agenda_resolver = agenda_resolver
        self.selected: SourceAdapter | None = None

    def _primary(self) -> SourceAdapter:
        if self.primary_factory is None:
            raise CanvasSourceError("This course does not have a Canvas course ID configured.")
        if self.primary is None:
            self.primary = self.primary_factory()
        return self.primary

    def _fallback(self) -> SourceAdapter:
        if self.fallback_factory is None:
            raise CanvasSourceError("No configured fallback source is available for this course.")
        return self.fallback_factory()

    def _canvas_capture(self, *, include_image: bool, fallback_available: bool):
        self.selected = self._primary()
        resolver = self.agenda_resolver
        try:
            capture = self.selected.capture(include_image=include_image)
        except CanvasAgendaOverrideError:
            raise
        except CanvasAgendaNotFound as missing:
            if resolver is None or not resolver.discovers(fallback_available=fallback_available):
                raise
            return resolver.resolve(self.selected, None, missing)
        # A temporary override is the course owner's own explicit, guarded choice.
        if resolver is None or capture.source_metadata.get("agenda_override"):
            return capture
        return resolver.resolve(self.selected, capture, None)

    def capture(self, *, include_image: bool):
        if self.strategy == AcquisitionStrategy.CANVAS_API:
            return self._canvas_capture(include_image=include_image, fallback_available=False)
        if self.strategy == AcquisitionStrategy.CONFIGURED_SOURCE:
            self.selected = self._fallback()
            return self.selected.capture(include_image=include_image)
        if self.primary_factory is not None:
            try:
                return self._canvas_capture(
                    include_image=include_image,
                    fallback_available=self.fallback_factory is not None,
                )
            except CanvasAgendaOverrideError:
                raise
            except CanvasSourceError as error:
                if self.fallback_factory is None:
                    raise
                fallback = self._fallback()
                self.selected = fallback
                capture = fallback.capture(include_image=include_image)
                metadata = dict(capture.source_metadata)
                metadata["acquisition_fallback"] = {
                    "from": "canvas_api",
                    "to": capture.source_type,
                    "reason": str(error),
                }
                verification = getattr(error, "verification", None)
                if verification is not None:
                    metadata["acquisition_fallback"]["agenda_verification"] = (
                        verification.status.value
                    )
                return capture.model_copy(update={"source_metadata": metadata})
        self.selected = self._fallback()
        return self.selected.capture(include_image=include_image)

    def add_image(self, capture):
        if self.selected is None:
            raise RuntimeError("Capture the source before requesting an image.")
        add_image = getattr(self.selected, "add_image", None)
        if not callable(add_image):
            raise RuntimeError("The selected agenda source cannot provide an image.")
        return add_image(capture)


def create_source_adapter(
    settings: SourceSettings,
    credentials: Credentials,
    **kwargs: Any,
) -> SourceAdapter:
    """Create a configured adapter; future source types register at this boundary."""
    if settings.type == "google_slides":
        kwargs.pop("capture_broker", None)
        return GoogleSlidesSource(settings, credentials, **kwargs)
    if settings.type == "browser":
        capture_broker = kwargs.pop("capture_broker", None)
        if kwargs:
            unexpected = ", ".join(sorted(kwargs))
            raise TypeError(f"Unexpected browser source option(s): {unexpected}")
        return BrowserConnectorSource(settings, capture_broker=capture_broker)
    raise ValueError(f"Unsupported source adapter type: {settings.type}")


def create_course_source_adapter(
    course: CourseSettings,
    credentials: Credentials,
    *,
    target_week_start: Any,
    acquisition_strategy: AcquisitionStrategy = AcquisitionStrategy.AUTO,
    agenda_resolver: Any | None = None,
    **kwargs: Any,
) -> SourceAdapter:
    canvas_factory: Any | None = None
    if course.canvas_course_id:
        canvas_course_id = course.canvas_course_id

        canvas_factory = lambda: CanvasAgendaSource(  # noqa: E731 - lazy credential lookup.
            course_id=canvas_course_id,
            target_week_start=target_week_start,
            base_url=course.canvas_base_url,
            timezone_name=course.timezone,
            agenda_override=course.canvas_agenda_override,
        )

    fallback_factory: Any | None = None
    if course.source.type != "none":

        def configured_fallback() -> SourceAdapter:
            fallback_settings = course.source.model_copy(deep=True)
            return create_source_adapter(fallback_settings, credentials, **kwargs)

        fallback_factory = configured_fallback

    return CourseAgendaSource(
        canvas_factory, fallback_factory, acquisition_strategy, agenda_resolver
    )


__all__ = [
    "BrowserConnectorSource",
    "BrowserSourceSettings",
    "CanvasAgendaSource",
    "CanvasSourceError",
    "CourseAgendaSource",
    "GoogleSlidesSource",
    "IncrementalImageSourceAdapter",
    "NoFallbackSourceSettings",
    "SourceAdapter",
    "create_course_source_adapter",
    "create_source_adapter",
]
