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
from canvas_task_sync.sources.canvas import CanvasAgendaSource, CanvasSourceError
from canvas_task_sync.sources.google_slides import GoogleSlidesSource


class CourseAgendaSource:
    """Try Canvas first and instantiate the configured fallback only if it is needed."""

    def __init__(
        self,
        primary_factory: Any | None,
        fallback_factory: Any | None,
        strategy: AcquisitionStrategy,
    ) -> None:
        self.primary_factory = primary_factory
        self.primary: SourceAdapter | None = None
        self.fallback_factory = fallback_factory
        self.strategy = strategy
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

    def capture(self, *, include_image: bool):
        if self.strategy == AcquisitionStrategy.CANVAS_API:
            self.selected = self._primary()
            return self.selected.capture(include_image=include_image)
        if self.strategy == AcquisitionStrategy.CONFIGURED_SOURCE:
            self.selected = self._fallback()
            return self.selected.capture(include_image=include_image)
        if self.primary_factory is not None:
            try:
                self.selected = self._primary()
                return self.selected.capture(include_image=include_image)
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
        )

    fallback_factory: Any | None = None
    if course.source.type != "none":

        def configured_fallback() -> SourceAdapter:
            fallback_settings = course.source.model_copy(deep=True)
            return create_source_adapter(fallback_settings, credentials, **kwargs)

        fallback_factory = configured_fallback

    return CourseAgendaSource(canvas_factory, fallback_factory, acquisition_strategy)


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
