from __future__ import annotations

from typing import Any

from google.oauth2.credentials import Credentials

from canvas_task_sync.configuration import GoogleSlidesSourceSettings
from canvas_task_sync.sources.base import IncrementalImageSourceAdapter, SourceAdapter
from canvas_task_sync.sources.google_slides import GoogleSlidesSource


def create_source_adapter(
    settings: GoogleSlidesSourceSettings,
    credentials: Credentials,
    **kwargs: Any,
) -> SourceAdapter:
    """Create a configured adapter; future source types register at this boundary."""
    if settings.type == "google_slides":
        return GoogleSlidesSource(settings, credentials, **kwargs)
    raise ValueError(f"Unsupported source adapter type: {settings.type}")


__all__ = [
    "GoogleSlidesSource",
    "IncrementalImageSourceAdapter",
    "SourceAdapter",
    "create_source_adapter",
]
